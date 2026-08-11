# devops-mcp

An MCP server that exposes Azure DevOps as tools for LLMs — pipelines, repositories, pull requests, and work items. Built with MCPServer (`mcp.server.mcpserver`) over stdio transport.

## Commands

- **Install deps**: `uv sync`
- **Run server**: `uv run devops-mcp`
- **Build package**: `uv run python -m build`
- **Run tests**: `uv run pytest`
- **Lint**: `uv run ruff check src tests`

`tests/` holds pytest modules that intercept HTTP with an
`httpx.AsyncBaseTransport` stub — no network, no credentials. New tools ship with
tests in that style.

## Architecture

```
src/devops_mcp/
├── server.py          # Entry point; configures logging, imports tools to trigger registration
├── _app.py            # Single MCPServer instance (isolated to avoid circular imports)
├── client.py          # Shared HTTP client, auth credential factory, lifespan context manager
├── models.py          # All Pydantic input models
└── tools/
    ├── pipelines.py           # 10 tools: list/get pipelines, runs, builds, logs, timeline, log search, artifacts
    ├── repositories.py        # 8 tools: list/get repos, branches, file content, image content, items, commits
    ├── pull_requests.py       # 17 tools: get/list/create/update/complete/abandon/vote/tag PRs, work item links, threads/comments, iterations/changes
    ├── work_items.py          # 11 tools: get/list/query/create/update work items, tags, comments, types/fields, delete (gated)
    ├── attachments.py         # 4 tools: list work item attachments (relations + inline), get attachment (returns image content), upload image attachment, link attachment relation
    ├── discovery.py           # 2 tools: list projects, list teams
    ├── advanced_security.py   # 3 tools: list/get/update GHAzDo alerts (advsec.dev.azure.com host)
    ├── service_connections.py # 2 tools: list/get service connections (redacted authorization.parameters)
    └── variable_groups.py     # 2 tools: list/get variable groups (redacted secret values)
```

`redaction.py` — pure secret-hygiene helpers (allowlist/denylist projection, no HTTP/MCP imports) shared by `service_connections.py` and `variable_groups.py`.

`attachment_media.py` — pure media helpers (magic-byte sniffing + format detection, the inline image byte cap, file-name hygiene, attachment-URL parse/validate, inline attachment-URL discovery in field text, embed-snippet construction; no HTTP/MCP/filesystem imports) used by `tools/attachments.py` and `tools/repositories.py`. The URL parser is an SSRF guard, not a formatter: it validates a caller-supplied attachment URL, extracts only the GUID and file name, and the caller rebuilds the request against its own resolved org — never follow (or store) a caller-supplied URL with a bearer token attached. It accepts absolute, protocol-relative (`//host/…`) and site-relative (`/{org}/…`) shapes; the relative ones skip only the checks they have no host for and still have to name the resolved org. `find_attachment_urls` is the opposite kind of function — a deliberately permissive *candidate finder* over user-authored field text, whose every result must still go through `parse_attachment_url`; a candidate that fails is dropped and counted, never echoed back to the model. It anchors on the fixed `/_apis/wit/attachments/{guid}` route and walks left to the URL's start rather than expressing the prefixes as regex alternations, which keeps it linear on hostile 200 KB field text now that a bare `/` can begin a candidate. `file_name_rejection_reason` is the one predicate every file-name entry point shares — the models raise on it, the URL parser drops on it.

A work item's two attachment surfaces are independent: an `AttachedFile` relation (the Attachments tab) and an inline image (a URL inside a large-text field). Enumerating relations alone misses every pasted screenshot, which is why `devops_list_work_item_attachments` scans both and de-duplicates by attachment GUID.

**Key invariants:**
- Python 3.10+, Pydantic v2, `mcp[cli]`, `httpx`, `azure-identity`
- Stdout is reserved for MCP stdio transport — never write to it directly
- All logging goes to stderr via the `logging` module; never use `print()`
- All configuration is env-driven; no hardcoded org names, project names, credentials, or tenant IDs

## Adding a New Tool

1. Add an input model to `src/devops_mcp/models.py`:
   - Name: `{Action}{Resource}Input` (e.g., `GetPipelineRunInput`)
   - Inherit from `AzDoBaseInput` if the tool needs org/project context
   - Annotate every public field with `Field(...)` descriptions and constraints

2. Implement the tool in the appropriate domain module under `src/devops_mcp/tools/`:
   - Name: `devops_{verb}_{noun}` (e.g., `devops_get_pipeline_run`)
   - Return type: `str` — always a JSON document, never Markdown prose.
     Exception, media tools: a tool whose purpose is to return binary media to
     the model returns `list[str | Image]`
     (`mcp.server.mcpserver.utilities.types.Image`), where element 0 is the JSON
     metadata string and any later elements are media blocks. Such a tool MUST be
     registered with `structured_output=False` — the SDK otherwise validates the
     return value against a schema derived from the annotation, and an `Image`
     fails that validation at call time. Errors from a media tool are still
     returned as a single-element list holding the JSON error string, so the
     return type is uniform. `Image(data=…)` MUST be given an explicit `format`
     (`png`/`jpeg`/`gif`/`webp` — note `jpeg`, not `jpg`, since the SDK builds
     the MIME type as `image/{format}` without validating it).
   - Decorate with `@mcp.tool()` from `src/devops_mcp/_app.py`
   - Set `annotations` truthfully: `read_only`, `destructive`, `idempotent`

3. Use `get_http_client()` from `client.py` — do not create ad-hoc HTTP clients.

4. Use `resolve_org()` and `resolve_project()` to merge per-call inputs with env defaults.

## Error Handling Contract

- Catch `httpx.HTTPStatusError` before broad `Exception` catches
- Never let uncaught exceptions escape a tool function
- Return errors as JSON with an actionable message, e.g. `{"error": "Pipeline 42 not found in project 'MyProject'"}`
- Include `count` on list-style responses when practical

## Azure DevOps Conventions

- API versions: prefer versions already used in the repo (v7.1 for pipelines/repos, v7.2-preview for PRs/work items)
- `build_params()` hard-codes `api-version=7.1` — do not use it for work-item endpoints (they need the per-operation `7.2-preview.N`); pass an explicit `params` dict instead
- **The Git Items route returns item *metadata JSON*, not the blob, unless you send `$format=octetStream`.** `download=true` and `includeContent=true` do not help; an `Accept: application/octet-stream` header would, but `build_headers()` always sends `Accept: application/json`. The failure is silent — HTTP 200, plausible JSON where a file should be — so any tool reading a file's bytes sends `_BLOB_FORMAT_PARAM` and ships a test asserting it reaches the wire
- A failed JSON-Patch `test /rev` op answers **HTTP 412 / `VS403351 TestPatchOperationFailedException`**, not 400 — handle 412 (and 409) as the optimistic-concurrency conflict
- For PR-to-work-item links: update the work item `ArtifactLink` relation — do not PATCH `workItemRefs` on the PR
- Org/project resolution order: per-call input → env var → error

## Authentication

Configured entirely via environment variables:

| Variable | Description |
|---|---|
| `AZDO_AUTH_TYPE` | `default` (recommended), `azure_cli`, `interactive`, `client_secret`, `managed_identity` |
| `AZDO_ORGANIZATION` | Default Azure DevOps organization name |
| `AZDO_PROJECT` | Default Azure DevOps project name |
| `AZDO_TENANT_ID` | Required for `interactive` and `client_secret` |
| `AZDO_CLIENT_ID` | Required for `client_secret` |
| `AZDO_CLIENT_SECRET` | Required for `client_secret` |
| `AZDO_TOKEN_CACHE_PROFILE` | Filename-safe suffix (`[A-Za-z0-9_-]`) isolating the interactive token cache + sidecar per tenant/account on a shared host; empty = shared default filenames |
| `AZDO_LOG_LEVEL` | `DEBUG`, `INFO` (default), `WARNING`, `ERROR` |

`default` auth tries all credential sources in order (environment, Azure CLI, managed identity) and is the right choice for local development.

## VS Code MCP Configuration

`.vscode/mcp.json` is gitignored (contains secrets). Minimal example:

```json
{
  "servers": {
    "devops-mcp": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "devops-mcp"],
      "env": {
        "AZDO_ORGANIZATION": "<org>",
        "AZDO_PROJECT": "<project>"
      }
    }
  }
}
```

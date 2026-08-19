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
    ├── queries.py             # 7 tools: list/search, get, run saved queries; create query/folder, update (rename/re-wiql/move/undelete), delete (gated)
    ├── attachments.py         # 4 tools: list work item attachments (relations + inline), get attachment (returns image content), upload image attachment, link attachment relation
    ├── discovery.py           # 2 tools: list projects, list teams
    ├── advanced_security.py   # 3 tools: list/get/update GHAzDo alerts (advsec.dev.azure.com host)
    ├── service_connections.py # 2 tools: list/get service connections (redacted authorization.parameters)
    └── variable_groups.py     # 2 tools: list/get variable groups (redacted secret values)
```

`auth_redirect.py` — the branded landing page for the interactive sign-in redirect (`http://localhost:{8400..8999}/?code=…`), styled after `assets/devops-mcp-banner.svg`. `InteractiveBrowserCredential` accepts a private `_server_class` keyword and drives it as `cls(hostname, port, timeout=…)` then `wait_for_redirect()`, so `BrandedAuthCodeRedirectServer` implements that contract on plain `http.server` rather than subclassing `azure.identity._internal.AuthCodeRedirectServer` — an upstream refactor of that private module then cannot break sign-in. `_new_interactive_credential()` in `client.py` is the only construction site and retries without the keyword on `TypeError`. The page renders `error`/`error_description` escaped and never echoes `code` — it is a live authorization code.

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

## Changelog

Follow [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) **as written** — the plain
example on that page, not an essay. Terse bullets under the standard headings only (`Added`,
`Changed`, `Deprecated`, `Removed`, `Fixed`, `Security`), in that order, omitting the empty ones.

- **One line per change**, ideally one sentence. Lead with the tool or behaviour that changed.
- **No `####` subsections, no prose paragraphs between bullets.** If a bullet needs a clause of
  justification, one trailing `—` clause is the ceiling.
- **Write for someone deciding whether to upgrade**, not for a reviewer of the diff. State what
  changed and what it means for them; leave out how it was built, what was measured, what was
  considered and rejected, and the API archaeology behind it.
- **Rationale, measurements and gotchas go elsewhere** — module docstrings next to the code, the
  PR body, or the commit message. All three outlive the release note and none of them are read by
  someone skimming for breaking changes.
- New version section: `## [X.Y.Z] - YYYY-MM-DD`, plus a compare link at the file's foot.

## Azure DevOps Conventions

- API versions: prefer versions already used in the repo (v7.1 for pipelines/repos, v7.2-preview for PRs/work items)
- `build_params()` hard-codes `api-version=7.1` — do not use it for work-item endpoints (they need the per-operation `7.2-preview.N`); pass an explicit `params` dict instead
- **The Git Items route returns item *metadata JSON*, not the blob, unless you send `$format=octetStream`.** `download=true` and `includeContent=true` do not help; an `Accept: application/octet-stream` header would, but `build_headers()` always sends `Accept: application/json`. The failure is silent — HTTP 200, plausible JSON where a file should be — so any tool reading a file's bytes sends `_BLOB_FORMAT_PARAM` and ships a test asserting it reaches the wire
- A failed JSON-Patch `test /rev` op answers **HTTP 412 / `VS403351 TestPatchOperationFailedException`**, not 400 — handle 412 (and 409) as the optimistic-concurrency conflict
- **`wit/queries` PATCH is merge-patch with plain `Content-Type: application/json`** — a partial `QueryHierarchyItem` holding only the keys you are changing. That is the *opposite* of `wit/workitems`, one module away, which requires `application/json-patch+json` and rejects plain JSON; copying that PATCH here yields a 400 that says nothing about content type. The queries resource also has no `rev` and no ETag, so there is no optimistic concurrency at all
- **A route value that is a multi-segment path — a saved query's `Shared Queries/Folder/Name` — must keep its `/`**: pass it through `build_url` (which uses `quote(path, safe="/")`) and never `quote(..., safe="")`. `%2F` separators return **404 for a query that plainly exists**, and the failure reads as "missing query" rather than "bad encoding", so any tool addressing one ships a wire test asserting `%2F` never appears in the path
- A `tree`/`oneHop` WIQL result returns `workItemRelations` and **omits `workItems` entirely** — `.get("workItems", [])` reports zero rows for a query that returned hundreds. Normalise both shapes and always de-duplicate ids before batching, but do not assume ids repeat: live over one 5-node hierarchy, `oneHop` gave **6 rows over 5 unique ids** (ids do repeat per link) while `tree` gave **5 rows over 5 ids** (one row per node, no repeats) — so the de-dup step is load-bearing for `oneHop` and a harmless no-op for a strict `tree`. The type is chosen by the MODE clause, not by you: `MODE (Recursive)` → `tree`, `MODE (MustContain)` → `oneHop` (both live-confirmed), so never treat `tree` as the only relation-shaped type. Projected row shape, live-confirmed: a **link row** carries `rel` (e.g. `System.LinkTypes.Hierarchy-Forward`) + integer `source_id` + `target_id`; a **root row omits `rel` and `source_id` outright** — absent keys, not nulls — and carries `target_id` alone
- **A soft-deleted saved query is addressable only by its GUID.** `$includeDeleted=true` on a *path* still answers `404 TF401243 QueryItemNotFoundException`; the same item by GUID answers 200 with `isDeleted: true`. So "retry with include_deleted" is dead-end advice for a path — the recovery is: list the *parent folder* with `$includeDeleted`, read the child's GUID, then `PATCH {"isDeleted": false}` at that GUID. Undelete also only cascades on the deleted→live **transition**: re-sending `$undeleteDescendants=true` at a folder that is already live is a 200 that restores nothing, so read `isDeleted` before the PATCH rather than reporting a restore off the status
- Saved-query traps, all live-verified: `wit/queries` DELETE answers **204 with an empty body** (the reference documents a 200 + body); a duplicate name is **`TF237018` at HTTP 400**, never 409 — match the code, not the status; an unknown `{team}` on the `wit/wiql/{id}` route is an **HTTP 500 `TeamNotFoundException`**, so rename it into an input error instead of surfacing a server fault; and `$filter` search matches **query names only**, never folder names
- **`$expand` on `wit/queries` is not monotonic.** Raw key sets off one query, measured live:

  | `$expand` | keys |
  |---|---|
  | `none` | `_links, createdBy, createdDate, id, isPublic, lastModifiedBy, lastModifiedDate, name, path, queryType, url` |
  | `minimal` | `id, isPublic, name, path, queryType, wiql` |
  | `wiql` | `none`'s set **+ `columns` + `wiql`** |
  | `all` | `wiql`'s set **+ `clauses`** |

  So `none` and `minimal` are each other's non-subsets (`minimal` alone has `wiql`; `none` alone has the author/dates/links). Two things are easy to state backwards: **`columns` never arrives at `none`** — only `wiql`/`all` — and **`minimal` does not drop `queryType`**, which is present at every level. Ask for `wiql` when you want the text and the metadata together, and let absent keys stay absent rather than materialising nulls
- **Renaming a ROOT query folder is permitted, answers 200, and bricks path addressing for the whole hierarchy beneath it.** `PATCH wit/queries/Shared Queries` with `{"name": "whatever"}` really renames the root; afterwards no path starts with `Shared Queries`/`My Queries`, so every path-addressed call fails and only GUIDs resolve. Nothing server-side refuses it, so `devops_update_query` does — and the guard cannot key off the name, because a renamed root reports `{"name": "whatever", "path": "whatever"}`. The durable signal is **`isFolder: true` + a single-segment `path`** (only a root has no parent in its path). Leave one way back — but key the exception off the item's **own identity**, not off the new name: "the new name is a valid root name" is *not* enough, because it also permits renaming a **healthy** root onto the other root's name, colliding two roots on one path with no clean owner to rename back. The rule is: only a **damaged** root (its single-segment path is no longer a root name, so it is reachable by GUID alone) may be renamed, only to a root name, and only to the root name **not currently held** by the other live root. A path-addressed root needs no pre-read at all — `_validate_query_ref` already forced the first segment to be a root name, and a damaged root answers to no such path, so a single-segment path ref is provably healthy and refused for free. **Root DELETE is the mirror image and needs no guard**: the service refuses it, live-verified, with `TF237023: You cannot delete root folders.` at **HTTP 400** (not 403), so `devops_delete_query` only has to explain the refusal — key on the code, never the status or `typeKey`, because that type is the generic `LegacyQueryItemException` that `TF237018` also rides. So: rename permitted and dangerous (guard it), delete refused (map it)
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

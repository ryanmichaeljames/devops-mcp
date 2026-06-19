# devops-mcp

An MCP server that exposes Azure DevOps as tools for LLMs — pipelines, repositories, pull requests, and work items. Built with FastMCP over stdio transport.

## Commands

- **Install deps**: `uv sync`
- **Run server**: `uv run devops-mcp`
- **Build package**: `uv run python -m build`

No test suite exists yet. No linter is configured.

## Architecture

```
src/devops_mcp/
├── server.py          # Entry point; configures logging, imports tools to trigger registration
├── _app.py            # Single FastMCP instance (isolated to avoid circular imports)
├── client.py          # Shared HTTP client, auth credential factory, lifespan context manager
├── models.py          # All Pydantic input models
└── tools/
    ├── pipelines.py   # 7 tools: list/get pipelines, runs, builds, logs, artifacts
    ├── repositories.py# 3 tools: list repos, get repo, list branches
    ├── pull_requests.py # 6 tools: get/list/create/update/tag PRs, link work items
    └── work_items.py  # 7 tools: get/list/query/create/update work items, comments
```

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
   - Return type: `str` (always JSON, never Markdown)
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

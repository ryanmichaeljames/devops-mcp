# GitHub Copilot Instructions

## Purpose

This repository is a Python FastMCP server for Azure DevOps, with tool implementations under `src/devops_mcp/tools/`.

## Keep These Invariants

- Python 3.10+, Pydantic v2, `mcp[cli]`, `httpx`, `azure-identity`
- Follow existing project patterns; keep changes small and consistent
- Keep tool modules domain-scoped (`pipelines.py`, `repositories.py`, `pull_requests.py`, `work_items.py`)
- Reuse the shared client helpers in `src/devops_mcp/client.py`

## Tool Design Rules

- Tool names: `devops_{verb}_{noun}`
- Input models: `{Action}{Resource}Input` in `src/devops_mcp/models.py`
- Input models should inherit from `AzDoBaseInput` when they need org/project context
- Use `Field(...)` descriptions and constraints on public inputs
- Set tool annotations truthfully for read-only, destructive, and idempotent behavior

## Response and Error Contract

- Every tool returns `str` containing JSON, not Markdown
- Do not raise uncaught exceptions from tools
- Catch `httpx.HTTPStatusError` before broad exceptions
- Include actionable error messages
- Include `count` on list-style responses when practical

## Azure DevOps Conventions

- Resolve organization and project from per-call input first, then env defaults
- Prefer existing Azure DevOps REST API versions already used in the repo
- For existing PR to work item links, update the work item `ArtifactLink` relation rather than PATCHing PR `workItemRefs`

## Logging and Security

- Never `print()`; use `logging`
- Do not hardcode credentials, tenant IDs, organization names, or project names
- Keep auth env-driven (`AZDO_AUTH_TYPE`, `AZDO_TENANT_ID`, `AZDO_CLIENT_ID`, `AZDO_CLIENT_SECRET`)
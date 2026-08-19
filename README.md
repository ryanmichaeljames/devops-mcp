![devops-mcp](https://raw.githubusercontent.com/ryanmichaeljames/devops-mcp/main/assets/devops-mcp-banner.svg)

[![CI](https://github.com/ryanmichaeljames/devops-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/ryanmichaeljames/devops-mcp/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/devops-mcp)](https://pypi.org/project/devops-mcp/)
[![Downloads](https://img.shields.io/pypi/dm/devops-mcp)](https://pypi.org/project/devops-mcp/)
[![License: MIT](https://img.shields.io/github/license/ryanmichaeljames/devops-mcp)](LICENSE)

An [MCP](https://modelcontextprotocol.io/) server that gives LLMs your Azure DevOps: pipelines, repos, pull requests, work items, and more. 66 tools, read-only by default.

Runs over stdio, so it works with Claude Code, GitHub Copilot, Cursor, and any other MCP client.

---

## Quick start

You don't start the server yourself — your MCP client launches it. Install [uv](https://docs.astral.sh/uv/) (it provides `uvx`), run `az login`, then register the server.

**Claude Code** — `.mcp.json` in your project root:

```json
{
  "mcpServers": {
    "devops-mcp": {
      "command": "uvx",
      "args": ["devops-mcp"],
      "env": {
        "AZDO_ORGANIZATION": "<your-org>",
        "AZDO_PROJECT": "<your-project>"
      }
    }
  }
}
```

**VS Code / GitHub Copilot** — `.vscode/mcp.json`, same thing under `servers` with an explicit type:

```json
{
  "servers": {
    "devops-mcp": {
      "type": "stdio",
      "command": "uvx",
      "args": ["devops-mcp"],
      "env": {
        "AZDO_ORGANIZATION": "<your-org>",
        "AZDO_PROJECT": "<your-project>"
      }
    }
  }
}
```

Restart the client, then ask your agent to *list my Azure DevOps projects*. Reads work straight away; writes need a gate (below).

---

## Configuration

Everything is set by environment variable. Nothing is hardcoded.

| Variable | Default | What it does |
|---|---|---|
| `AZDO_ORGANIZATION` | — | Default org. Any tool call can override it. |
| `AZDO_PROJECT` | — | Default project. Any tool call can override it. |
| `AZDO_AUTH_TYPE` | `default` | `default`, `azure_cli`, `interactive`, `client_secret`, or `managed_identity`. |
| `AZDO_TENANT_ID` | — | Required for `client_secret`. Recommended for `interactive`. |
| `AZDO_CLIENT_ID` | — | Service principal ID (`client_secret` only). |
| `AZDO_CLIENT_SECRET` | — | Service principal secret (`client_secret` only). |
| `AZDO_ALLOW_WRITE` | off | `true` registers the write tools. |
| `AZDO_ALLOW_DELETE` | off | `true` registers the delete tools. |
| `AZDO_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR`. Logs go to stderr. |
| `AZDO_AUTH_TIMEOUT_SECONDS` | `30` | Give up on credential acquisition after this long. Raise it for slow MFA. |
| `AZDO_EPHEMERAL_TOKEN` | `false` | `interactive` only. `true` keeps the token cache in memory, so every restart re-prompts. |
| `AZDO_TOKEN_CACHE_PROFILE` | — | `interactive` only. Suffix (`[A-Za-z0-9_-]`) that keeps two tenants' caches apart on one host. |
| `AZDO_ATTACHMENT_ROOT` | — | Confine attachment uploads to one directory tree. |

---

## Authentication

Microsoft Entra ID OAuth 2.0 via [`azure-identity`](https://pypi.org/project/azure-identity/).

| `AZDO_AUTH_TYPE` | How it signs in | Use it for |
|---|---|---|
| `default` | Tries env vars, Azure CLI, managed identity in order. Never prompts. | **Most setups** |
| `azure_cli` | Your `az login` session. | Local dev |
| `interactive` | Opens a browser. Supports MFA. | Local dev without the CLI |
| `client_secret` | Service principal. Needs tenant, client ID, and secret. | CI/CD |
| `managed_identity` | Azure managed identity. No credentials to store. | Azure-hosted workloads |

`interactive` prompts once. After that the token cache (encrypted by the OS secret store) makes restarts silent, until the refresh token expires. Two things worth knowing:

- On headless Linux with no secret store, the cache falls back to memory and the server logs a warning. `AZDO_EPHEMERAL_TOKEN=true` silences it.
- Two sessions signed in to different tenants share cache files and clobber each other. Give each a distinct `AZDO_TOKEN_CACHE_PROFILE`.

---

## Security

**Write and delete tools are off by default.** They aren't registered, so the agent can't see them, let alone call them. Set `AZDO_ALLOW_WRITE=true` and `AZDO_ALLOW_DELETE=true` to opt in.

**Deletes are recoverable by default.** `devops_delete_work_item` sends the item to the recycle bin; `destroy=true` is opt-in per call, and Azure DevOps gates it behind a separate admin permission. `devops_delete_query` is always soft — restore with `devops_update_query(undelete=true)`, though permissions on the item don't come back.

**Attachment URLs are parsed, then thrown away.** An `<img src>` from a work item description is untrusted input, so following it with a bearer token attached would leak that token. Instead the URL is validated (https, `dev.azure.com` or `{org}.visualstudio.com`, the attachment route, the right org), only the GUID and file name are kept, and the request is rebuilt against the resolved org. Rejections happen before a token is minted. A URL that fails is counted, never echoed back.

**Uploads are images only, checked by bytes.** `devops_upload_work_item_attachment` requires a `.png/.jpg/.jpeg/.gif/.webp` name *and* matching magic bytes, so `id_rsa` renamed to `x.png` is refused. `AZDO_ATTACHMENT_ROOT` restricts it further.

**Secrets are never returned.** Service connection `authorization.parameters` are filtered through an allowlist of non-secret fields; variable group secrets are dropped, as are non-secret variables with credential-shaped names. Withheld field *names* are reported so an agent knows a credential exists.

**Stdout belongs to the MCP transport.** All logs go to stderr. Don't redirect stdout.

**Retries are safe by construction.** `429` retries on any method; `502/503/504` retries only on reads, because a gateway error on a write may land after the write committed. Responses over 5 MB are refused with a hint to narrow the query.

---

## Client setup

Every client launches the same stdio process; only the JSON wrapper differs. Claude and Cursor use `mcpServers`, VS Code uses `servers` plus `"type": "stdio"`.

| Client | Config file |
|---|---|
| Claude Code | `.mcp.json` in the project root, or `~/.claude.json` for local scope |
| Claude Desktop | `%APPDATA%\Claude\claude_desktop_config.json` (Windows), `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) |
| VS Code / GitHub Copilot | `.vscode/mcp.json` in the workspace root |
| Cursor | `.cursor/mcp.json`, or `~/.cursor/mcp.json` for all projects |

Keep these files out of source control — they can hold a client secret. Restart the client after editing; the write and delete gates are read once at startup.

**Enabling writes.** Add `"AZDO_ALLOW_WRITE": "true"` to the `env` block, and `"AZDO_ALLOW_DELETE": "true"` for deletes.

**Service principal (CI/CD).** Swap the `env` block for:

```json
{
  "AZDO_AUTH_TYPE": "client_secret",
  "AZDO_TENANT_ID": "<your-tenant-id>",
  "AZDO_CLIENT_ID": "<your-client-id>",
  "AZDO_CLIENT_SECRET": "<your-client-secret>",
  "AZDO_ORGANIZATION": "<your-org>",
  "AZDO_PROJECT": "<your-project>"
}
```

**Local checkout.** Point the client at your clone — `uv` syncs the environment before launching, and `--directory` means the client's working directory doesn't matter:

```json
{
  "command": "uv",
  "args": ["--directory", "/path/to/devops-mcp", "run", "devops-mcp"]
}
```

**Two tenants on one host.** Register one entry per tenant, each with its own `AZDO_TENANT_ID` and `AZDO_TOKEN_CACHE_PROFILE` (`prod`, `dev`, …). Each signs in once, then restarts silently as its own account. One entry already reaches any org you have access to — `organization` and `project` are per-call arguments — so add a second only for a different account or tenant.

---

## Tools

66 tools across 9 domains. The **gate** column says when a tool is registered: `default` always, `write` under `AZDO_ALLOW_WRITE=true`, `delete` under `AZDO_ALLOW_DELETE=true`.

### Pipelines (10)

| Tool | Gate | Description |
|---|---|---|
| `devops_list_pipelines` | default | List a project's pipelines |
| `devops_list_pipeline_runs` | default | List runs of one pipeline |
| `devops_get_pipeline_run` | default | Get one run |
| `devops_get_build` | default | Get a build by `buildId` |
| `devops_list_run_logs` | default | List a build's log files |
| `devops_get_run_timeline` | default | Failure-filtered timeline with inline errors — start here for "why did it fail" |
| `devops_get_run_log_content` | default | Read log text, paged (500 lines by default) with `tail` and line windows |
| `devops_search_run_log` | default | Grep a log; only matching lines come back |
| `devops_list_build_artifacts` | default | List a build's artifacts |
| `devops_run_pipeline` | write | Queue a run; override branch, parameters, variables, or skip stages |

### Repositories (8)

| Tool | Gate | Description |
|---|---|---|
| `devops_list_repositories` | default | List repos |
| `devops_get_repository` | default | Get one repo |
| `devops_list_branches` | default | List branches |
| `devops_get_file_content` | default | Read a text file at a branch or commit |
| `devops_get_repository_image` | default | Read a committed image and return it as viewable content |
| `devops_list_repository_items` | default | Browse files and folders at a chosen depth |
| `devops_list_commits` | default | List commits, filtered by branch, author, or date |
| `devops_get_commit` | default | Get one commit, optionally with changed paths |

### Pull requests (17)

| Tool | Gate | Description |
|---|---|---|
| `devops_get_pull_request` | default | Get one PR |
| `devops_list_pull_requests` | default | List PRs by status, branch, creator, reviewer, or label |
| `devops_list_pull_request_threads` | default | List comment threads |
| `devops_get_pull_request_thread` | default | Get one thread and its comments |
| `devops_list_pull_request_iterations` | default | List pushes to the PR |
| `devops_get_pull_request_changes` | default | List changed files in an iteration |
| `devops_create_pull_request` | write | Create a PR, optionally linking work items |
| `devops_update_pull_request` | write | Edit title, description, status, draft, target branch, or auto-complete |
| `devops_complete_pull_request` | write | Merge it — irreversible, so confirm strategy and branch deletion first |
| `devops_abandon_pull_request` | write | Abandon without merging |
| `devops_vote_pull_request` | write | Vote (10 approve, 5 with suggestions, 0 reset, -5 waiting, -10 reject) |
| `devops_tag_pull_request` | write | Add labels |
| `devops_link_work_items_to_pull_request` | write | Link work items |
| `devops_create_pull_request_thread` | write | Start a thread, general or inline on a file and line |
| `devops_set_pull_request_thread_status` | write | Set thread status (`active`, `fixed`, `wontFix`, …) |
| `devops_add_pull_request_comment` | write | Reply in a thread |
| `devops_update_pull_request_comment` | write | Edit a comment |

### Work items (15)

| Tool | Gate | Description |
|---|---|---|
| `devops_get_work_item` | default | Get one work item |
| `devops_list_work_items` | default | Fetch up to 200 by ID |
| `devops_query_work_items` | default | Run WIQL and hydrate the results |
| `devops_list_work_item_types` | default | List types (Bug, Task, Epic) and reference names |
| `devops_list_work_item_fields` | default | List field definitions |
| `devops_list_work_item_attachments` | default | List attachments from both surfaces — the Attachments tab *and* images pasted into fields |
| `devops_get_work_item_attachment` | default | Download an attachment and return it as viewable image content |
| `devops_create_work_item` | write | Create one (large-text fields save as markdown) |
| `devops_update_work_item` | write | Update fields (large-text fields save as markdown) |
| `devops_update_work_item_tags` | write | Add or remove tags without replacing the whole set |
| `devops_add_work_item_comment` | write | Comment on a work item |
| `devops_update_work_item_comment` | write | Edit a comment |
| `devops_upload_work_item_attachment` | write | Upload an image and get paste-ready embed snippets |
| `devops_link_work_item_attachment` | write | Put an uploaded attachment on the Attachments tab |
| `devops_delete_work_item` | delete | Recycle-bin it; `destroy=true` erases it for good |

### Saved queries (7)

Address a query by GUID or by path — `Shared Queries/Website team/All Bugs`. The roots are `Shared Queries` and `My Queries`; neither can be renamed or moved here, because Azure DevOps allows it and it breaks path addressing for everything below.

| Tool | Gate | Description |
|---|---|---|
| `devops_list_queries` | default | Browse both roots, or search by query name (`name_filter`) |
| `devops_get_query` | default | Get one query or folder with its WIQL, optionally with children |
| `devops_run_query` | default | Run a saved query and hydrate the results |
| `devops_create_query` | write | Save a new WIQL query; `validate_only=true` checks it without saving |
| `devops_create_query_folder` | write | Create a folder |
| `devops_update_query` | write | Rename, re-WIQL, move, or undelete. Last write wins — no ETag on this resource |
| `devops_delete_query` | delete | Soft-delete a query or folder (folders cascade) |

To recover a deleted query: read the *parent folder* with `devops_get_query(depth=1, include_deleted=true)`, take the child's GUID, then `devops_update_query(query=<guid>, undelete=true)`. A deleted item answers to its GUID only — `include_deleted` on its path still returns 404.

### Discovery (2)

| Tool | Gate | Description |
|---|---|---|
| `devops_list_projects` | default | List an org's projects |
| `devops_list_teams` | default | List a project's teams; `mine=true` narrows to yours |

### Advanced Security (3)

Needs GitHub Advanced Security for Azure DevOps enabled on the repo.

| Tool | Gate | Description |
|---|---|---|
| `devops_list_advanced_security_alerts` | default | List alerts by type, state, severity, rule, tool, or branch |
| `devops_get_advanced_security_alert` | default | Get one alert. `expand=validationFingerprint` can return secrets in cleartext |
| `devops_update_advanced_security_alert` | write | Dismiss, re-activate, or mark fixed |

### Service connections (2)

| Tool | Gate | Description |
|---|---|---|
| `devops_list_service_connections` | default | List connections, filtered by type, name, or auth scheme |
| `devops_get_service_connection` | default | Get one, with credential values redacted |

Health is three separate signals: `is_ready`, `is_disabled`, and `is_outdated` (stored config no longer matches the real resource — usually an expired secret). A ready connection can still fail to authenticate.

### Variable groups (2)

| Tool | Gate | Description |
|---|---|---|
| `devops_list_variable_groups` | default | List groups; values are omitted unless asked for |
| `devops_get_variable_group` | default | Get one group with redacted values |

---

## Notes

**API versions.** Pipelines, repos, discovery, work item schema, service connections, and variable groups use v7.1. PR tools, work item writes, and saved queries use v7.2-preview. Advanced Security uses v7.2-preview.1 on `advsec.dev.azure.com`. See the [Azure DevOps REST API docs](https://learn.microsoft.com/en-us/rest/api/azure/devops/).

**`run_id` and `build_id` are the same number.** A Pipelines run ID is the Build API's `buildId`, so IDs from one API work in the other.

**Reading logs cheaply.** Try `devops_get_run_timeline` first — it often answers "why did it fail" with no log fetch at all. Then `devops_search_run_log` to grep. Only then `devops_get_run_log_content`, which pages rather than dumping.

**Requires MCP Python SDK 2.0+.** Releases up to 1.3.0 need SDK 1.x and won't start on 2.x — the SDK dropped `mcp.server.fastmcp`. Upgrade to 1.4.0 or later.

---

## Development

```bash
uv sync                        # install
uv run pytest                  # tests (no network, no credentials)
uv run ruff check src tests    # lint
uv run python -m build         # build
```

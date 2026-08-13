# devops-mcp

[![PyPI](https://img.shields.io/pypi/v/devops-mcp)](https://pypi.org/project/devops-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/devops-mcp)](https://pypi.org/project/devops-mcp/)
[![License: MIT](https://img.shields.io/github/license/ryanmichaeljames/devops-mcp)](LICENSE)

An [MCP](https://modelcontextprotocol.io/) server that exposes Azure DevOps as tools for LLMs — pipelines, repositories, pull requests, and work items. Built with [MCPServer](https://github.com/modelcontextprotocol/python-sdk) (the Python MCP SDK's ergonomic server class) over stdio transport.

Communicates over **stdio** and works with GitHub Copilot, Claude Code, and any MCP-compatible client.

---

## Quick Start

**1. Install dependencies**

```bash
uv sync
```

**2. Configure** — add to your MCP client config (see [MCP Client Setup](#mcp-client-setup) below).

**3. Run the server**

```bash
uv run devops-mcp
```

---

## Installation

### Prerequisites

- Python `>=3.10`
- [uv](https://docs.astral.sh/uv/) (recommended)
- A Microsoft Entra ID identity with access to Azure DevOps

Requires the MCP Python SDK `>=2.0.0`. Versions up to and including 1.3.0 require SDK 1.x and will not start against 2.x — the SDK removed `mcp.server.fastmcp` in 2.0.0, so an older release installed today fails at import with `ModuleNotFoundError: No module named 'mcp.server.fastmcp'`. Upgrade to 1.4.0 or later.

### Install dependencies

```bash
uv sync
```

### Build the package

```bash
uv run python -m build
```

---

## Configuration

All configuration is driven by environment variables — no secrets in code, no hardcoded org names or tenant IDs.

| Variable | Required? | Default | Description |
|---|---|---|---|
| `AZDO_AUTH_TYPE` | No | `default` | Authentication method. One of: `default`, `azure_cli`, `interactive`, `client_secret`, `managed_identity`. `default` tries all credential sources in order (environment variables, Azure CLI, managed identity) and is the right choice for local development. See [Authentication](#authentication). |
| `AZDO_ORGANIZATION` | No | — | Default Azure DevOps organization name. Can be overridden per tool call. Required if not supplied per call. |
| `AZDO_PROJECT` | No | — | Default Azure DevOps project name. Can be overridden per tool call. Required if not supplied per call. |
| `AZDO_TENANT_ID` | Conditional | — | Microsoft Entra ID tenant ID. Required for `client_secret`. Recommended for `interactive` to constrain sign-in to the correct tenant. |
| `AZDO_CLIENT_ID` | `client_secret` only | — | Service principal client ID. |
| `AZDO_CLIENT_SECRET` | `client_secret` only | — | Service principal client secret. |
| `AZDO_LOG_LEVEL` | No | `INFO` | Log verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR`. All logs go to stderr; stdout is reserved for MCP stdio transport. |
| `AZDO_ALLOW_WRITE` | No | off | Set `true` to register create, update, tag, link, and comment (write) tools. When unset the server is read-only — write tools are not visible to the agent at all. |
| `AZDO_ALLOW_DELETE` | No | off | Set `true` to register delete tools. When unset, delete tools are not visible to the agent. |
| `AZDO_EPHEMERAL_TOKEN` | No | `false` | **Interactive auth only.** When `false` (the default), the MSAL token cache is persisted to disk via the OS secret store (Windows DPAPI, macOS Keychain, Linux libsecret), and an `AuthenticationRecord` sidecar is written to `~/.devops-mcp/auth-record.json` so subsequent server restarts authenticate silently without a new browser prompt. Set `true`, `1`, or `yes` to use an in-memory-only cache (no disk cache, no sidecar) — re-prompts on every restart. Invalid values fall back to `false` with a logged warning. Has no effect on any auth type other than `interactive`. |
| `AZDO_TOKEN_CACHE_PROFILE` | No | — | **Interactive auth only.** A filename-safe suffix (`[A-Za-z0-9_-]`) appended to the MSAL cache name and the `AuthenticationRecord` sidecar so two server instances signed in to **different tenants/accounts** on the same host keep separate caches instead of overwriting each other's pinned account. Omit (or leave empty) for a single-tenant setup — the original shared filenames are used. Characters outside `[A-Za-z0-9_-]` raise an error rather than being silently dropped (sanitizing could collapse two distinct profiles into one shared cache). |
| `AZDO_AUTH_TIMEOUT_SECONDS` | No | `30` | Maximum seconds to wait for credential acquisition before failing with an auth error. Applies to all auth types. Invalid or non-positive values fall back to `30`. Increase this in slow-network or MFA-heavy environments. |
| `AZDO_ATTACHMENT_ROOT` | No | — | **Upload hardening.** When set, `devops_upload_work_item_attachment` will only read a `file_path` that resolves inside this directory tree; anything else is refused. Unset (the default) means no directory restriction, matching the server's existing trust model — it runs as you, with your credentials. Set it when the server runs with broader credentials than the human at the keyboard. |

---

## Authentication

The server uses **Microsoft Entra ID (Azure AD) OAuth 2.0** via the [`azure-identity`](https://pypi.org/project/azure-identity/) library. Set `AZDO_AUTH_TYPE` to select a method.

| `AZDO_AUTH_TYPE` | Description | Best for |
|---|---|---|
| `default` *(default)* | `DefaultAzureCredential` — tries environment variables, Azure CLI session, managed identity, and other sources in order. Does not prompt in-process. | **Recommended — works everywhere** |
| `azure_cli` | Uses the active Azure CLI session (`az login`). Does not prompt in-process. | Local development with an existing CLI session |
| `interactive` | Opens a browser for interactive sign-in. Supports MFA and multi-account use. Benefits from the persistent token cache (on by default; disable with `AZDO_EPHEMERAL_TOKEN=true`): the first launch prompts; subsequent restarts reuse the cached refresh token silently while it remains valid. | Local development without a CLI session |
| `client_secret` | Service principal with client secret. Requires `AZDO_TENANT_ID`, `AZDO_CLIENT_ID`, and `AZDO_CLIENT_SECRET`. | CI/CD, unattended automation |
| `managed_identity` | Azure Managed Identity. No credentials to manage. | Azure-hosted workloads (VMs, Functions, Container Apps) |

**For `default` / local dev:** run `az login` once — `DefaultAzureCredential` will pick it up automatically.

**For `interactive`:** a browser window opens on first use. Set `AZDO_TENANT_ID` to constrain sign-in to a specific Entra ID tenant (recommended when multiple accounts are in use). The persistent token cache is on by default, so subsequent restarts are silent; set `AZDO_EPHEMERAL_TOKEN=true` to opt out.

**For `client_secret`:** also set `AZDO_TENANT_ID`, `AZDO_CLIENT_ID`, and `AZDO_CLIENT_SECRET`.

---

## Security

### Safe-by-default write and delete gates

Write and delete tools are **not registered by default** — they do not appear to the agent at all until explicitly enabled. The server is read-only until `AZDO_ALLOW_WRITE=true` and/or `AZDO_ALLOW_DELETE=true` are set. Each flag is independent; set only the ones you need.

Two tools sit behind the `delete` gate, and they are the only ones annotated `destructiveHint: true`.

`devops_delete_query` is the milder of the two: query deletion is soft with no permanent option, so a deleted query or folder can be restored with `devops_update_query(undelete=true)` — except its permissions, which do not come back. Deleting a folder cascades to everything inside it.

`devops_delete_work_item`'s default is the **recoverable** operation: the work item goes to the project's recycle bin and a human can restore it from Boards > Work Items > Recycle Bin. The unrecoverable operation is opt-in per call via `destroy=true`, which erases the work item and all its revisions with no undo — Azure DevOps guards that separately with the project-level *Permanently delete work items* permission, held by Project Administrators by default, so an ordinary contributor's token gets an HTTP 403 rather than a silent permanent delete.

### Env-driven configuration

All configuration is supplied via environment variables. No secrets, org names, project names, or tenant IDs are hardcoded. `.vscode/mcp.json` is gitignored because it may contain credentials.

### Attachment URLs are rebuilt, never followed

`devops_get_work_item_attachment` accepts a `url` so an agent can paste the `<img src>` it found in a work item description. That description is user-authored content and therefore a prompt-injection surface: fetching an arbitrary URL with an `Authorization: Bearer …` header attached would hand a live Azure DevOps token to whatever host an injected description names.

The guard is not a host allowlist — an open redirect on a Microsoft-owned host would defeat that. The URL is **parsed and then discarded**: the scheme must be `https`, the netloc must carry no userinfo and no non-443 port, the parsed hostname must be `dev.azure.com` or `{org}.visualstudio.com`, the path must be the `/_apis/wit/attachments/{guid}` route, and the organization named in the URL must match the resolved one. Only the attachment GUID and `fileName` survive; the request is rebuilt against the resolved organization. Every rejection happens **before** a token is acquired, so a crafted URL never causes one to be minted.

Two relative shapes are accepted alongside the absolute one, because the web UI can write either into an `<img src>`: protocol-relative `//dev.azure.com/{org}/…`, which gets the identical host, userinfo and port treatment with the scheme taken to be `https`, and site-relative `/{org}/_apis/wit/attachments/{guid}`, which has no host to check but still has to name the resolved organization in its first path segment. Neither weakens anything — the URL is discarded and the request rebuilt either way, so there is no host to follow — and a relative URL naming a different organization is refused and counted exactly like an absolute one. The alternative was worse: before, they matched nothing at all and were *silently* missed, so a caller saw a clean result rather than a rejection.

The same guard runs on every URL the other attachment tools handle, for two more reasons:

- `devops_list_work_item_attachments` scrapes URLs out of field and comment text — the most attacker-influenceable input in the product. A URL that fails validation is dropped and only *counted* (`rejected_urls`); it is never repeated back, because echoing it would relay to the model exactly the payload the guard just caught. Only canonical rebuilt URLs are ever surfaced.
- `devops_link_work_item_attachment` **stores** a URL on the work item. An unvalidated one there is a persistent injection vector re-read by every future reader, not a single bad request — so the supplied URL is parsed, discarded, and the relation is rebuilt from the resolved organization before anything is written.

### Attachment uploads are image-only, by bytes as well as by name

`devops_upload_work_item_attachment` reads a local file chosen by a model, which in the general case is a data-exfiltration primitive. Four guards apply: it is registered only under `AZDO_ALLOW_WRITE`; the file name must end in `.png`, `.jpg`, `.jpeg`, `.gif` or `.webp`; the **actual bytes** must carry a matching image signature, so renaming `id_rsa` to `x.png` is refused; and the path is resolved (collapsing `..`) and must be a regular file. Set `AZDO_ATTACHMENT_ROOT` to additionally confine reads to one directory tree. Weakening the extension allowlist or the magic-byte check — for example "to support PDFs" — is a security change, not a feature tweak.

### Stdout reserved for MCP transport

Stdout is exclusively reserved for MCP stdio transport messages. All server logs (including auth events) go to stderr via the Python `logging` module. Never redirect stdout to a log file.

### Token cache caveats (`interactive` auth)

By default the MSAL token cache is encrypted at rest using the OS secret store (Windows DPAPI, macOS Keychain, Linux libsecret). The `AuthenticationRecord` sidecar stored at `~/.devops-mcp/auth-record.json` contains only account metadata (home account ID, tenant, authority, username) — no tokens or client secrets.

On headless Linux without a secret store (e.g., no GNOME Keyring / libsecret installed), the OS-encrypted cache may be unavailable. The server logs an actionable warning and falls back to an in-memory-only cache. Set `AZDO_EPHEMERAL_TOKEN=true` to suppress the warning and always use in-memory cache on such hosts.

### Multiple tenants/accounts on one host (`interactive` auth)

The default cache and sidecar filenames (`devops-mcp.cache`, `~/.devops-mcp/auth-record.json`) are shared per host, so two `interactive` sessions signed in to **different tenants/accounts** would overwrite each other's pinned account. Give each session a distinct `AZDO_TOKEN_CACHE_PROFILE` (e.g. `prod`, `dev`) to keep their caches and `AuthenticationRecord` sidecars separate. The profile is a tenant-wide cache key: each entry signs in once (its own browser prompt) and then restarts silently as its own account, while tools still receive the specific `organization`/`project` per call. The profiles never collide.

Register two server entries, each with its own profile and (recommended) matching `AZDO_TENANT_ID`:

```json
{
  "servers": {
    "devops-mcp-prod": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "devops-mcp"],
      "env": {
        "AZDO_AUTH_TYPE": "interactive",
        "AZDO_TENANT_ID": "<prod-tenant-id>",
        "AZDO_TOKEN_CACHE_PROFILE": "prod"
      }
    },
    "devops-mcp-dev": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "devops-mcp"],
      "env": {
        "AZDO_AUTH_TYPE": "interactive",
        "AZDO_TENANT_ID": "<dev-tenant-id>",
        "AZDO_TOKEN_CACHE_PROFILE": "dev"
      }
    }
  }
}
```

### Resilience behavior

These behaviors are built in and require no configuration:

- **Automatic retries** — requests that receive `429` (throttling) or transient gateway errors (`502`, `503`, `504`) are retried automatically with back-off and `Retry-After` header honoring. **Non-idempotent writes (POST, PATCH) are not retried on `5xx`** — a gateway error on a write may arrive after the server has already committed the operation; only `429` (which guarantees the request was rejected before processing) is safe to retry on all methods.
- **Response size cap** — responses larger than **5 MB** are replaced with an error asking the agent to narrow the query. For large pipeline logs, prefer `devops_get_run_timeline` to triage without pulling log text at all; `devops_get_run_log_content` defaults to a bounded 500-line page (`max_lines`, `tail`, `start_line`/`end_line`) with a paging envelope, and `devops_search_run_log` returns only matching lines.
- **Auth timeout** — credential acquisition is bounded by `AZDO_AUTH_TIMEOUT_SECONDS` (default 30 s). A slow or hung auth call releases the per-scope lock so subsequent callers are not serialized indefinitely.

---

## MCP Client Setup

### GitHub Copilot (VS Code)

Add to `.vscode/mcp.json` in your project root. Note: `.vscode/mcp.json` is gitignored because it may contain secrets.

**Default / local dev (recommended):**

```json
{
  "servers": {
    "devops-mcp": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "devops-mcp"],
      "env": {
        "AZDO_ORGANIZATION": "<your-org>",
        "AZDO_PROJECT": "<your-project>"
      }
    }
  }
}
```

**With write tools enabled:**

```json
{
  "servers": {
    "devops-mcp": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "devops-mcp"],
      "env": {
        "AZDO_ORGANIZATION": "<your-org>",
        "AZDO_PROJECT": "<your-project>",
        "AZDO_ALLOW_WRITE": "true"
      }
    }
  }
}
```

**Service principal (CI/CD):**

```json
{
  "servers": {
    "devops-mcp": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "devops-mcp"],
      "env": {
        "AZDO_AUTH_TYPE": "client_secret",
        "AZDO_TENANT_ID": "<your-tenant-id>",
        "AZDO_CLIENT_ID": "<your-client-id>",
        "AZDO_CLIENT_SECRET": "<your-client-secret>",
        "AZDO_ORGANIZATION": "<your-org>",
        "AZDO_PROJECT": "<your-project>"
      }
    }
  }
}
```

---

## Tools

**66 tools** across 9 domains. Tools marked with a gate are only registered when the corresponding env flag is set.

| Gate | Meaning |
|---|---|
| `default` | Always registered (reads and safe queries). |
| `write` | Registered only when `AZDO_ALLOW_WRITE=true`. |
| `delete` | Registered only when `AZDO_ALLOW_DELETE=true`. |

### Pipelines (10 tools)

| Tool | Gate | Description |
|---|---|---|
| `devops_list_pipelines` | default | List pipelines defined in a project |
| `devops_list_pipeline_runs` | default | List runs for a specific pipeline |
| `devops_get_pipeline_run` | default | Get details of a specific pipeline run |
| `devops_get_build` | default | Get build details by `buildId` (resolves a build URL to pipeline info) |
| `devops_list_run_logs` | default | List log metadata for a build by `buildId` |
| `devops_get_run_timeline` | default | Compact, filtered build timeline with inline failure messages — the recommended first stop for "why did this build fail," often needing zero log fetches |
| `devops_get_run_log_content` | default | Get plain-text content of a specific log; bounded to `max_lines` (default 500) by default, with `start_line`/`end_line`/`tail` windowing and a paging envelope (`has_more`/`next_start_line`) |
| `devops_search_run_log` | default | Search (grep) a log in-process and return only matching lines plus context — non-matching lines never reach the model |
| `devops_list_build_artifacts` | default | List artifacts produced by a build |
| `devops_run_pipeline` | write | Trigger a new pipeline run; optionally override branch, template parameters, or queue-time variables |

### Repositories (8 tools)

| Tool | Gate | Description |
|---|---|---|
| `devops_list_repositories` | default | List Git repositories in a project |
| `devops_get_repository` | default | Get details of a specific repository |
| `devops_list_branches` | default | List branches in a repository |
| `devops_get_file_content` | default | Get the text content of a file; supports optional `branch` or `commit_id`; binary files return an error pointing at `devops_get_repository_image`. Sends `$format=octetStream`, without which the Git Items route returns the item's metadata JSON rather than the file |
| `devops_get_repository_image` | default | Read an image committed to a repository (diagram, chart, design asset) and return it as viewable image content. Same addressing as `devops_get_file_content`; a sibling rather than a change to it, so the text tool's contract is untouched |
| `devops_list_repository_items` | default | Browse files and folders; control depth with `recursion_level` (`oneLevel`, `full`, etc.) |
| `devops_list_commits` | default | List commits with optional filters for branch, author, and date range |
| `devops_get_commit` | default | Get details of a specific commit; set `change_count` to include changed file paths |

### Pull Requests (17 tools)

| Tool | Gate | Description |
|---|---|---|
| `devops_get_pull_request` | default | Get details of a specific pull request |
| `devops_list_pull_requests` | default | List pull requests with optional filters (status, branch, creator, reviewer, labels) |
| `devops_create_pull_request` | write | Create a new pull request, optionally linking work items |
| `devops_update_pull_request` | write | Update title, description, status, draft state, target branch, auto-complete, or completion options |
| `devops_complete_pull_request` | write | Complete (merge) a pull request — irreversible; confirm merge strategy and source-branch deletion first to avoid unwanted merge type or history loss |
| `devops_abandon_pull_request` | write | Abandon a pull request without merging |
| `devops_vote_pull_request` | write | Cast a reviewer vote (10 approve, 5 approve with suggestions, 0 reset, -5 waiting, -10 reject) |
| `devops_tag_pull_request` | write | Add labels/tags to a pull request |
| `devops_link_work_items_to_pull_request` | write | Link Azure Boards work items to a pull request |
| `devops_list_pull_request_threads` | default | List comment threads on a pull request |
| `devops_get_pull_request_thread` | default | Get a single comment thread with its comments |
| `devops_create_pull_request_thread` | write | Start a comment thread — general, or inline on a file/line via thread context |
| `devops_set_pull_request_thread_status` | write | Set a thread's status (`active`, `fixed`, `wontFix`, `closed`, `byDesign`, `pending`) |
| `devops_add_pull_request_comment` | write | Reply to an existing comment thread |
| `devops_update_pull_request_comment` | write | Edit the text of an existing comment |
| `devops_list_pull_request_iterations` | default | List a pull request's iterations (push history) |
| `devops_get_pull_request_changes` | default | List changed files for a PR iteration (path + change type) |

### Work Items (15 tools)

| Tool | Gate | Description |
|---|---|---|
| `devops_get_work_item` | default | Get a single work item by ID |
| `devops_list_work_items` | default | Bulk-fetch up to 200 work items by ID |
| `devops_query_work_items` | default | Query work items with WIQL, auto-fetching full details |
| `devops_create_work_item` | write | Create a new work item (large-text fields saved as markdown by default; `format=html` to opt out) |
| `devops_update_work_item` | write | Update fields on an existing work item (large-text fields saved as markdown by default; `format=html` to opt out) |
| `devops_update_work_item_tags` | write | Add and/or remove tags on an existing work item (case-insensitive matching; unlike `devops_update_work_item`'s `tags` field, this does not replace the whole tag set) |
| `devops_add_work_item_comment` | write | Add a comment to a work item (markdown by default; `format=html` to opt out) |
| `devops_update_work_item_comment` | write | Update an existing work item comment (markdown by default; `format=html` to opt out) |
| `devops_delete_work_item` | delete | Delete a work item. By default it goes to the project's **recycle bin** and can be restored; `destroy=true` permanently destroys it with no recycle bin and no way to recover it (and needs the elevated *Permanently delete work items* permission) |
| `devops_list_work_item_types` | default | List work item types (e.g., Bug, Task, Epic) and their reference names |
| `devops_list_work_item_fields` | default | List field definitions for a work item type or all fields in the process |
| `devops_list_work_item_attachments` | default | List every attachment a work item references, from **both** surfaces: `AttachedFile` relations (the Attachments tab) *and* images pasted inline into large-text fields. De-duplicated by attachment GUID, with the `sources` and field names each was found in. The **most recent** comment is always scanned, since `System.History` carries its text; `include_comments=true` costs one extra call and reaches *older* comments plus their comment ids |
| `devops_get_work_item_attachment` | default | Download an attachment and return it as viewable image content, from an `attachment_id` or an `<img src>` URL copied out of a description. Returns image blocks rather than JSON alone; non-image attachments are reported as metadata only |
| `devops_upload_work_item_attachment` | write | Upload an image (from `file_path` or `data_base64`) and return the attachment reference plus ready-to-paste `embed.markdown` / `embed.html` snippets. Images only — the extension allowlist *and* the file's magic bytes must both agree |
| `devops_link_work_item_attachment` | write | Add the `AttachedFile` relation that puts an uploaded attachment on the Attachments tab — the second step `devops_upload_work_item_attachment` deliberately does not perform. Idempotent (no PATCH when already linked) and guarded by a `/rev` test op |

### Saved Queries (7 tools)

Queries and folders are addressed either by GUID or by path — `Shared Queries/Website team/All Bugs`. The only two roots are `Shared Queries` (project-wide) and `My Queries` (yours alone); neither root can be renamed or moved through these tools, because Azure DevOps permits the rename and it breaks path addressing for everything beneath it.

| Tool | Gate | Description |
|---|---|---|
| `devops_list_queries` | default | Browse the hierarchy from both roots (`depth` up to 2), or set `name_filter` to search by name instead. Search matches **query names only** — folder names are not searched, so a filter naming a folder exactly returns nothing |
| `devops_get_query` | default | Get one query or folder by GUID or path, including its WIQL, and optionally its children (`depth`). `expand` is not monotonic: `wiql` returns the text *and* the author/date metadata, while `minimal` drops the metadata |
| `devops_run_query` | default | Run a saved query and return the work items, fields already hydrated in batches. Handles flat, `tree` and `oneHop` result shapes; `team` is required only for queries scoped to a team |
| `devops_create_query` | write | Save a new WIQL query under a folder. `validate_only=true` parses and validates the WIQL without creating anything |
| `devops_create_query_folder` | write | Create a folder in the query hierarchy |
| `devops_update_query` | write | Rename, re-WIQL, move, or undelete a query or folder. No optimistic concurrency exists on this resource — there is no `rev` and no ETag, so a concurrent edit is last-write-wins |
| `devops_delete_query` | delete | Delete a query or folder; deleting a folder cascades to its contents. The delete is **soft** with no `destroy` option — restore with `devops_update_query(undelete=true)`, though permissions set on the item are not restored |

A soft-deleted item is addressable **only by GUID**: `include_deleted=true` on its *path* still returns 404. To recover one, read its **parent folder** with `devops_get_query(depth=1, include_deleted=true)`, take the child's GUID from the listing, then `devops_update_query(query=<guid>, undelete=true)`.

### Discovery (2 tools)

| Tool | Gate | Description |
|---|---|---|
| `devops_list_projects` | default | List projects in an organization; use when project name is unknown |
| `devops_list_teams` | default | List teams in a project; supports `mine=true` to filter to the authenticated user's teams |

### Advanced Security (3 tools)

Requires GitHub Advanced Security for Azure DevOps (GHAzDo) to be enabled on the repository.

| Tool | Gate | Description |
|---|---|---|
| `devops_list_advanced_security_alerts` | default | List security alerts for a repository; filter by `alert_type` (`secret`, `dependency`, `code`), state, severity, rule, tool, or branch |
| `devops_get_advanced_security_alert` | default | Get a single alert by ID. `expand=validationFingerprint` can return secret values in cleartext — leave unset unless needed |
| `devops_update_advanced_security_alert` | write | Dismiss, re-activate, or mark an alert fixed; dismissing requires a dismissal reason |

### Service Connections (2 tools)

Read-only. Credential values are never returned — `authorization.parameters` is projected onto an explicit allowlist of non-secret identity fields (never a denylist, since parameter names are type-specific and open-ended), and the withheld field *names* (never values) are reported back so an agent can tell a credential exists without seeing it.

| Tool | Gate | Description |
|---|---|---|
| `devops_list_service_connections` | default | List service connections (service endpoints) in a project; filter by `type`, `names`, or `auth_schemes`. No server-side pagination — `top` is applied client-side |
| `devops_get_service_connection` | default | Get a single service connection by GUID, including redacted `auth_parameters` (allowlisted identity fields only) and `auth_parameters_dropped` (withheld field names) |

Both tools report health as three separate signals: `is_ready` (provisioning finished), `is_disabled` (turned off) and `is_outdated` (stored config no longer matches the underlying resource — typically an expired or rotated secret). A connection can be ready and still fail to authenticate, so `is_ready` alone is not a health check.

### Variable Groups (2 tools)

Read-only. Secret variables (`isSecret: true`) never have their value returned, regardless of what the server sends back. Non-secret variables whose name matches a credential-like pattern (e.g. `DB_PASSWORD`) are withheld too and flagged `redacted: "name_heuristic"` — a safety net for values the author forgot to mark secret.

| Tool | Gate | Description |
|---|---|---|
| `devops_list_variable_groups` | default | List variable groups in a project; variable values are omitted by default (`include_values=False`) so discovery calls cannot leak a value at all |
| `devops_get_variable_group` | default | Get a single variable group by ID, including (redacted) values by default; set `include_values=False` for names/flags only |

---

## API Reference

All tools use the [Azure DevOps REST API](https://learn.microsoft.com/en-us/rest/api/azure/devops/). Pipeline, repository, and discovery tools use **v7.1**. Work item schema tools (`devops_list_work_item_types`, `devops_list_work_item_fields`) use **v7.1**. Service connection and variable group tools use **v7.1** (GA — no preview moniker needed). PR tools (get/list/create/update/tag/link), work item write operations, and saved-query tools use **v7.2-preview**. Advanced Security alert tools use **v7.2-preview.1** on the `advsec.dev.azure.com` host.

**Note:** `run_id` and `build_id` share the same numeric value — a Pipelines API `run_id` is identical to the Build API `buildId` for the same run. This enables cross-API calls (e.g., use `devops_list_run_logs` to get log IDs, then `devops_get_run_log_content` with the same `build_id`).

### Efficient log retrieval

Pipeline logs can be huge and expensive to read. Work from cheapest to most expensive:

1. **`devops_get_run_timeline`** — start here for "why did it fail." Returns a compact, failure-filtered timeline with inline error messages, often answering the question with **zero log fetches**.
2. **`devops_search_run_log`** — grep a specific log in-process; only matching lines (plus context) are returned, so non-matching content never reaches the model.
3. **`devops_get_run_log_content`** — read log text directly. Bounded to `max_lines` (default 500) with `start_line`/`end_line`/`tail` windowing and a paging envelope (`has_more`, `next_start_line`) to page through large logs or grab just the `tail`.

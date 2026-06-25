# devops-mcp

[![PyPI](https://img.shields.io/pypi/v/devops-mcp)](https://pypi.org/project/devops-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/devops-mcp)](https://pypi.org/project/devops-mcp/)
[![License: MIT](https://img.shields.io/github/license/ryanmichaeljames/devops-mcp)](LICENSE)

An [MCP](https://modelcontextprotocol.io/) server that exposes Azure DevOps as tools for LLMs — pipelines, repositories, pull requests, and work items. Built with [FastMCP](https://github.com/modelcontextprotocol/python-sdk) over stdio transport.

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

### Env-driven configuration

All configuration is supplied via environment variables. No secrets, org names, project names, or tenant IDs are hardcoded. `.vscode/mcp.json` is gitignored because it may contain credentials.

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
- **Response size cap** — responses larger than **5 MB** are replaced with an error asking the agent to narrow the query. For large pipeline logs use `devops_get_run_log_content`'s `start_line`/`end_line` parameters to slice the content at the API level.
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

**31 tools** across 4 domains. Tools marked with a gate are only registered when the corresponding env flag is set.

| Gate | Meaning |
|---|---|
| `default` | Always registered (reads and safe queries). |
| `write` | Registered only when `AZDO_ALLOW_WRITE=true`. |
| `delete` | Registered only when `AZDO_ALLOW_DELETE=true`. |

### Pipelines (7 tools)

| Tool | Gate | Description |
|---|---|---|
| `devops_list_pipelines` | default | List pipelines defined in a project |
| `devops_list_pipeline_runs` | default | List runs for a specific pipeline |
| `devops_get_pipeline_run` | default | Get details of a specific pipeline run |
| `devops_get_build` | default | Get build details by `buildId` (resolves a build URL to pipeline info) |
| `devops_list_run_logs` | default | List log metadata for a build by `buildId` |
| `devops_get_run_log_content` | default | Get plain-text content of a specific log; use `start_line`/`end_line` to slice large logs |
| `devops_list_build_artifacts` | default | List artifacts produced by a build |

### Repositories (3 tools)

| Tool | Gate | Description |
|---|---|---|
| `devops_list_repositories` | default | List Git repositories in a project |
| `devops_get_repository` | default | Get details of a specific repository |
| `devops_list_branches` | default | List branches in a repository |

### Pull Requests (14 tools)

| Tool | Gate | Description |
|---|---|---|
| `devops_get_pull_request` | default | Get details of a specific pull request |
| `devops_list_pull_requests` | default | List pull requests with optional filters (status, branch, creator, reviewer, labels) |
| `devops_create_pull_request` | write | Create a new pull request, optionally linking work items |
| `devops_update_pull_request` | write | Update title, description, status, draft state, target branch, auto-complete, or completion options |
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

### Work Items (7 tools)

| Tool | Gate | Description |
|---|---|---|
| `devops_get_work_item` | default | Get a single work item by ID |
| `devops_list_work_items` | default | Bulk-fetch up to 200 work items by ID |
| `devops_query_work_items` | default | Query work items with WIQL, auto-fetching full details |
| `devops_create_work_item` | write | Create a new work item |
| `devops_update_work_item` | write | Update fields on an existing work item |
| `devops_add_work_item_comment` | write | Add a comment to a work item |
| `devops_update_work_item_comment` | write | Update an existing work item comment |

---

## API Reference

All tools use the [Azure DevOps REST API](https://learn.microsoft.com/en-us/rest/api/azure/devops/). Pipeline, repository, work item read tools, and the PR comment-thread and diff tools use **v7.1**. The remaining pull request tools (get/list/create/update/tag/link) and work item write operations use **v7.2-preview**.

**Note:** `run_id` and `build_id` share the same numeric value — a Pipelines API `run_id` is identical to the Build API `buildId` for the same run. This enables cross-API calls (e.g., use `devops_list_run_logs` to get log IDs, then `devops_get_run_log_content` with the same `build_id`).

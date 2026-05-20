# devops-mcp

An MCP server for interacting with Azure DevOps — pipelines, repositories, and work items.

## Tools

### Pipelines
| Tool | Description |
|---|---|
| `devops_list_pipelines` | List pipelines defined in a project |
| `devops_list_pipeline_runs` | List runs for a specific pipeline |
| `devops_get_pipeline_run` | Get details of a specific pipeline run |
| `devops_get_build` | Get build details by `buildId` (resolves a build URL to pipeline info) |
| `devops_list_run_logs` | List log metadata for a build by `buildId` |
| `devops_get_run_log_content` | Get plain-text content of a specific log |
| `devops_list_build_artifacts` | List artifacts produced by a build |

### Repositories
| Tool | Description |
|---|---|
| `devops_list_repositories` | List Git repositories in a project |
| `devops_get_repository` | Get details of a specific repository |
| `devops_list_branches` | List branches in a repository |

### Work Items
| Tool | Description |
|---|---|
| `devops_get_work_item` | Get a single work item by ID |
| `devops_list_work_items` | Bulk-fetch up to 200 work items by ID |
| `devops_query_work_items` | Query work items with WIQL, auto-fetching full details |

## Setup

### Prerequisites

- Python `>=3.10`
- [uv](https://docs.astral.sh/uv/) (recommended)
- A Microsoft Entra ID identity with access to Azure DevOps (see [Authentication](#authentication) below)

### Install

```bash
uv sync
```

### Configuration

Configure the server via environment variables:

| Variable | Required | Description |
|---|---|---|
| `AZDO_AUTH_TYPE` | No | Credential type (default: `default`). See [Authentication](#authentication) |
| `AZDO_TENANT_ID` | interactive / client_secret | Entra ID tenant ID. Required for `client_secret`; recommended for `interactive` to constrain sign-in to the correct tenant |
| `AZDO_CLIENT_ID` | client_secret only | Service principal client ID |
| `AZDO_CLIENT_SECRET` | client_secret only | Service principal client secret |
| `AZDO_ORGANIZATION` | No | Default organization name (can be supplied per-tool call) |
| `AZDO_PROJECT` | No | Default project name (can be supplied per-tool call) |
| `AZDO_LOG_LEVEL` | No | Log verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR` (default: `DEBUG`) |

### Authentication

This server uses **Microsoft Entra ID (Azure AD) OAuth 2.0** via the [`azure-identity`](https://pypi.org/project/azure-identity/) library. Set `AZDO_AUTH_TYPE` to one of:

| `AZDO_AUTH_TYPE` | Description | Best for |
|---|---|---|
| `default` *(default)* | `DefaultAzureCredential` — tries all methods in order | **Recommended — works everywhere** |
| `azure_cli` | Uses the signed-in Azure CLI session (`az login`) | Local development |
| `interactive` | Opens a browser for interactive sign-in | Local development |
| `client_secret` | Service principal with client secret | CI/CD, unattended automation |
| `managed_identity` | Azure Managed Identity | Azure-hosted workloads (VMs, Functions, Container Apps) |

**For local dev with `default`:** run `az login` once — `DefaultAzureCredential` will pick it up automatically.

**For `interactive`:** a browser window will open on first use. Set `AZDO_TENANT_ID` to constrain sign-in to a specific Entra ID tenant (recommended when multiple accounts are in use).

**For `client_secret`:** also set `AZDO_TENANT_ID`, `AZDO_CLIENT_ID`, and `AZDO_CLIENT_SECRET`.

### VS Code MCP Configuration

Add to your `.vscode/mcp.json` (or copy from `.vscode/mcp.json.example`):

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

### Run directly

```bash
uv run devops-mcp
```

## API Reference

All tools use the [Azure DevOps REST API v7.1](https://learn.microsoft.com/en-us/rest/api/azure/devops/?view=azure-devops-rest-7.1).

**Note:** `run_id` and `build_id` share the same numeric value — a Pipelines API `run_id` is identical to the Build API `buildId` for the same run. This enables cross-API calls (e.g., use `devops_list_run_logs` to get log IDs, then `devops_get_run_log_content` with the same `build_id`).


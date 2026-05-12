# devops-mcp

An MCP server for interacting with Azure DevOps — pipelines, repositories, and work items.

## Tools

### Pipelines
| Tool | Description |
|---|---|
| `devops_list_pipelines` | List pipelines defined in a project |
| `devops_list_pipeline_runs` | List runs for a specific pipeline |
| `devops_get_pipeline_run` | Get details of a specific pipeline run |
| `devops_list_run_logs` | List log metadata (IDs, line counts) for a run |
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
- An Azure DevOps [Personal Access Token (PAT)](https://learn.microsoft.com/en-us/azure/devops/organizations/accounts/use-personal-access-tokens-to-authenticate) with the following scopes:
  - **Build (Read)** — for pipelines, runs, logs, artifacts
  - **Code (Read)** — for repositories and branches
  - **Work Items (Read)** — for work items

### Install

```bash
uv sync
```

### Configuration

Configure the server via environment variables:

| Variable | Required | Description |
|---|---|---|
| `AZDO_PAT` | **Yes** | Azure DevOps Personal Access Token |
| `AZDO_ORGANIZATION` | No | Default organization name (can be supplied per-tool call) |
| `AZDO_PROJECT` | No | Default project name (can be supplied per-tool call) |

### VS Code MCP Configuration

Add to your `.vscode/mcp.json` (or copy from `.vscode/mcp.json.example`):

```json
{
  "servers": {
    "devops-mcp": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "devops-mcp"],
      "env": {
        "AZDO_PAT": "<your-pat>",
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


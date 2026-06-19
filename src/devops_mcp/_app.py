"""FastMCP application instance.

This module exists to avoid circular imports between server.py and tool
modules. Tool modules import ``mcp`` from here; server.py imports ``mcp``
from here and registers tool modules.
"""

import os

from mcp.server.fastmcp import FastMCP

from devops_mcp.client import devops_lifespan

mcp = FastMCP(
    "devops_mcp",
    instructions=(
        "Azure DevOps MCP server. Use devops_list_pipelines to discover pipelines, "
        "devops_list_pipeline_runs to see recent runs, devops_list_run_logs + "
        "devops_get_run_log_content to read build logs, devops_list_build_artifacts "
        "to inspect artifacts, devops_list_repositories and devops_list_branches for "
        "source control, and devops_query_work_items or devops_get_work_item for "
        "work item tracking."
    ),
    lifespan=devops_lifespan,
)

_ALLOW_WRITE = os.environ.get("AZDO_ALLOW_WRITE", "").lower() == "true"
_ALLOW_DELETE = os.environ.get("AZDO_ALLOW_DELETE", "").lower() == "true"


def write_tool(**kwargs):
    if _ALLOW_WRITE:
        return mcp.tool(**kwargs)
    return lambda f: f


def delete_tool(**kwargs):
    if _ALLOW_DELETE:
        return mcp.tool(**kwargs)
    return lambda f: f

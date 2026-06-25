"""Smoke test: importing devops_mcp.server registers all tools in the FastMCP registry."""

import devops_mcp.server  # noqa: F401 — side-effect: registers all @mcp.tool() decorators
from devops_mcp._app import mcp


async def test_tool_registry_is_non_empty():
    """Verify that importing the server module registers at least one tool."""
    tools = await mcp.list_tools()
    assert len(tools) > 0, "No tools registered — server import did not trigger @mcp.tool() decorators"

"""FastMCP server for Azure DevOps MCP tools."""

import logging
import sys

# Configure logging to stderr (stdout reserved for stdio transport)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr,
)

from devops_mcp._app import mcp  # noqa: E402

# Import tool modules to trigger @mcp.tool() registration
import devops_mcp.tools.pipelines  # noqa: E402, F401
import devops_mcp.tools.repositories  # noqa: E402, F401
import devops_mcp.tools.work_items  # noqa: E402, F401


def main():
    """Entry point for the Azure DevOps MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()

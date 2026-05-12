"""Repository tools for Azure DevOps MCP."""

import asyncio
import json
import logging

from mcp.server.fastmcp import Context

from devops_mcp._app import mcp
from devops_mcp.client import (
    AppContext,
    build_params,
    build_url,
    get_http_client,
    resolve_org,
    resolve_project,
)
from devops_mcp.models import GetRepositoryInput, ListBranchesInput, ListRepositoriesInput

logger = logging.getLogger(__name__)


@mcp.tool(
    name="devops_list_repositories",
    annotations={
        "title": "List Repositories",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def devops_list_repositories(params: ListRepositoriesInput, ctx: Context) -> str:
    """List Git repositories in an Azure DevOps project.

    Returns repository IDs, names, default branches, HTTPS and SSH clone URLs,
    web URLs, sizes, and project details. Use the repository ID or name with
    devops_get_repository or devops_list_branches.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        url = build_url(organization, project, "git/repositories")
        query_params = build_params(
            includeLinks="true" if params.include_links else None,
            includeAllUrls="true" if params.include_all_urls else None,
            includeHidden="true" if params.include_hidden else None,
        )

        def _query():
            with get_http_client(app_ctx.credential) as client:
                response = client.get(url, params=query_params)
                response.raise_for_status()
                return response.json()

        data = await asyncio.to_thread(_query)
        repos = data.get("value", [])
        return json.dumps({
            "repositories": repos,
            "count": data.get("count", len(repos)),
        })

    except ValueError as e:
        return json.dumps({"error": True, "message": str(e)})
    except Exception as e:
        return json.dumps({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


@mcp.tool(
    name="devops_get_repository",
    annotations={
        "title": "Get Repository",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def devops_get_repository(params: GetRepositoryInput, ctx: Context) -> str:
    """Get details of a specific Azure DevOps Git repository.

    Returns full repository metadata including ID, name, default branch,
    remote URL, SSH URL, web URL, size in bytes, fork status, maintenance
    status, and project information.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        url = build_url(organization, project, f"git/repositories/{params.repository_id}")

        def _query():
            with get_http_client(app_ctx.credential) as client:
                response = client.get(url, params=build_params())
                response.raise_for_status()
                return response.json()

        repo = await asyncio.to_thread(_query)
        return json.dumps(repo)

    except ValueError as e:
        return json.dumps({"error": True, "message": str(e)})
    except Exception as e:
        return json.dumps({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


@mcp.tool(
    name="devops_list_branches",
    annotations={
        "title": "List Branches",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def devops_list_branches(params: ListBranchesInput, ctx: Context) -> str:
    """List branches in an Azure DevOps Git repository.

    Returns branch names (in full ref format, e.g., refs/heads/main), commit
    SHAs, and creator information. Use filter_contains to narrow results to
    branches whose names contain a given substring.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        url = build_url(
            organization, project,
            f"git/repositories/{params.repository_id}/refs",
        )
        query_params = build_params(
            filter="heads/",
            filterContains=params.filter_contains,
            **{"$top": params.top},
        )

        def _query():
            with get_http_client(app_ctx.credential) as client:
                response = client.get(url, params=query_params)
                response.raise_for_status()
                data = response.json()
                continuation = response.headers.get("x-ms-continuationtoken")
                return data, continuation

        data, continuation_token = await asyncio.to_thread(_query)
        branches = data.get("value", [])
        result = {"branches": branches, "count": data.get("count", len(branches))}
        if continuation_token:
            result["continuation_token"] = continuation_token
        return json.dumps(result)

    except ValueError as e:
        return json.dumps({"error": True, "message": str(e)})
    except Exception as e:
        return json.dumps({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})

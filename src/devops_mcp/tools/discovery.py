"""Discovery tools for Azure DevOps MCP (organization-level)."""

import logging

import httpx
from mcp.server.fastmcp import Context

from devops_mcp._app import mcp
from devops_mcp.client import (
    AppContext,
    build_headers,
    build_org_url,
    build_params,
    extract_error_message,
    finalize_response,
    paginate_results,
    request_with_retry,
    resolve_org,
    resolve_project,
)
from devops_mcp.models import ListProjectsInput, ListTeamsInput

logger = logging.getLogger(__name__)


@mcp.tool(
    name="devops_list_projects",
    annotations={
        "title": "List Projects",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def devops_list_projects(params: ListProjectsInput, ctx: Context) -> str:
    """List projects in an Azure DevOps organization.

    Returns project IDs, names, descriptions, state, and visibility. Use the
    project name or ID with other tools that accept an organization and project.
    Supports pagination via continuation_token for organizations with many projects.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    try:
        organization = resolve_org(app_ctx, params.organization)
        url = build_org_url(organization, "projects")

        base_params = build_params(
            stateFilter=params.state_filter,
            **{"$top": params.top},
        )

        headers = await build_headers(app_ctx)
        projects, has_more = await paginate_results(
            app_ctx.http_client,
            url,
            headers,
            base_params,
            record_key="value",
            top=params.top,
            initial_continuation_token=params.continuation_token,
        )

        return finalize_response({
            "projects": projects,
            "count": len(projects),
            "has_more": has_more,
        })

    except ValueError as e:
        return finalize_response({"error": True, "message": str(e)})
    except httpx.HTTPStatusError as e:
        msg = extract_error_message(e.response)
        logger.error("Azure DevOps HTTP %d: %s", e.response.status_code, msg)
        return finalize_response({"error": True, "message": f"Azure DevOps returned HTTP {e.response.status_code}: {msg}"})
    except Exception as e:
        logger.exception("Unexpected error in devops_list_projects")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


@mcp.tool(
    name="devops_list_teams",
    annotations={
        "title": "List Teams",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def devops_list_teams(params: ListTeamsInput, ctx: Context) -> str:
    """List teams in an Azure DevOps project.

    Returns team IDs, names, descriptions, and project references. Use
    mine=true to return only teams the authenticated user belongs to.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        url = build_org_url(organization, f"projects/{project}/teams")

        query_params = build_params(
            **{"$mine": "true" if params.mine else None},
            **{"$top": params.top},
            **{"$skip": params.skip},
        )

        response = await request_with_retry(
            app_ctx.http_client,
            "GET",
            url,
            headers=await build_headers(app_ctx),
            params=query_params,
        )
        response.raise_for_status()
        data = response.json()
        teams = data.get("value", [])
        return finalize_response({"teams": teams, "count": len(teams)})

    except ValueError as e:
        return finalize_response({"error": True, "message": str(e)})
    except httpx.HTTPStatusError as e:
        msg = extract_error_message(e.response)
        logger.error("Azure DevOps HTTP %d: %s", e.response.status_code, msg)
        return finalize_response({"error": True, "message": f"Azure DevOps returned HTTP {e.response.status_code}: {msg}"})
    except Exception as e:
        logger.exception("Unexpected error in devops_list_teams")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})

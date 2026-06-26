"""Repository tools for Azure DevOps MCP."""

import logging

import httpx
from mcp.server.fastmcp import Context

from devops_mcp._app import mcp
from devops_mcp.client import (
    AppContext,
    build_headers,
    build_params,
    build_url,
    extract_error_message,
    finalize_response,
    paginate_results,
    request_with_retry,
    resolve_org,
    resolve_project,
)
from devops_mcp.models import GetFileContentInput, GetRepositoryInput, ListBranchesInput, ListRepositoriesInput

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

        response = await request_with_retry(
            app_ctx.http_client,
            "GET",
            url,
            headers=await build_headers(app_ctx),
            params=query_params,
        )
        response.raise_for_status()
        data = response.json()
        repos = data.get("value", [])
        return finalize_response({
            "repositories": repos,
            "count": data.get("count", len(repos)),
        })

    except ValueError as e:
        return finalize_response({"error": True, "message": str(e)})
    except httpx.HTTPStatusError as e:
        msg = extract_error_message(e.response)
        logger.error("Azure DevOps HTTP %d: %s", e.response.status_code, msg)
        return finalize_response({"error": True, "message": f"Azure DevOps returned HTTP {e.response.status_code}: {msg}"})
    except Exception as e:
        logger.exception("Unexpected error in devops_list_repositories")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


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

        response = await request_with_retry(
            app_ctx.http_client,
            "GET",
            url,
            headers=await build_headers(app_ctx),
            params=build_params(),
        )
        response.raise_for_status()
        return finalize_response(response.json())

    except ValueError as e:
        return finalize_response({"error": True, "message": str(e)})
    except httpx.HTTPStatusError as e:
        msg = extract_error_message(e.response)
        logger.error("Azure DevOps HTTP %d: %s", e.response.status_code, msg)
        return finalize_response({"error": True, "message": f"Azure DevOps returned HTTP {e.response.status_code}: {msg}"})
    except Exception as e:
        logger.exception("Unexpected error in devops_get_repository")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


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

        effective_top = params.top if params.top is not None else 100
        base_params = build_params(
            filter="heads/",
            filterContains=params.filter_contains,
            **{"$top": effective_top},
        )

        headers = await build_headers(app_ctx)
        branches, has_more = await paginate_results(
            app_ctx.http_client,
            url,
            headers,
            base_params,
            record_key="value",
            top=effective_top,
        )

        result: dict = {
            "branches": branches,
            "count": len(branches),
            "has_more": has_more,
        }
        return finalize_response(result)

    except ValueError as e:
        return finalize_response({"error": True, "message": str(e)})
    except httpx.HTTPStatusError as e:
        msg = extract_error_message(e.response)
        logger.error("Azure DevOps HTTP %d: %s", e.response.status_code, msg)
        return finalize_response({"error": True, "message": f"Azure DevOps returned HTTP {e.response.status_code}: {msg}"})
    except Exception as e:
        logger.exception("Unexpected error in devops_list_branches")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


@mcp.tool(
    name="devops_get_file_content",
    annotations={
        "title": "Get File Content",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def devops_get_file_content(params: GetFileContentInput, ctx: Context) -> str:
    """Retrieve the text content of a file from an Azure DevOps Git repository.

    Returns the raw text content of the specified file as a JSON object with
    path, content, and optional branch/commit_id fields. Binary files return
    an error. Use branch or commit_id to read a specific version; omit both
    to use the repository's default branch.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        url = build_url(
            organization, project,
            f"git/repositories/{params.repository_id}/items",
        )

        query_params = build_params(path=params.path)
        if params.commit_id is not None:
            query_params["versionDescriptor.version"] = params.commit_id
            query_params["versionDescriptor.versionType"] = "commit"
        elif params.branch is not None:
            query_params["versionDescriptor.version"] = params.branch
            query_params["versionDescriptor.versionType"] = "branch"

        response = await request_with_retry(
            app_ctx.http_client,
            "GET",
            url,
            headers=await build_headers(app_ctx),
            params=query_params,
        )
        response.raise_for_status()

        content_type = response.headers.get("content-type", "").lower()
        if "application/octet-stream" in content_type or content_type.startswith("image/"):
            return finalize_response({
                "error": True,
                "message": (
                    f"File '{params.path}' is binary (content-type: {content_type}). "
                    "Binary file content is not supported."
                ),
            })

        try:
            text = response.text
        except Exception:
            return finalize_response({
                "error": True,
                "message": f"File '{params.path}' could not be decoded as text.",
            })

        result: dict = {"path": params.path, "content": text}
        if params.commit_id is not None:
            result["commit_id"] = params.commit_id
        elif params.branch is not None:
            result["branch"] = params.branch

        return finalize_response(result)

    except ValueError as e:
        return finalize_response({"error": True, "message": str(e)})
    except httpx.HTTPStatusError as e:
        msg = extract_error_message(e.response)
        logger.error("Azure DevOps HTTP %d: %s", e.response.status_code, msg)
        return finalize_response({"error": True, "message": f"Azure DevOps returned HTTP {e.response.status_code}: {msg}"})
    except Exception as e:
        logger.exception("Unexpected error in devops_get_file_content")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})

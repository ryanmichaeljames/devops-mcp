"""Pull request tools for Azure DevOps MCP."""

import json
import logging
from urllib.parse import quote

import httpx
from mcp.server.fastmcp import Context

from devops_mcp._app import mcp, write_tool
from devops_mcp.client import (
    AppContext,
    build_headers,
    build_url,
    extract_error_message,
    finalize_response,
    request_with_retry,
    resolve_org,
    resolve_project,
)
from devops_mcp.models import (
    CreatePullRequestInput,
    GetPullRequestInput,
    LinkWorkItemsToPullRequestInput,
    ListPullRequestsInput,
    TagPullRequestInput,
    UpdatePullRequestInput,
)

logger = logging.getLogger(__name__)

_PR_API_VERSION = "7.2-preview.2"
_WIT_API_VERSION = "7.2-preview.3"


def _build_pull_request_artifact_uri(
    project_id: str, repository_id: str, pull_request_id: int
) -> str:
    """Build the Azure DevOps artifact URI for a pull request work item link."""
    artifact_key = quote(f"{project_id}/{repository_id}/{pull_request_id}", safe="")
    return f"vstfs:///Git/PullRequestId/{artifact_key}"


@mcp.tool(
    name="devops_get_pull_request",
    annotations={
        "title": "Get Pull Request",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def devops_get_pull_request(params: GetPullRequestInput, ctx: Context) -> str:
    """Get details of a specific Azure DevOps pull request.

    Returns the full pull request object including ID, title, description,
    status, source and target branches, created-by identity, reviewers,
    merge status, completion options, and optional commits and work item refs.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        url = build_url(
            organization,
            project,
            f"git/repositories/{params.repository_id}/pullrequests/{params.pull_request_id}",
        )
        query_params: dict = {"api-version": _PR_API_VERSION}
        if params.include_commits:
            query_params["includeCommits"] = "true"
        if params.include_work_item_refs:
            query_params["includeWorkItemRefs"] = "true"

        response = await request_with_retry(
            app_ctx.http_client,
            "GET",
            url,
            headers=await build_headers(app_ctx),
            params=query_params,
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
        logger.exception("Unexpected error in devops_get_pull_request")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


@mcp.tool(
    name="devops_list_pull_requests",
    annotations={
        "title": "List Pull Requests",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def devops_list_pull_requests(params: ListPullRequestsInput, ctx: Context) -> str:
    """List pull requests in an Azure DevOps Git repository.

    Returns a list of pull requests matching the given filters. By default
    returns active pull requests. Supports filtering by status, source/target
    branch, creator, reviewer, labels, and title substring. Use skip and top
    for pagination.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        url = build_url(
            organization,
            project,
            f"git/repositories/{params.repository_id}/pullrequests",
        )

        # Build scalar search criteria params
        query_params: list[tuple[str, str]] = [("api-version", _PR_API_VERSION)]
        if params.status is not None:
            query_params.append(("searchCriteria.status", params.status))
        if params.source_ref_name is not None:
            query_params.append(("searchCriteria.sourceRefName", params.source_ref_name))
        if params.target_ref_name is not None:
            query_params.append(("searchCriteria.targetRefName", params.target_ref_name))
        if params.creator_id is not None:
            query_params.append(("searchCriteria.creatorId", params.creator_id))
        if params.reviewer_id is not None:
            query_params.append(("searchCriteria.reviewerId", params.reviewer_id))
        if params.title is not None:
            query_params.append(("searchCriteria.title", params.title))
        if params.top is not None:
            query_params.append(("$top", str(params.top)))
        if params.skip is not None:
            query_params.append(("$skip", str(params.skip)))
        # Labels require repeated query parameters
        if params.labels:
            for label in params.labels:
                query_params.append(("searchCriteria.labels", label))

        response = await request_with_retry(
            app_ctx.http_client,
            "GET",
            url,
            headers=await build_headers(app_ctx),
            params=query_params,
        )
        response.raise_for_status()
        data = response.json()
        pull_requests = data.get("value", [])
        return finalize_response({
            "pullRequests": pull_requests,
            "count": data.get("count", len(pull_requests)),
            "has_more": params.top is not None and len(pull_requests) >= params.top,
        })

    except ValueError as e:
        return finalize_response({"error": True, "message": str(e)})
    except httpx.HTTPStatusError as e:
        msg = extract_error_message(e.response)
        logger.error("Azure DevOps HTTP %d: %s", e.response.status_code, msg)
        return finalize_response({"error": True, "message": f"Azure DevOps returned HTTP {e.response.status_code}: {msg}"})
    except Exception as e:
        logger.exception("Unexpected error in devops_list_pull_requests")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


async def _link_work_items(
    app_ctx: AppContext,
    organization: str,
    project: str,
    repository_id: str,
    pull_request_id: int,
    work_item_ids: list[int],
    read_headers: dict,
) -> None:
    """Link work items to a pull request via ArtifactLink relations on the work item side.

    Fetches repository details to obtain the project ID needed to build the
    artifact URI, then for each work item GETs its current relations and
    PATCHes an ArtifactLink add operation only when the link is not already
    present. Uses the work-item JSON-Patch API (_WIT_API_VERSION).

    Raises httpx.HTTPStatusError on any non-2xx response; callers are
    responsible for catching it within their ordered error handlers.
    """
    repo_url = build_url(organization, project, f"git/repositories/{repository_id}")
    repo_response = await request_with_retry(
        app_ctx.http_client,
        "GET",
        repo_url,
        headers=read_headers,
        params={"api-version": "7.1"},
    )
    repo_response.raise_for_status()
    repository = repo_response.json()

    resolved_repository_id = repository["id"]
    project_id = repository["project"]["id"]
    artifact_uri = _build_pull_request_artifact_uri(project_id, resolved_repository_id, pull_request_id)

    patch_headers = await build_headers(
        app_ctx,
        extra={"Content-Type": "application/json-patch+json"},
    )

    for work_item_id in work_item_ids:
        work_item_url = build_url(organization, project, f"wit/workitems/{work_item_id}")
        work_item_response = await request_with_retry(
            app_ctx.http_client,
            "GET",
            work_item_url,
            headers=read_headers,
            params={"api-version": _WIT_API_VERSION, "$expand": "relations"},
        )
        work_item_response.raise_for_status()
        work_item = work_item_response.json()

        relations = work_item.get("relations", [])
        already_linked = any(
            relation.get("rel") == "ArtifactLink" and relation.get("url") == artifact_uri
            for relation in relations
        )
        if already_linked:
            continue

        patch_ops = [
            {
                "op": "add",
                "path": "/relations/-",
                "value": {
                    "rel": "ArtifactLink",
                    "url": artifact_uri,
                    "attributes": {"name": "Pull Request"},
                },
            }
        ]
        patch_response = await request_with_retry(
            app_ctx.http_client,
            "PATCH",
            work_item_url,
            headers=patch_headers,
            params={"api-version": _WIT_API_VERSION},
            content=json.dumps(patch_ops).encode(),
        )
        patch_response.raise_for_status()


@write_tool(
    name="devops_create_pull_request",
    annotations={
        "title": "Create Pull Request",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def devops_create_pull_request(params: CreatePullRequestInput, ctx: Context) -> str:
    """Create a new pull request in an Azure DevOps Git repository.

    Creates a PR from source_ref_name into target_ref_name. Optionally sets
    a description, draft status, reviewers, labels, and work item associations.
    Completion options (delete source branch, merge strategy) can also be set
    at creation time.

    When work_item_ids is supplied, each work item is linked to the newly
    created PR by adding an ArtifactLink relation on the work item side (the
    same mechanism used by devops_link_work_items_to_pull_request). Azure
    DevOps does not honour workItemRefs on the PR create/PATCH API, so that
    field is never set here. Returns the newly created pull request object.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        url = build_url(
            organization,
            project,
            f"git/repositories/{params.repository_id}/pullrequests",
        )

        body: dict = {
            "sourceRefName": params.source_ref_name,
            "targetRefName": params.target_ref_name,
            "title": params.title,
            "isDraft": params.is_draft,
        }
        if params.description is not None:
            body["description"] = params.description
        if params.reviewers:
            body["reviewers"] = [{"id": uid} for uid in params.reviewers]
        if params.labels:
            body["labels"] = [{"name": name} for name in params.labels]

        completion_options: dict = {}
        if params.delete_source_branch:
            completion_options["deleteSourceBranch"] = True
        if params.merge_strategy is not None:
            completion_options["mergeStrategy"] = params.merge_strategy
        if completion_options:
            body["completionOptions"] = completion_options

        response = await request_with_retry(
            app_ctx.http_client,
            "POST",
            url,
            headers=await build_headers(app_ctx, include_content_type=True),
            params={"api-version": _PR_API_VERSION},
            json=body,
        )
        response.raise_for_status()
        created_pr = response.json()

        if params.work_item_ids:
            read_headers = await build_headers(app_ctx)
            await _link_work_items(
                app_ctx,
                organization,
                project,
                params.repository_id,
                created_pr["pullRequestId"],
                params.work_item_ids,
                read_headers,
            )

        return finalize_response(created_pr)

    except ValueError as e:
        return finalize_response({"error": True, "message": str(e)})
    except httpx.HTTPStatusError as e:
        msg = extract_error_message(e.response)
        logger.error("Azure DevOps HTTP %d: %s", e.response.status_code, msg)
        return finalize_response({"error": True, "message": f"Azure DevOps returned HTTP {e.response.status_code}: {msg}"})
    except Exception as e:
        logger.exception("Unexpected error in devops_create_pull_request")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


@write_tool(
    name="devops_update_pull_request",
    annotations={
        "title": "Update Pull Request",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def devops_update_pull_request(params: UpdatePullRequestInput, ctx: Context) -> str:
    """Update an existing Azure DevOps pull request.

    Supply only the fields you want to change. Supports updating title,
    description, status (active/abandoned/completed), draft state, target
    branch, auto-complete, and completion options (delete source branch,
    merge strategy, merge commit message, work item transitions). Returns
    the updated pull request object.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        url = build_url(
            organization,
            project,
            f"git/repositories/{params.repository_id}/pullrequests/{params.pull_request_id}",
        )

        body: dict = {}
        if params.title is not None:
            body["title"] = params.title
        if params.description is not None:
            body["description"] = params.description
        if params.status is not None:
            body["status"] = params.status
        if params.is_draft is not None:
            body["isDraft"] = params.is_draft
        if params.target_ref_name is not None:
            body["targetRefName"] = params.target_ref_name
        if params.auto_complete_identity_id is not None:
            body["autoCompleteSetBy"] = {"id": params.auto_complete_identity_id}

        completion_options: dict = {}
        if params.delete_source_branch is not None:
            completion_options["deleteSourceBranch"] = params.delete_source_branch
        if params.merge_strategy is not None:
            completion_options["mergeStrategy"] = params.merge_strategy
        if params.merge_commit_message is not None:
            completion_options["mergeCommitMessage"] = params.merge_commit_message
        if params.transition_work_items is not None:
            completion_options["transitionWorkItems"] = params.transition_work_items
        if completion_options:
            body["completionOptions"] = completion_options

        response = await request_with_retry(
            app_ctx.http_client,
            "PATCH",
            url,
            headers=await build_headers(app_ctx, include_content_type=True),
            params={"api-version": _PR_API_VERSION},
            json=body,
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
        logger.exception("Unexpected error in devops_update_pull_request")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


@write_tool(
    name="devops_tag_pull_request",
    annotations={
        "title": "Tag or Label Pull Request",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def devops_tag_pull_request(params: TagPullRequestInput, ctx: Context) -> str:
    """Add labels or tags to an Azure DevOps pull request (PR).

    Use this tool when you want to tag, label, or categorize a pull request.
    It adds each specified label using the dedicated labels endpoint. Labels are
    created automatically if they do not already exist in the project. Returns
    a list of the label objects that were created or applied. This operation is
    additive — existing labels on the PR are not removed.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        base_url = build_url(
            organization,
            project,
            f"git/repositories/{params.repository_id}/pullRequests/{params.pull_request_id}/labels",
        )

        results = []
        headers = await build_headers(app_ctx, include_content_type=True)
        for label_name in params.labels:
            response = await request_with_retry(
                app_ctx.http_client,
                "POST",
                base_url,
                headers=headers,
                params={"api-version": _PR_API_VERSION},
                json={"name": label_name},
            )
            response.raise_for_status()
            results.append(response.json())

        return finalize_response({"labels": results, "count": len(results)})

    except ValueError as e:
        return finalize_response({"error": True, "message": str(e)})
    except httpx.HTTPStatusError as e:
        msg = extract_error_message(e.response)
        logger.error("Azure DevOps HTTP %d: %s", e.response.status_code, msg)
        return finalize_response({"error": True, "message": f"Azure DevOps returned HTTP {e.response.status_code}: {msg}"})
    except Exception as e:
        logger.exception("Unexpected error in devops_tag_pull_request")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


@write_tool(
    name="devops_link_work_items_to_pull_request",
    annotations={
        "title": "Link Work Items or Boards Items to Pull Request",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def devops_link_work_items_to_pull_request(
    params: LinkWorkItemsToPullRequestInput, ctx: Context
) -> str:
    """Link Azure Boards work items to an existing Azure DevOps pull request.

    Use this tool when you want to link a work item, board item, story, bug,
    task, or backlog item to a PR. It links one or more work items to a pull
    request by adding ArtifactLink relations on the work item side. Azure
    DevOps does not support updating workItemRefs via the pull request PATCH
    API. For the most reliable work item linking, prefer supplying
    work_item_ids when calling devops_create_pull_request. Returns the updated
    pull request object with the workItemRefs included.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        pr_url = build_url(
            organization,
            project,
            f"git/repositories/{params.repository_id}/pullrequests/{params.pull_request_id}",
        )

        read_headers = await build_headers(app_ctx)
        await _link_work_items(
            app_ctx,
            organization,
            project,
            params.repository_id,
            params.pull_request_id,
            params.work_item_ids,
            read_headers,
        )

        # Return the updated PR with work item refs
        pr_response = await request_with_retry(
            app_ctx.http_client,
            "GET",
            pr_url,
            headers=read_headers,
            params={
                "api-version": _PR_API_VERSION,
                "includeWorkItemRefs": "true",
            },
        )
        pr_response.raise_for_status()
        return finalize_response(pr_response.json())

    except ValueError as e:
        return finalize_response({"error": True, "message": str(e)})
    except httpx.HTTPStatusError as e:
        msg = extract_error_message(e.response)
        logger.error("Azure DevOps HTTP %d: %s", e.response.status_code, msg)
        return finalize_response({"error": True, "message": f"Azure DevOps returned HTTP {e.response.status_code}: {msg}"})
    except Exception as e:
        logger.exception("Unexpected error in devops_link_work_items_to_pull_request")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})

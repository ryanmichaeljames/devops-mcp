"""Work item tools for Azure DevOps MCP."""

import json
import logging
from urllib.parse import quote

import httpx
from mcp.server.fastmcp import Context

from devops_mcp._app import mcp, write_tool
from devops_mcp.client import (
    AppContext,
    build_headers,
    build_params,
    build_url,
    extract_error_message,
    finalize_response,
    request_with_retry,
    resolve_org,
    resolve_project,
)
from devops_mcp.models import (
    AddWorkItemCommentInput,
    CreateWorkItemInput,
    GetWorkItemInput,
    ListWorkItemFieldsInput,
    ListWorkItemsInput,
    ListWorkItemTypesInput,
    QueryWorkItemsInput,
    UpdateWorkItemCommentInput,
    UpdateWorkItemInput,
)

logger = logging.getLogger(__name__)

_WIT_API_VERSION = "7.2-preview.3"
# 7.1-preview.4 is the earliest version that honours the `format` query parameter
# (markdown | html). Older versions silently store every comment as HTML.
_WIT_COMMENTS_API_VERSION = "7.1-preview.4"
_WIT_SCHEMA_API_VERSION = "7.1"


@mcp.tool(
    name="devops_get_work_item",
    annotations={
        "title": "Get Work Item",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def devops_get_work_item(params: GetWorkItemInput, ctx: Context) -> str:
    """Get details of a specific Azure DevOps work item by ID.

    Returns work item fields including title, state, type, assigned user,
    area and iteration paths, created/changed dates, description, and tags.
    Use the 'fields' parameter to limit which fields are returned, and
    'expand' to include relations or links.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        url = build_url(organization, project, f"wit/workitems/{params.work_item_id}")
        query_params = build_params(
            fields=",".join(params.fields) if params.fields else None,
            **{"$expand": params.expand},
        )

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
        logger.exception("Unexpected error in devops_get_work_item")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


@mcp.tool(
    name="devops_list_work_items",
    annotations={
        "title": "List Work Items",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def devops_list_work_items(params: ListWorkItemsInput, ctx: Context) -> str:
    """Bulk-fetch Azure DevOps work items by their IDs (max 200 per call).

    Returns full field values for each work item. Use devops_query_work_items
    to discover work item IDs via a WIQL query, then call this tool to retrieve
    the full details for a specific set of IDs.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        url = build_url(organization, project, "wit/workitems")
        query_params = build_params(
            ids=",".join(str(i) for i in params.ids),
            fields=",".join(params.fields) if params.fields else None,
            errorPolicy=params.error_policy,
            **{"$expand": params.expand},
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
        work_items = data.get("value", [])
        return finalize_response({
            "work_items": work_items,
            "count": data.get("count", len(work_items)),
        })

    except ValueError as e:
        return finalize_response({"error": True, "message": str(e)})
    except httpx.HTTPStatusError as e:
        msg = extract_error_message(e.response)
        logger.error("Azure DevOps HTTP %d: %s", e.response.status_code, msg)
        return finalize_response({"error": True, "message": f"Azure DevOps returned HTTP {e.response.status_code}: {msg}"})
    except Exception as e:
        logger.exception("Unexpected error in devops_list_work_items")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


@mcp.tool(
    name="devops_query_work_items",
    annotations={
        "title": "Query Work Items",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def devops_query_work_items(params: QueryWorkItemsInput, ctx: Context) -> str:
    """Query Azure DevOps work items using WIQL (Work Item Query Language).

    Executes a WIQL query and by default auto-fetches full field values for
    all returned work items (batching in groups of 200 as required by the API).
    Set fetch_details=False to return only IDs and URLs from the WIQL result.

    Common WIQL patterns:
    - All open items:  WHERE [System.TeamProject] = @project AND [System.State] <> 'Closed'
    - Open bugs:       WHERE [System.WorkItemType] = 'Bug' AND [System.State] <> 'Closed'
    - Assigned to me:  WHERE [System.AssignedTo] = @me
    - Recent changes:  WHERE [System.ChangedDate] >= @today - 7

    Note: WIQL only returns IDs; this tool handles the two-step fetch automatically.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        wiql_url = build_url(organization, project, "wit/wiql")

        headers = await build_headers(app_ctx, include_content_type=True)
        wiql_response = await request_with_retry(
            app_ctx.http_client,
            "POST",
            wiql_url,
            headers=headers,
            params=build_params(**{"$top": params.top}),
            json={"query": params.wiql},
        )
        wiql_response.raise_for_status()
        wiql_result = wiql_response.json()

        raw_items = wiql_result.get("workItems", [])
        ids = [item["id"] for item in raw_items]

        if not ids:
            return finalize_response({
                "work_items": [],
                "count": 0,
                "query_type": wiql_result.get("queryType"),
                "as_of": wiql_result.get("asOf"),
            })

        if not params.fetch_details:
            return finalize_response({
                "work_item_ids": ids,
                "count": len(ids),
                "query_type": wiql_result.get("queryType"),
                "as_of": wiql_result.get("asOf"),
            })

        read_headers = await build_headers(app_ctx)
        all_work_items: list[dict] = []
        for i in range(0, len(ids), 200):
            batch = ids[i : i + 200]
            details_url = build_url(organization, project, "wit/workitems")
            details_params = build_params(
                ids=",".join(str(i) for i in batch),
                fields=",".join(params.fields) if params.fields else None,
                errorPolicy="omit",
            )
            details_response = await request_with_retry(
                app_ctx.http_client,
                "GET",
                details_url,
                headers=read_headers,
                params=details_params,
            )
            details_response.raise_for_status()
            all_work_items.extend(details_response.json().get("value", []))

        return finalize_response({
            "work_items": all_work_items,
            "count": len(all_work_items),
            "query_type": wiql_result.get("queryType"),
            "as_of": wiql_result.get("asOf"),
        })

    except ValueError as e:
        return finalize_response({"error": True, "message": str(e)})
    except httpx.HTTPStatusError as e:
        msg = extract_error_message(e.response)
        logger.error("Azure DevOps HTTP %d: %s", e.response.status_code, msg)
        return finalize_response({"error": True, "message": f"Azure DevOps returned HTTP {e.response.status_code}: {msg}"})
    except Exception as e:
        logger.exception("Unexpected error in devops_query_work_items")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


@write_tool(
    name="devops_create_work_item",
    annotations={
        "title": "Create Work Item",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def devops_create_work_item(params: CreateWorkItemInput, ctx: Context) -> str:
    """Create a new work item in an Azure DevOps project.

    Builds a JSON Patch document from the supplied field values and POSTs it
    to the Azure DevOps Work Items API. Returns the newly created work item
    object including its ID, revision, and all fields.

    Common work item types: Bug, Task, User Story, Feature, Epic, Issue,
    Test Case. The exact set of valid types depends on the project process
    template (Agile, Scrum, CMMI, or custom).
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        url = build_url(organization, project, f"wit/workitems/${params.work_item_type}")

        patch_ops: list[dict] = [
            {"op": "add", "path": "/fields/System.Title", "value": params.title},
        ]
        if params.description is not None:
            patch_ops.append({"op": "add", "path": "/fields/System.Description", "value": params.description})
        if params.assigned_to is not None:
            patch_ops.append({"op": "add", "path": "/fields/System.AssignedTo", "value": params.assigned_to})
        if params.state is not None:
            patch_ops.append({"op": "add", "path": "/fields/System.State", "value": params.state})
        if params.area_path is not None:
            patch_ops.append({"op": "add", "path": "/fields/System.AreaPath", "value": params.area_path})
        if params.iteration_path is not None:
            patch_ops.append({"op": "add", "path": "/fields/System.IterationPath", "value": params.iteration_path})
        if params.priority is not None:
            patch_ops.append({"op": "add", "path": "/fields/Microsoft.VSTS.Common.Priority", "value": params.priority})
        if params.tags is not None:
            patch_ops.append({"op": "add", "path": "/fields/System.Tags", "value": params.tags})
        if params.parent_id is not None:
            parent_url = f"https://dev.azure.com/{quote(organization, safe='')}/_apis/wit/workItems/{params.parent_id}"
            patch_ops.append({
                "op": "add",
                "path": "/relations/-",
                "value": {
                    "rel": "System.LinkTypes.Hierarchy-Reverse",
                    "url": parent_url,
                    "attributes": {"isLocked": False},
                },
            })
        if params.additional_fields:
            for field_name, field_value in params.additional_fields.items():
                patch_ops.append({"op": "add", "path": f"/fields/{field_name}", "value": field_value})

        response = await request_with_retry(
            app_ctx.http_client,
            "POST",
            url,
            headers=await build_headers(
                app_ctx,
                extra={"Content-Type": "application/json-patch+json"},
            ),
            params={"api-version": _WIT_API_VERSION},
            content=json.dumps(patch_ops).encode(),
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
        logger.exception("Unexpected error in devops_create_work_item")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


@write_tool(
    name="devops_update_work_item",
    annotations={
        "title": "Update Work Item",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def devops_update_work_item(params: UpdateWorkItemInput, ctx: Context) -> str:
    """Update an existing Azure DevOps work item.

    Builds a JSON Patch document from only the fields you provide and PATCHes
    the work item. Supply only the fields you want to change — omitted fields
    are left unchanged. Returns the updated work item object.

    Use additional_fields for any field not exposed as a named parameter
    (e.g., story points, remaining work, custom fields).
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        url = build_url(organization, project, f"wit/workitems/{params.work_item_id}")

        patch_ops: list[dict] = []
        if params.title is not None:
            patch_ops.append({"op": "add", "path": "/fields/System.Title", "value": params.title})
        if params.description is not None:
            patch_ops.append({"op": "add", "path": "/fields/System.Description", "value": params.description})
        if params.assigned_to is not None:
            patch_ops.append({"op": "add", "path": "/fields/System.AssignedTo", "value": params.assigned_to})
        if params.state is not None:
            patch_ops.append({"op": "add", "path": "/fields/System.State", "value": params.state})
        if params.area_path is not None:
            patch_ops.append({"op": "add", "path": "/fields/System.AreaPath", "value": params.area_path})
        if params.iteration_path is not None:
            patch_ops.append({"op": "add", "path": "/fields/System.IterationPath", "value": params.iteration_path})
        if params.priority is not None:
            patch_ops.append({"op": "add", "path": "/fields/Microsoft.VSTS.Common.Priority", "value": params.priority})
        if params.tags is not None:
            patch_ops.append({"op": "add", "path": "/fields/System.Tags", "value": params.tags})
        if params.comment is not None:
            patch_ops.append({"op": "add", "path": "/fields/System.History", "value": params.comment})
        if params.additional_fields:
            for field_name, field_value in params.additional_fields.items():
                patch_ops.append({"op": "add", "path": f"/fields/{field_name}", "value": field_value})

        if not patch_ops:
            return finalize_response({"error": True, "message": "No fields to update were provided."})

        response = await request_with_retry(
            app_ctx.http_client,
            "PATCH",
            url,
            headers=await build_headers(
                app_ctx,
                extra={"Content-Type": "application/json-patch+json"},
            ),
            params={"api-version": _WIT_API_VERSION},
            content=json.dumps(patch_ops).encode(),
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
        logger.exception("Unexpected error in devops_update_work_item")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


@write_tool(
    name="devops_add_work_item_comment",
    annotations={
        "title": "Add Work Item Comment",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def devops_add_work_item_comment(params: AddWorkItemCommentInput, ctx: Context) -> str:
    """Add a comment to an Azure DevOps work item.

    Posts a new comment to the specified work item's discussion thread. The text
    is stored as markdown by default; pass format='html' to store raw HTML.
    Returns the created comment object including its commentId, version,
    createdBy, and createdDate.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        url = build_url(organization, project, f"wit/workItems/{params.work_item_id}/comments")

        response = await request_with_retry(
            app_ctx.http_client,
            "POST",
            url,
            headers=await build_headers(app_ctx, include_content_type=True),
            params={"api-version": _WIT_COMMENTS_API_VERSION, "format": params.format},
            json={"text": params.text},
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
        logger.exception("Unexpected error in devops_add_work_item_comment")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


@write_tool(
    name="devops_update_work_item_comment",
    annotations={
        "title": "Update Work Item Comment",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def devops_update_work_item_comment(params: UpdateWorkItemCommentInput, ctx: Context) -> str:
    """Update an existing comment on an Azure DevOps work item.

    Replaces the text of the specified comment. The text is stored as markdown by
    default; pass format='html' to store raw HTML. Use devops_add_work_item_comment
    to get the commentId from the original add response, or retrieve existing
    comment IDs via the Azure DevOps work item comments API. Returns the updated
    comment object including the new version number.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        url = build_url(
            organization,
            project,
            f"wit/workItems/{params.work_item_id}/comments/{params.comment_id}",
        )

        response = await request_with_retry(
            app_ctx.http_client,
            "PATCH",
            url,
            headers=await build_headers(app_ctx, include_content_type=True),
            params={"api-version": _WIT_COMMENTS_API_VERSION, "format": params.format},
            json={"text": params.text},
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
        logger.exception("Unexpected error in devops_update_work_item_comment")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


@mcp.tool(
    name="devops_list_work_item_types",
    annotations={
        "title": "List Work Item Types",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def devops_list_work_item_types(params: ListWorkItemTypesInput, ctx: Context) -> str:
    """List work item types defined in an Azure DevOps project.

    Returns type names, reference names, descriptions, colors, and icons.
    Use the name with devops_create_work_item (work_item_type field) and
    devops_list_work_item_fields to discover valid fields per type.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        url = build_url(organization, project, "wit/workitemtypes")

        response = await request_with_retry(
            app_ctx.http_client,
            "GET",
            url,
            headers=await build_headers(app_ctx),
            params={"api-version": _WIT_SCHEMA_API_VERSION},
        )
        response.raise_for_status()
        data = response.json()
        types = data.get("value", [])
        return finalize_response({"work_item_types": types, "count": len(types)})

    except ValueError as e:
        return finalize_response({"error": True, "message": str(e)})
    except httpx.HTTPStatusError as e:
        msg = extract_error_message(e.response)
        logger.error("Azure DevOps HTTP %d: %s", e.response.status_code, msg)
        return finalize_response({"error": True, "message": f"Azure DevOps returned HTTP {e.response.status_code}: {msg}"})
    except Exception as e:
        logger.exception("Unexpected error in devops_list_work_item_types")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


@mcp.tool(
    name="devops_list_work_item_fields",
    annotations={
        "title": "List Work Item Fields",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def devops_list_work_item_fields(params: ListWorkItemFieldsInput, ctx: Context) -> str:
    """List work item field definitions for an Azure DevOps project.

    When work_item_type is provided, returns only the fields applicable to
    that type (e.g., 'Bug', 'Task'). When omitted, returns all fields defined
    in the process. Use reference names with devops_create_work_item's
    additional_fields and with WIQL queries.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)

        if params.work_item_type is not None:
            url = build_url(organization, project, f"wit/workitemtypes/{params.work_item_type}/fields")
        else:
            url = build_url(organization, project, "wit/fields")

        response = await request_with_retry(
            app_ctx.http_client,
            "GET",
            url,
            headers=await build_headers(app_ctx),
            params={"api-version": _WIT_SCHEMA_API_VERSION},
        )
        response.raise_for_status()
        data = response.json()
        fields = data.get("value", [])
        return finalize_response({"fields": fields, "count": len(fields)})

    except ValueError as e:
        return finalize_response({"error": True, "message": str(e)})
    except httpx.HTTPStatusError as e:
        msg = extract_error_message(e.response)
        logger.error("Azure DevOps HTTP %d: %s", e.response.status_code, msg)
        return finalize_response({"error": True, "message": f"Azure DevOps returned HTTP {e.response.status_code}: {msg}"})
    except Exception as e:
        logger.exception("Unexpected error in devops_list_work_item_fields")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})

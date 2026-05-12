"""Work item tools for Azure DevOps MCP."""

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
from devops_mcp.models import GetWorkItemInput, ListWorkItemsInput, QueryWorkItemsInput

logger = logging.getLogger(__name__)


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

        def _query():
            with get_http_client(app_ctx.pat) as client:
                response = client.get(url, params=query_params)
                response.raise_for_status()
                return response.json()

        work_item = await asyncio.to_thread(_query)
        return json.dumps(work_item)

    except ValueError as e:
        return json.dumps({"error": True, "message": str(e)})
    except Exception as e:
        return json.dumps({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


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

        def _query():
            with get_http_client(app_ctx.pat) as client:
                response = client.get(url, params=query_params)
                response.raise_for_status()
                return response.json()

        data = await asyncio.to_thread(_query)
        work_items = data.get("value", [])
        return json.dumps({
            "work_items": work_items,
            "count": data.get("count", len(work_items)),
        })

    except ValueError as e:
        return json.dumps({"error": True, "message": str(e)})
    except Exception as e:
        return json.dumps({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


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

        def _run_wiql():
            with get_http_client(app_ctx.pat) as client:
                response = client.post(
                    wiql_url,
                    params=build_params(**{"$top": params.top}),
                    json={"query": params.wiql},
                )
                response.raise_for_status()
                return response.json()

        wiql_result = await asyncio.to_thread(_run_wiql)
        raw_items = wiql_result.get("workItems", [])
        ids = [item["id"] for item in raw_items]

        if not ids:
            return json.dumps({
                "work_items": [],
                "count": 0,
                "query_type": wiql_result.get("queryType"),
                "as_of": wiql_result.get("asOf"),
            })

        if not params.fetch_details:
            return json.dumps({
                "work_item_ids": ids,
                "count": len(ids),
                "query_type": wiql_result.get("queryType"),
                "as_of": wiql_result.get("asOf"),
            })

        def _fetch_batch(batch_ids: list[int]) -> list[dict]:
            details_url = build_url(organization, project, "wit/workitems")
            details_params = build_params(
                ids=",".join(str(i) for i in batch_ids),
                fields=",".join(params.fields) if params.fields else None,
                errorPolicy="omit",
            )
            with get_http_client(app_ctx.pat) as client:
                response = client.get(details_url, params=details_params)
                response.raise_for_status()
                return response.json().get("value", [])

        all_work_items: list[dict] = []
        for i in range(0, len(ids), 200):
            batch = ids[i : i + 200]
            items = await asyncio.to_thread(_fetch_batch, batch)
            all_work_items.extend(items)

        return json.dumps({
            "work_items": all_work_items,
            "count": len(all_work_items),
            "query_type": wiql_result.get("queryType"),
            "as_of": wiql_result.get("asOf"),
        })

    except ValueError as e:
        return json.dumps({"error": True, "message": str(e)})
    except Exception as e:
        return json.dumps({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})

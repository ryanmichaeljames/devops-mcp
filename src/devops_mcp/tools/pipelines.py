"""Pipeline tools for Azure DevOps MCP."""

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
from devops_mcp.models import (
    GetBuildInput,
    GetPipelineRunInput,
    GetRunLogContentInput,
    ListBuildArtifactsInput,
    ListPipelineRunsInput,
    ListPipelinesInput,
    ListRunLogsInput,
)

logger = logging.getLogger(__name__)


@mcp.tool(
    name="devops_list_pipelines",
    annotations={
        "title": "List Pipelines",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def devops_list_pipelines(params: ListPipelinesInput, ctx: Context) -> str:
    """List pipelines defined in an Azure DevOps project.

    Returns pipeline IDs, names, folder paths, and configuration types (yaml,
    designerJson, etc.). Use the returned pipeline ID with
    devops_list_pipeline_runs to see recent runs for a specific pipeline.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        url = build_url(organization, project, "pipelines")
        query_params = build_params(
            **{
                "$top": params.top,
                "continuationToken": params.continuation_token,
                "orderBy": params.order_by,
            }
        )

        def _query():
            with get_http_client(app_ctx.credential) as client:
                response = client.get(url, params=query_params)
                response.raise_for_status()
                data = response.json()
                continuation = response.headers.get("x-ms-continuationtoken")
                return data, continuation

        data, continuation_token = await asyncio.to_thread(_query)
        pipelines = data.get("value", data) if isinstance(data, dict) else data
        result = {"pipelines": pipelines, "count": len(pipelines)}
        if continuation_token:
            result["continuation_token"] = continuation_token
        return json.dumps(result)

    except ValueError as e:
        return json.dumps({"error": True, "message": str(e)})
    except Exception as e:
        return json.dumps({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


@mcp.tool(
    name="devops_list_pipeline_runs",
    annotations={
        "title": "List Pipeline Runs",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def devops_list_pipeline_runs(params: ListPipelineRunsInput, ctx: Context) -> str:
    """List runs for a specific Azure DevOps pipeline.

    Returns run state (inProgress, completed), result (succeeded, failed, canceled),
    timestamps, triggered branch/commit, and the run ID. The run ID is the same
    as the build ID and can be used with devops_list_run_logs and
    devops_list_build_artifacts.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        url = build_url(organization, project, f"pipelines/{params.pipeline_id}/runs")

        def _query():
            with get_http_client(app_ctx.credential) as client:
                response = client.get(url, params=build_params())
                response.raise_for_status()
                return response.json()

        data = await asyncio.to_thread(_query)
        runs = data.get("value", data) if isinstance(data, dict) else data
        if params.top:
            runs = runs[: params.top]
        return json.dumps({"runs": runs, "count": len(runs)})

    except ValueError as e:
        return json.dumps({"error": True, "message": str(e)})
    except Exception as e:
        return json.dumps({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


@mcp.tool(
    name="devops_get_pipeline_run",
    annotations={
        "title": "Get Pipeline Run",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def devops_get_pipeline_run(params: GetPipelineRunInput, ctx: Context) -> str:
    """Get detailed information about a specific Azure DevOps pipeline run.

    Returns run state, result, timestamps, triggered branch/commit, variables,
    template parameters, and resource links. Use devops_list_run_logs to
    retrieve log entries for this run.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        url = build_url(
            organization, project,
            f"pipelines/{params.pipeline_id}/runs/{params.run_id}",
        )

        def _query():
            with get_http_client(app_ctx.credential) as client:
                response = client.get(url, params=build_params())
                response.raise_for_status()
                return response.json()

        run = await asyncio.to_thread(_query)
        return json.dumps(run)

    except ValueError as e:
        return json.dumps({"error": True, "message": str(e)})
    except Exception as e:
        return json.dumps({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


@mcp.tool(
    name="devops_list_run_logs",
    annotations={
        "title": "List Run Logs",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def devops_list_run_logs(params: ListRunLogsInput, ctx: Context) -> str:
    """List log entries (metadata) for an Azure DevOps pipeline build/run.

    Returns log IDs, line counts, and timestamps for each log in the build.
    Accepts build_id directly — the 'buildId' value from a build URL
    (e.g., dev.azure.com/org/project/_build/results?buildId=12345).
    Use the returned log IDs with devops_get_run_log_content to fetch log text.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        url = build_url(
            organization, project,
            f"build/builds/{params.build_id}/logs",
        )

        def _query():
            with get_http_client(app_ctx.credential) as client:
                response = client.get(url, params=build_params())
                response.raise_for_status()
                return response.json()

        data = await asyncio.to_thread(_query)
        logs = data.get("value", []) if isinstance(data, dict) else data
        return json.dumps({"logs": logs, "count": len(logs)})

    except ValueError as e:
        return json.dumps({"error": True, "message": str(e)})
    except Exception as e:
        return json.dumps({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


@mcp.tool(
    name="devops_get_build",
    annotations={
        "title": "Get Build",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def devops_get_build(params: GetBuildInput, ctx: Context) -> str:
    """Get details of a specific Azure DevOps build by build ID.

    Accepts the build_id directly from a build URL
    (e.g., dev.azure.com/org/project/_build/results?buildId=12345).
    Returns build status, result, branch, commit, triggered-by info, and
    the pipeline definition ID and name — useful for resolving a build URL
    to the pipeline_id needed by other tools.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        url = build_url(organization, project, f"build/builds/{params.build_id}")

        def _query():
            with get_http_client(app_ctx.credential) as client:
                response = client.get(url, params=build_params())
                response.raise_for_status()
                return response.json()

        build = await asyncio.to_thread(_query)
        return json.dumps(build)

    except ValueError as e:
        return json.dumps({"error": True, "message": str(e)})
    except Exception as e:
        return json.dumps({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


@mcp.tool(
    name="devops_get_run_log_content",
    annotations={
        "title": "Get Run Log Content",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def devops_get_run_log_content(params: GetRunLogContentInput, ctx: Context) -> str:
    """Get the plain-text content of a specific log from an Azure DevOps pipeline run.

    Uses the Build API to retrieve actual log text. The build_id is the same as
    the run_id for a given run. Use devops_list_run_logs first to discover
    available log IDs and their line counts.

    Use start_line and end_line to fetch a specific portion of large logs.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        url = build_url(
            organization, project,
            f"build/builds/{params.build_id}/logs/{params.log_id}",
        )
        query_params = build_params(
            startLine=params.start_line,
            endLine=params.end_line,
        )

        def _query():
            with get_http_client(app_ctx.credential) as client:
                # Override Accept to receive plain text log content
                response = client.get(
                    url,
                    params=query_params,
                    headers={"Accept": "text/plain"},
                )
                response.raise_for_status()
                return response.text

        content = await asyncio.to_thread(_query)
        return json.dumps({
            "build_id": params.build_id,
            "log_id": params.log_id,
            "content": content,
        })

    except ValueError as e:
        return json.dumps({"error": True, "message": str(e)})
    except Exception as e:
        return json.dumps({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


@mcp.tool(
    name="devops_list_build_artifacts",
    annotations={
        "title": "List Build Artifacts",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def devops_list_build_artifacts(params: ListBuildArtifactsInput, ctx: Context) -> str:
    """List artifacts produced by an Azure DevOps pipeline build.

    Returns artifact names, types, and download URLs for each artifact
    associated with the build. The build_id is the same as the run_id.
    Optionally filter to a specific artifact by name.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        url = build_url(
            organization, project,
            f"build/builds/{params.build_id}/artifacts",
        )
        query_params = build_params(artifactName=params.artifact_name)

        def _query():
            with get_http_client(app_ctx.credential) as client:
                response = client.get(url, params=query_params)
                response.raise_for_status()
                return response.json()

        data = await asyncio.to_thread(_query)
        if isinstance(data, list):
            artifacts = data
        elif isinstance(data, dict) and "value" in data:
            artifacts = data["value"]
        elif isinstance(data, dict):
            artifacts = [data]
        else:
            artifacts = []

        return json.dumps({"artifacts": artifacts, "count": len(artifacts)})

    except ValueError as e:
        return json.dumps({"error": True, "message": str(e)})
    except Exception as e:
        return json.dumps({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})

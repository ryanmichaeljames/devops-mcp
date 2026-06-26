"""Pipeline tools for Azure DevOps MCP."""

import logging

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
    paginate_results,
    request_with_retry,
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
    RunPipelineInput,
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

        effective_top = params.top if params.top is not None else 100
        base_params = build_params(
            **{
                "$top": effective_top,
                "orderBy": params.order_by,
            }
        )

        headers = await build_headers(app_ctx)
        pipelines, has_more = await paginate_results(
            app_ctx.http_client,
            url,
            headers,
            base_params,
            record_key="value",
            top=effective_top,
            initial_continuation_token=params.continuation_token,
        )

        result: dict = {
            "pipelines": pipelines,
            "count": len(pipelines),
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
        logger.exception("Unexpected error in devops_list_pipelines")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


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

        response = await request_with_retry(
            app_ctx.http_client,
            "GET",
            url,
            headers=await build_headers(app_ctx),
            params=build_params(),
        )
        response.raise_for_status()
        data = response.json()

        # The pipelines/{id}/runs endpoint (api-version 7.1) returns ALL runs in a single
        # response — it supports neither a $top/top query parameter nor x-ms-continuationtoken
        # server-side paging. Client-side slicing is therefore the only option, and applying
        # the cap here is intentional (not a bug). has_more reflects whether the full set
        # exceeded the requested cap.
        runs = data.get("value", data) if isinstance(data, dict) else data
        total_count = len(runs)
        if params.top:
            runs = runs[: params.top]
        return finalize_response({
            "runs": runs,
            "count": len(runs),
            "has_more": params.top is not None and total_count > params.top,
        })

    except ValueError as e:
        return finalize_response({"error": True, "message": str(e)})
    except httpx.HTTPStatusError as e:
        msg = extract_error_message(e.response)
        logger.error("Azure DevOps HTTP %d: %s", e.response.status_code, msg)
        return finalize_response({"error": True, "message": f"Azure DevOps returned HTTP {e.response.status_code}: {msg}"})
    except Exception as e:
        logger.exception("Unexpected error in devops_list_pipeline_runs")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


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
        logger.exception("Unexpected error in devops_get_pipeline_run")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


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

        response = await request_with_retry(
            app_ctx.http_client,
            "GET",
            url,
            headers=await build_headers(app_ctx),
            params=build_params(),
        )
        response.raise_for_status()
        data = response.json()
        logs = data.get("value", []) if isinstance(data, dict) else data
        return finalize_response({"logs": logs, "count": len(logs)})

    except ValueError as e:
        return finalize_response({"error": True, "message": str(e)})
    except httpx.HTTPStatusError as e:
        msg = extract_error_message(e.response)
        logger.error("Azure DevOps HTTP %d: %s", e.response.status_code, msg)
        return finalize_response({"error": True, "message": f"Azure DevOps returned HTTP {e.response.status_code}: {msg}"})
    except Exception as e:
        logger.exception("Unexpected error in devops_list_run_logs")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


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
        logger.exception("Unexpected error in devops_get_build")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


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

        headers = await build_headers(app_ctx)
        headers["Accept"] = "text/plain"
        response = await request_with_retry(
            app_ctx.http_client,
            "GET",
            url,
            headers=headers,
            params=query_params,
        )
        response.raise_for_status()
        return finalize_response({
            "build_id": params.build_id,
            "log_id": params.log_id,
            "content": response.text,
        })

    except ValueError as e:
        return finalize_response({"error": True, "message": str(e)})
    except httpx.HTTPStatusError as e:
        msg = extract_error_message(e.response)
        logger.error("Azure DevOps HTTP %d: %s", e.response.status_code, msg)
        return finalize_response({"error": True, "message": f"Azure DevOps returned HTTP {e.response.status_code}: {msg}"})
    except Exception as e:
        logger.exception("Unexpected error in devops_get_run_log_content")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


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

        response = await request_with_retry(
            app_ctx.http_client,
            "GET",
            url,
            headers=await build_headers(app_ctx),
            params=query_params,
        )
        response.raise_for_status()
        data = response.json()

        if isinstance(data, list):
            artifacts = data
        elif isinstance(data, dict) and "value" in data:
            artifacts = data["value"]
        elif isinstance(data, dict):
            artifacts = [data]
        else:
            artifacts = []

        return finalize_response({"artifacts": artifacts, "count": len(artifacts)})

    except ValueError as e:
        return finalize_response({"error": True, "message": str(e)})
    except httpx.HTTPStatusError as e:
        msg = extract_error_message(e.response)
        logger.error("Azure DevOps HTTP %d: %s", e.response.status_code, msg)
        return finalize_response({"error": True, "message": f"Azure DevOps returned HTTP {e.response.status_code}: {msg}"})
    except Exception as e:
        logger.exception("Unexpected error in devops_list_build_artifacts")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


@write_tool(
    name="devops_run_pipeline",
    annotations={
        "title": "Run Pipeline",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def devops_run_pipeline(params: RunPipelineInput, ctx: Context) -> str:
    """Trigger a new run of an Azure DevOps pipeline.

    Queues a pipeline run and returns the run ID, state, and web URL.
    Optionally override the target branch, template parameters, or
    queue-time variables. Requires AZDO_ALLOW_WRITE=true.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        url = build_url(organization, project, f"pipelines/{params.pipeline_id}/runs")

        body: dict = {}

        if params.branch is not None:
            ref = params.branch if params.branch.startswith("refs/") else f"refs/heads/{params.branch}"
            body["resources"] = {"repositories": {"self": {"refName": ref}}}

        if params.template_parameters is not None:
            body["templateParameters"] = params.template_parameters

        if params.variables is not None:
            body["variables"] = {
                k: {"value": str(v), "isSecret": False}
                for k, v in params.variables.items()
            }

        response = await request_with_retry(
            app_ctx.http_client,
            "POST",
            url,
            headers=await build_headers(app_ctx, include_content_type=True),
            params=build_params(),
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
        logger.exception("Unexpected error in devops_run_pipeline")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})

"""Pipeline tools for Azure DevOps MCP."""

import logging
import re
from datetime import datetime

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
    GetRunTimelineInput,
    ListBuildArtifactsInput,
    ListPipelineRunsInput,
    ListPipelinesInput,
    ListRunLogsInput,
    RunPipelineInput,
    SearchRunLogInput,
)

logger = logging.getLogger(__name__)

# Timeline records whose result falls in this set (or that have errorCount > 0)
# are considered "failing" for the failed_only filter on devops_get_run_timeline.
_TIMELINE_FAILING_RESULTS = {"failed", "canceled", "succeededWithIssues"}


def _compute_duration_seconds(start_time: str | None, finish_time: str | None) -> float | None:
    """Compute elapsed seconds between two ISO 8601 timestamps, or None if unavailable."""
    if not start_time or not finish_time:
        return None
    try:
        start_dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        finish_dt = datetime.fromisoformat(finish_time.replace("Z", "+00:00"))
        return (finish_dt - start_dt).total_seconds()
    except (ValueError, AttributeError):
        return None


async def _fetch_log_line_counts(
    app_ctx: AppContext,
    organization: str,
    project: str,
    build_id: int,
    headers: dict,
) -> dict[int, int]:
    """Fetch {log_id: lineCount} for all logs in a build. Returns {} on any failure."""
    line_counts: dict[int, int] = {}
    try:
        logs_url = build_url(organization, project, f"build/builds/{build_id}/logs")
        logs_response = await request_with_retry(
            app_ctx.http_client,
            "GET",
            logs_url,
            headers=headers,
            params=build_params(),
        )
        logs_response.raise_for_status()
        logs_data = logs_response.json()
        logs = logs_data.get("value", []) if isinstance(logs_data, dict) else logs_data
        for log in logs or []:
            if isinstance(log, dict) and log.get("id") is not None and log.get("lineCount") is not None:
                line_counts[log["id"]] = log["lineCount"]
    except Exception as exc:
        logger.warning(
            "Failed to fetch log line counts for build %d; continuing without them: %s",
            build_id,
            exc,
        )
    return line_counts


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
    name="devops_get_run_timeline",
    annotations={
        "title": "Get Run Timeline",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def devops_get_run_timeline(params: GetRunTimelineInput, ctx: Context) -> str:
    """Get a compact, filtered timeline of an Azure DevOps pipeline build/run.

    This is the recommended starting point for "why did this build fail" —
    it returns a stripped-down projection of the build's step tree (stages,
    phases, jobs, tasks) with state/result/error counts, and each failing
    record's inline issues[] (error/warning messages), which frequently
    contain the actual failure text. This can often answer the question with
    zero log fetches. By default only failing records are returned
    (failed_only=True); set it False to see the full tree.

    Use the returned log_id with devops_get_run_log_content or
    devops_search_run_log only when the inline issue messages aren't enough.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        url = build_url(organization, project, f"build/builds/{params.build_id}/timeline")

        headers = await build_headers(app_ctx)
        response = await request_with_retry(
            app_ctx.http_client,
            "GET",
            url,
            headers=headers,
            params=build_params(),
        )
        response.raise_for_status()
        data = response.json()
        raw_records = data.get("records") or []

        log_line_counts: dict[int, int] = {}
        if params.include_log_line_counts:
            log_line_counts = await _fetch_log_line_counts(
                app_ctx, organization, project, params.build_id, headers
            )

        record_types_lower = (
            {t.lower() for t in params.record_types} if params.record_types else None
        )

        total_errors = 0
        total_warnings = 0
        overall_result = None
        projected: list[dict] = []

        for record in raw_records:
            if not isinstance(record, dict):
                continue

            error_count = record.get("errorCount") or 0
            warning_count = record.get("warningCount") or 0
            total_errors += error_count
            total_warnings += warning_count

            if record.get("parentId") is None:
                overall_result = record.get("result")

            if record_types_lower is not None:
                record_type = record.get("type") or ""
                if record_type.lower() not in record_types_lower:
                    continue

            if params.failed_only:
                result = record.get("result")
                is_failing = result in _TIMELINE_FAILING_RESULTS or error_count > 0
                if not is_failing:
                    continue

            log_ref = record.get("log")
            log_id = log_ref.get("id") if isinstance(log_ref, dict) else None
            start_time = record.get("startTime")
            finish_time = record.get("finishTime")

            projected_record: dict = {
                "id": record.get("id"),
                "parent_id": record.get("parentId"),
                "order": record.get("order"),
                "type": record.get("type"),
                "name": record.get("name"),
                "state": record.get("state"),
                "result": record.get("result"),
                "error_count": error_count,
                "warning_count": warning_count,
                "start_time": start_time,
                "finish_time": finish_time,
                "duration_seconds": _compute_duration_seconds(start_time, finish_time),
                "log_id": log_id,
                "log_line_count": log_line_counts.get(log_id) if log_id is not None else None,
            }

            if params.include_issues:
                issues = record.get("issues") or []
                projected_record["issues"] = [
                    {"type": issue.get("type"), "message": issue.get("message")}
                    for issue in issues
                    if isinstance(issue, dict)
                ]

            projected.append(projected_record)

        return finalize_response({
            "build_id": params.build_id,
            "overall_result": overall_result,
            "counts": {
                "records": len(raw_records),
                "returned": len(projected),
                "errors": total_errors,
                "warnings": total_warnings,
            },
            "records": projected,
            "has_more": False,
        })

    except ValueError as e:
        return finalize_response({"error": True, "message": str(e)})
    except httpx.HTTPStatusError as e:
        msg = extract_error_message(e.response)
        logger.error("Azure DevOps HTTP %d: %s", e.response.status_code, msg)
        return finalize_response({"error": True, "message": f"Azure DevOps returned HTTP {e.response.status_code}: {msg}"})
    except Exception as e:
        logger.exception("Unexpected error in devops_get_run_timeline")
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
    the run_id for a given run. Use devops_list_run_logs or devops_get_run_timeline
    first to discover available log IDs.

    BEHAVIOR CHANGE: a call with no start_line/end_line/tail now returns at most
    max_lines lines (default 500) instead of the whole log — check has_more and
    next_start_line in the response to continue reading. Use start_line/end_line
    to window a specific portion, or tail to read the last N lines (most errors
    are at the end).
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        url = build_url(
            organization, project,
            f"build/builds/{params.build_id}/logs/{params.log_id}",
        )
        headers = await build_headers(app_ctx)

        # Best-effort lookup of the log's total line count (only source of a
        # total — Timeline records don't carry it). Required to compute tail;
        # used to clamp/compute has_more elsewhere, degrading gracefully.
        total_line_count: int | None = None
        line_counts = await _fetch_log_line_counts(
            app_ctx, organization, project, params.build_id, headers
        )
        total_line_count = line_counts.get(params.log_id)

        if params.tail is not None and total_line_count is None:
            return finalize_response({
                "error": True,
                "message": (
                    f"Cannot compute 'tail' window for build {params.build_id} "
                    f"log {params.log_id}: the log's total line count is "
                    "unavailable (the log may still be in progress, or the "
                    "log_id does not exist). Use start_line/end_line instead, "
                    "or retry once the log has completed."
                ),
            })

        start_line = params.start_line
        end_line = params.end_line

        if params.tail is not None:
            # total_line_count is guaranteed non-None here (checked above).
            end_line = total_line_count
            start_line = max(1, total_line_count - params.tail + 1)  # type: ignore[operator]
        elif start_line is None and end_line is None:
            # No range requested: bounded head (the intentional behavior change).
            start_line = 1
            end_line = params.max_lines
            if total_line_count is not None:
                end_line = min(end_line, total_line_count)
        elif start_line is not None and end_line is None:
            end_line = start_line + params.max_lines - 1
            if total_line_count is not None:
                end_line = min(end_line, total_line_count)
        elif start_line is None and end_line is not None:
            start_line = max(1, end_line - params.max_lines + 1)
        # else: both start_line and end_line given explicitly — used as-is,
        # max_lines is ignored (existing behavior preserved).

        # Defensive clamps — startLine/endLine semantics are undocumented by
        # Microsoft; never send a non-positive startLine or an inverted range.
        if start_line < 1:
            start_line = 1
        if end_line < start_line:
            end_line = start_line

        query_params = build_params(startLine=start_line, endLine=end_line)

        content_headers = dict(headers)
        content_headers["Accept"] = "text/plain"
        response = await request_with_retry(
            app_ctx.http_client,
            "GET",
            url,
            headers=content_headers,
            params=query_params,
        )
        response.raise_for_status()
        content = response.text
        returned_line_count = len(content.splitlines()) if content else 0

        if total_line_count is not None:
            has_more = end_line < total_line_count
        else:
            # No total available — infer from whether we got a full page.
            has_more = returned_line_count >= params.max_lines

        next_start_line = end_line + 1 if has_more else None

        return finalize_response({
            "build_id": params.build_id,
            "log_id": params.log_id,
            "total_line_count": total_line_count,
            "start_line": start_line,
            "end_line": end_line,
            "returned_line_count": returned_line_count,
            "has_more": has_more,
            "next_start_line": next_start_line,
            "content": content,
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
    name="devops_search_run_log",
    annotations={
        "title": "Search Run Log",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def devops_search_run_log(params: SearchRunLogInput, ctx: Context) -> str:
    """Search (grep) a specific log from an Azure DevOps pipeline run.

    Downloads the full log text inside the MCP server process and returns
    only matching lines plus a little surrounding context — non-matching
    lines never reach the model, so even a very large log can cost only a
    few dozen lines of tokens. Defaults to a literal substring match; set
    is_regex=True for regular expressions (pattern length and match count
    are bounded to limit runaway regex cost).

    Use devops_get_run_timeline first — its inline issues[] often surface the
    failure text with no log fetch at all. Reach for this tool when you know
    which log to search but not which lines matter.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        url = build_url(
            organization, project,
            f"build/builds/{params.build_id}/logs/{params.log_id}",
        )

        headers = await build_headers(app_ctx)
        headers["Accept"] = "text/plain"
        response = await request_with_retry(
            app_ctx.http_client,
            "GET",
            url,
            headers=headers,
            params=build_params(),
        )
        response.raise_for_status()
        content = response.text
        lines = content.splitlines() if content else []
        total_line_count = len(lines)

        if params.is_regex:
            flags = re.IGNORECASE if params.ignore_case else 0
            compiled = re.compile(params.pattern, flags)

            def _is_match(line: str) -> bool:
                return compiled.search(line) is not None
        else:
            needle = params.pattern.lower() if params.ignore_case else params.pattern

            def _is_match(line: str) -> bool:
                haystack = line.lower() if params.ignore_case else line
                return needle in haystack

        match_count = 0
        matches: list[dict] = []
        for i, line in enumerate(lines):
            if not _is_match(line):
                continue
            match_count += 1
            if len(matches) < params.max_matches:
                context_before = lines[max(0, i - params.context):i]
                context_after = lines[i + 1:i + 1 + params.context]
                matches.append({
                    "line_number": i + 1,
                    "line": line,
                    "context_before": context_before,
                    "context_after": context_after,
                })

        return finalize_response({
            "build_id": params.build_id,
            "log_id": params.log_id,
            "total_line_count": total_line_count,
            "match_count": match_count,
            "truncated": match_count > len(matches),
            "matches": matches,
        })

    except ValueError as e:
        return finalize_response({"error": True, "message": str(e)})
    except httpx.HTTPStatusError as e:
        msg = extract_error_message(e.response)
        logger.error("Azure DevOps HTTP %d: %s", e.response.status_code, msg)
        return finalize_response({"error": True, "message": f"Azure DevOps returned HTTP {e.response.status_code}: {msg}"})
    except re.error as e:
        return finalize_response({"error": True, "message": f"Invalid regex pattern: {e}"})
    except Exception as e:
        logger.exception("Unexpected error in devops_search_run_log")
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

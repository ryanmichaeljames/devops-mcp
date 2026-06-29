"""Advanced Security (GHAzDo) alert tools for Azure DevOps MCP.

All three operations target https://advsec.dev.azure.com — a separate host from the
core dev.azure.com surface used by the other tool modules.  build_advsec_url() in
client.py handles the host difference; the shared auth and HTTP client are unchanged.

Security note: never log response bodies from this module — alert responses may
contain secret values when expand=validationFingerprint is requested.
"""

import logging

import httpx
from mcp.server.fastmcp import Context

from devops_mcp._app import mcp, write_tool
from devops_mcp.client import (
    AppContext,
    build_advsec_url,
    build_headers,
    extract_error_message,
    finalize_response,
    request_with_retry,
    resolve_org,
    resolve_project,
)
from devops_mcp.models import (
    GetAdvancedSecurityAlertInput,
    ListAdvancedSecurityAlertsInput,
    UpdateAdvancedSecurityAlertInput,
)

logger = logging.getLogger(__name__)

_ADVSEC_API_VERSION = "7.2-preview.1"


@mcp.tool(
    name="devops_list_advanced_security_alerts",
    annotations={
        "title": "List Advanced Security Alerts",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def devops_list_advanced_security_alerts(
    params: ListAdvancedSecurityAlertsInput, ctx: Context
) -> str:
    """List GitHub Advanced Security (GHAzDo) alerts for an Azure DevOps repository.

    Returns secret-scanning, dependency-scanning, and code-scanning alerts.
    Use alert_type to filter to a single category, and states/severities to
    narrow by triage status or risk level. Supports cursor-based pagination
    via the continuation_token field (pass the token returned in a previous
    response to retrieve the next page).

    Returns a JSON object with 'alerts' (list), 'count', and optionally
    'continuation_token' when more results are available.

    Requires GitHub Advanced Security to be enabled on the repository. Returns
    an error if the feature is not enabled or the repository is not found.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        url = build_advsec_url(
            organization, project,
            f"alert/repositories/{params.repository}/alerts",
        )

        # Build query params — filter out None values; pass criteria.* names explicitly.
        raw_params: dict = {"api-version": _ADVSEC_API_VERSION, "top": params.top}
        if params.alert_type is not None:
            raw_params["criteria.alertType"] = params.alert_type
        if params.states is not None:
            raw_params["criteria.states"] = params.states
        if params.severities is not None:
            raw_params["criteria.severities"] = params.severities
        if params.rule_id is not None:
            raw_params["criteria.ruleId"] = params.rule_id
        if params.tool_name is not None:
            raw_params["criteria.toolName"] = params.tool_name
        if params.ref is not None:
            raw_params["criteria.ref"] = params.ref
        if params.only_default_branch is not None:
            raw_params["criteria.onlyDefaultBranch"] = str(params.only_default_branch).lower()
        if params.order_by is not None:
            raw_params["orderBy"] = params.order_by
        if params.continuation_token is not None:
            raw_params["continuationToken"] = params.continuation_token

        headers = await build_headers(app_ctx)
        response = await request_with_retry(
            app_ctx.http_client,
            "GET",
            url,
            headers=headers,
            params=raw_params,
        )
        response.raise_for_status()

        data = response.json()
        # The advsec API returns {"count": N, "value": [...]}
        alerts = data.get("value") or []
        next_token = response.headers.get("x-ms-continuationtoken")

        result: dict = {
            "alerts": alerts,
            "count": len(alerts),
        }
        if next_token:
            result["continuation_token"] = next_token

        return finalize_response(result)

    except ValueError as e:
        return finalize_response({"error": True, "message": str(e)})
    except httpx.HTTPStatusError as e:
        msg = extract_error_message(e.response)
        logger.error(
            "Azure DevOps Advanced Security HTTP %d: %s",
            e.response.status_code,
            msg,
        )
        return finalize_response({
            "error": True,
            "message": f"Azure DevOps returned HTTP {e.response.status_code}: {msg}",
        })
    except Exception as e:
        logger.exception("Unexpected error in devops_list_advanced_security_alerts")
        return finalize_response({
            "error": True,
            "message": f"Unexpected error: {type(e).__name__}: {e}",
        })


@mcp.tool(
    name="devops_get_advanced_security_alert",
    annotations={
        "title": "Get Advanced Security Alert",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def devops_get_advanced_security_alert(
    params: GetAdvancedSecurityAlertInput, ctx: Context
) -> str:
    """Get a single GitHub Advanced Security (GHAzDo) alert by its numeric ID.

    Returns full alert details including rule metadata, affected location,
    first/last-seen timestamps, state, and dismissal information if applicable.

    CAUTION: setting expand='validationFingerprint' can return secret values
    in cleartext in the response. Only use it when strictly necessary and ensure
    the response is handled securely. This field defaults to unset (server
    default: 'none') to avoid accidental secret exposure.

    Requires GitHub Advanced Security to be enabled on the repository.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        url = build_advsec_url(
            organization, project,
            f"alert/repositories/{params.repository}/alerts/{params.alert_id}",
        )

        raw_params: dict = {"api-version": _ADVSEC_API_VERSION}
        if params.ref is not None:
            raw_params["ref"] = params.ref
        if params.expand is not None:
            raw_params["expand"] = params.expand

        headers = await build_headers(app_ctx)
        response = await request_with_retry(
            app_ctx.http_client,
            "GET",
            url,
            headers=headers,
            params=raw_params,
        )
        response.raise_for_status()

        # Do NOT log response.json() — may contain secret values when expand=validationFingerprint
        return finalize_response(response.json())

    except ValueError as e:
        return finalize_response({"error": True, "message": str(e)})
    except httpx.HTTPStatusError as e:
        msg = extract_error_message(e.response)
        logger.error(
            "Azure DevOps Advanced Security HTTP %d: %s",
            e.response.status_code,
            msg,
        )
        return finalize_response({
            "error": True,
            "message": f"Azure DevOps returned HTTP {e.response.status_code}: {msg}",
        })
    except Exception as e:
        logger.exception("Unexpected error in devops_get_advanced_security_alert")
        return finalize_response({
            "error": True,
            "message": f"Unexpected error: {type(e).__name__}: {e}",
        })


@write_tool(
    name="devops_update_advanced_security_alert",
    annotations={
        "title": "Update Advanced Security Alert",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def devops_update_advanced_security_alert(
    params: UpdateAdvancedSecurityAlertInput, ctx: Context
) -> str:
    """Update the state of a GitHub Advanced Security (GHAzDo) alert.

    Use this tool to dismiss an alert (with a mandatory reason and optional
    comment), re-activate a previously dismissed alert, or mark an alert as
    fixed. Re-applying the same state is a no-op (idempotent).

    When state='dismissed', dismissed_reason is required. Valid dismissal
    reasons: 'fixed' (issue resolved in code), 'acceptedRisk' (risk accepted
    by the team), 'falsePositive' (finding is incorrect), 'agreedToGuidance'
    (acknowledged guidance), 'toolUpgrade' (resolved by tool update),
    'notDistributed' (secret not in use/distributed).

    Returns the updated alert object. Requires the AZDO_ALLOW_WRITE=true
    environment variable and GitHub Advanced Security to be enabled on the
    repository.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        url = build_advsec_url(
            organization, project,
            f"alert/repositories/{params.repository}/alerts/{params.alert_id}",
        )

        # Build the AlertStateUpdate body — only include keys that are set.
        body: dict = {"state": params.state}
        if params.dismissed_reason is not None:
            body["dismissedReason"] = params.dismissed_reason
        if params.dismissed_comment is not None:
            body["dismissedComment"] = params.dismissed_comment

        headers = await build_headers(app_ctx, include_content_type=True)
        response = await request_with_retry(
            app_ctx.http_client,
            "PATCH",
            url,
            headers=headers,
            params={"api-version": _ADVSEC_API_VERSION},
            json=body,
        )
        response.raise_for_status()

        # Do NOT log response.json() — response may contain sensitive alert details.
        return finalize_response(response.json())

    except ValueError as e:
        return finalize_response({"error": True, "message": str(e)})
    except httpx.HTTPStatusError as e:
        msg = extract_error_message(e.response)
        logger.error(
            "Azure DevOps Advanced Security HTTP %d: %s",
            e.response.status_code,
            msg,
        )
        return finalize_response({
            "error": True,
            "message": f"Azure DevOps returned HTTP {e.response.status_code}: {msg}",
        })
    except Exception as e:
        logger.exception("Unexpected error in devops_update_advanced_security_alert")
        return finalize_response({
            "error": True,
            "message": f"Unexpected error: {type(e).__name__}: {e}",
        })

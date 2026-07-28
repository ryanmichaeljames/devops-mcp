"""Service connection (service endpoint) tools for Azure DevOps MCP.

Read-only. `authorization.parameters` is a credential-shaped, open-ended bag
with no documented redaction contract from Microsoft — both tools here
project the API response onto an explicit allowlist/denylist rather than
passing it through. See devops_mcp.redaction for the full rationale.

Security note: never log response bodies from this module.
"""

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
    request_with_retry,
    resolve_org,
    resolve_project,
)
from devops_mcp.models import GetServiceConnectionInput, ListServiceConnectionsInput
from devops_mcp.redaction import AUTH_PARAM_ALLOWLIST, allowlist_mapping, project_identity, scrub_mapping

logger = logging.getLogger(__name__)

_HTML_SIGNIN_MESSAGE = (
    "Azure DevOps returned an HTML sign-in page instead of JSON — the credential "
    "was not accepted. Check AZDO_AUTH_TYPE and re-authenticate."
)


def _project_operation_status(op: dict | None) -> dict | None:
    """Project the free-form operationStatus bag onto {state, status_message}."""
    if not op:
        return None
    return {"state": op.get("state"), "status_message": op.get("statusMessage")}


def _project_endpoint_project_references(refs: list | None) -> list[dict]:
    """Project serviceEndpointProjectReferences onto [{project_id, project_name, name, description}]."""
    if not refs:
        return []
    projected: list[dict] = []
    for ref in refs:
        project_ref = ref.get("projectReference") or {}
        projected.append({
            "project_id": project_ref.get("id"),
            "project_name": project_ref.get("name"),
            "name": ref.get("name"),
            "description": ref.get("description"),
        })
    return projected


def _project_list_item(ep: dict) -> dict:
    """Project a single ServiceEndpoint onto the discovery-safe list shape."""
    authorization = ep.get("authorization") or {}
    return {
        "id": ep.get("id"),
        "name": ep.get("name"),
        "type": ep.get("type"),
        "url": ep.get("url"),
        "description": ep.get("description"),
        "is_ready": ep.get("isReady"),
        "is_shared": ep.get("isShared"),
        "owner": ep.get("owner"),
        "auth_scheme": authorization.get("scheme"),
        "operation_status": _project_operation_status(ep.get("operationStatus")),
        "shared_project_count": len(ep.get("serviceEndpointProjectReferences") or []),
    }


def _list_error_message(status_code: int, msg: str, *, organization: str, project: str) -> str:
    if status_code == 401:
        return (
            "Authentication failed (HTTP 401). Re-check AZDO_AUTH_TYPE / your sign-in; "
            "reading service connections needs the `vso.serviceendpoint` scope."
        )
    if status_code == 403:
        return (
            "Access denied (HTTP 403). The signed-in identity needs at least the "
            "Reader role on the service connection — Project Settings → Service "
            f"connections → (connection) → Security — in project '{project}'."
        )
    if status_code == 404:
        return f"Project '{project}' not found in organization '{organization}'."
    if status_code == 400:
        return (
            "Azure DevOps rejected the request (HTTP 400) — check the filter values "
            "(names, type, auth_schemes). api-version=7.1."
        )
    return f"Azure DevOps returned HTTP {status_code}: {msg}"


def _get_error_message(status_code: int, msg: str, *, project: str, endpoint_id: str) -> str:
    if status_code == 401:
        return (
            "Authentication failed (HTTP 401). Re-check AZDO_AUTH_TYPE / your sign-in; "
            "reading service connections needs the `vso.serviceendpoint` scope."
        )
    if status_code == 403:
        return (
            "Access denied (HTTP 403). The signed-in identity needs at least the "
            "Reader role on the service connection — Project Settings → Service "
            f"connections → (connection) → Security — in project '{project}'."
        )
    if status_code == 404:
        return (
            f"Service connection {endpoint_id} not found in project '{project}'. "
            "It may not exist, or it may belong to another project and not be "
            "shared into this one."
        )
    if status_code == 400:
        return (
            "Azure DevOps rejected the request (HTTP 400) — check that endpoint_id "
            "is a valid GUID. api-version=7.1."
        )
    return f"Azure DevOps returned HTTP {status_code}: {msg}"


@mcp.tool(
    name="devops_list_service_connections",
    annotations={
        "title": "List Service Connections",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def devops_list_service_connections(params: ListServiceConnectionsInput, ctx: Context) -> str:
    """List service connections (service endpoints) in an Azure DevOps project.

    Discovery tool — returns identity, readiness, and sharing info per
    connection, but never touches authorization.parameters (the credential
    bag); use devops_get_service_connection for the (still redacted) auth
    detail. Filter with type, names, and/or auth_schemes to narrow results —
    this route has no server-side pagination, so top is applied client-side
    and has_more indicates more connections exist than were returned.

    Permission-filtered results are not an error: the API only returns
    connections the signed-in identity can read, so fewer results than the
    UI shows is a permissions symptom, not a bug.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        url = build_url(organization, project, "serviceendpoint/endpoints")

        include_failed = None
        if params.include_failed is not None:
            include_failed = "true" if params.include_failed else "false"

        query_params = build_params(
            type=params.type,
            # httpx serialises a Python list as repeated params, which this
            # API rejects — comma-join array filters into a single value.
            endpointNames=",".join(params.names) if params.names else None,
            authSchemes=",".join(params.auth_schemes) if params.auth_schemes else None,
            includeFailed=include_failed,
        )

        response = await request_with_retry(
            app_ctx.http_client,
            "GET",
            url,
            headers=await build_headers(app_ctx),
            params=query_params,
        )

        # HTML sign-in trap: a rejected token can yield HTTP 200/203 with an
        # HTML body instead of JSON — check before raise_for_status/json().
        content_type = response.headers.get("content-type", "")
        if "text/html" in content_type.lower():
            return finalize_response({"error": True, "message": _HTML_SIGNIN_MESSAGE})

        response.raise_for_status()
        data = response.json()
        endpoints = data.get("value") or []

        top = params.top
        has_more = len(endpoints) > top
        projected = [_project_list_item(ep) for ep in endpoints[:top]]

        return finalize_response({
            "service_connections": projected,
            "count": len(projected),
            "has_more": has_more,
        })

    except ValueError as e:
        return finalize_response({"error": True, "message": str(e)})
    except httpx.HTTPStatusError as e:
        raw_msg = extract_error_message(e.response)
        logger.error("Azure DevOps HTTP %d: %s", e.response.status_code, raw_msg)
        message = _list_error_message(
            e.response.status_code,
            raw_msg,
            organization=organization,
            project=project,
        )
        return finalize_response({"error": True, "message": message})
    except Exception as e:
        logger.exception("Unexpected error in devops_list_service_connections")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


@mcp.tool(
    name="devops_get_service_connection",
    annotations={
        "title": "Get Service Connection",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def devops_get_service_connection(params: GetServiceConnectionInput, ctx: Context) -> str:
    """Get details of a specific Azure DevOps service connection by GUID.

    Credential values are never returned. auth_parameters contains only
    non-secret identity fields from an explicit allowlist (e.g., tenantId,
    serviceprincipalid, username); auth_parameters_dropped lists the names
    (never values) of any authorization.parameters keys that were withheld.
    Set include_data=False to omit the 'data' bag (subscription id, scope
    level, etc.) entirely.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        url = build_url(organization, project, f"serviceendpoint/endpoints/{params.endpoint_id}")

        response = await request_with_retry(
            app_ctx.http_client,
            "GET",
            url,
            headers=await build_headers(app_ctx),
            params=build_params(),
        )

        content_type = response.headers.get("content-type", "")
        if "text/html" in content_type.lower():
            return finalize_response({"error": True, "message": _HTML_SIGNIN_MESSAGE})

        response.raise_for_status()
        data = response.json()

        # It is not documented whether an unknown-but-well-formed GUID 404s
        # or returns 200 with an empty/null body — handle both the same way.
        if not data:
            return finalize_response({
                "error": True,
                "message": (
                    f"Service connection {params.endpoint_id} not found in project "
                    f"'{project}'. It may not exist, or it may belong to another "
                    "project and not be shared into this one."
                ),
            })

        authorization = data.get("authorization") or {}
        auth_params, auth_dropped = allowlist_mapping(authorization.get("parameters"), AUTH_PARAM_ALLOWLIST)

        result: dict = {
            "id": data.get("id"),
            "name": data.get("name"),
            "type": data.get("type"),
            "url": data.get("url"),
            "description": data.get("description"),
            "is_ready": data.get("isReady"),
            "is_shared": data.get("isShared"),
            "owner": data.get("owner"),
            "auth_scheme": authorization.get("scheme"),
            "auth_parameters": auth_params,
            "auth_parameters_dropped": auth_dropped,
            "operation_status": _project_operation_status(data.get("operationStatus")),
            "created_by": project_identity(data.get("createdBy")),
            "project_references": _project_endpoint_project_references(
                data.get("serviceEndpointProjectReferences")
            ),
        }

        if params.include_data:
            scrubbed_data, data_dropped = scrub_mapping(data.get("data"))
            result["data"] = scrubbed_data
            result["data_dropped"] = data_dropped

        return finalize_response(result)

    except ValueError as e:
        return finalize_response({"error": True, "message": str(e)})
    except httpx.HTTPStatusError as e:
        raw_msg = extract_error_message(e.response)
        logger.error("Azure DevOps HTTP %d: %s", e.response.status_code, raw_msg)
        message = _get_error_message(
            e.response.status_code,
            raw_msg,
            project=project,
            endpoint_id=params.endpoint_id,
        )
        return finalize_response({"error": True, "message": message})
    except Exception as e:
        logger.exception("Unexpected error in devops_get_service_connection")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})

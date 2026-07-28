"""Variable group tools for Azure DevOps MCP.

Read-only. `variables` values and `providerData` are credential-adjacent —
both tools here project the API response onto explicit shapes and apply the
same secret-redaction ladder rather than passing the API response through.
See devops_mcp.redaction for the full rationale.

Pagination: single page + explicit x-ms-continuationtoken cursor, mirroring
devops_list_advanced_security_alerts. Deliberately NOT client.paginate_results()
— that helper discards the trailing continuation token, which here is a small
integer the model can hand back to resume; discarding it would leave has_more
with no way forward.

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
from devops_mcp.models import GetVariableGroupInput, ListVariableGroupsInput
from devops_mcp.redaction import is_secret_name, project_identity

logger = logging.getLogger(__name__)

_HTML_SIGNIN_MESSAGE = (
    "Azure DevOps returned an HTML sign-in page instead of JSON — the credential "
    "was not accepted. Check AZDO_AUTH_TYPE and re-authenticate."
)

_MAX_VARIABLE_VALUE_LENGTH = 4096


def _project_variable(name: str, vv: dict | None, include_values: bool) -> dict:
    """Project a single VariableValue through the secret-hygiene ladder.

    1. isSecret truthy -> value key absent, value_available=False (Rule 3 —
       never echo the server's null, omit the key regardless of what came back).
    2. else name matches the credential-name heuristic -> value=None,
       redacted='name_heuristic' (Rule 2 safety net).
    3. else include_values False -> names/flags only.
    4. else -> value included, truncated at _MAX_VARIABLE_VALUE_LENGTH.
    """
    vv = vv or {}
    is_secret = bool(vv.get("isSecret"))
    item: dict = {
        "name": name,
        "is_secret": is_secret,
        "is_readonly": bool(vv.get("isReadOnly")),
    }
    # Undocumented but observed on Key Vault-backed groups; project only when
    # present and never depend on it.
    if "enabled" in vv:
        item["is_enabled"] = vv.get("enabled")

    if is_secret:
        item["value_available"] = False
        return item

    if is_secret_name(name):
        item["value"] = None
        item["redacted"] = "name_heuristic"
        return item

    if not include_values:
        return item

    value = vv.get("value")
    if isinstance(value, str) and len(value) > _MAX_VARIABLE_VALUE_LENGTH:
        item["value"] = value[:_MAX_VARIABLE_VALUE_LENGTH]
        item["truncated"] = True
    else:
        item["value"] = value
    return item


def _project_provider_data(pd: dict | None) -> dict | None:
    """Project VariableGroupProviderData onto {vault, service_endpoint_id, last_refreshed_on}."""
    if not pd:
        return None
    return {
        "vault": pd.get("vault"),
        "service_endpoint_id": pd.get("serviceEndpointId"),
        "last_refreshed_on": pd.get("lastRefreshedOn"),
    }


def _project_group_references(refs: list | None) -> list[dict]:
    """Project variableGroupProjectReferences onto [{project_id, project_name, name, description}]."""
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


def _list_error_message(status_code: int, msg: str, *, organization: str, project: str) -> str:
    if status_code == 401:
        return (
            "Authentication failed (HTTP 401). Re-check AZDO_AUTH_TYPE / your sign-in; "
            "reading variable groups needs the `vso.variablegroups_read` scope."
        )
    if status_code == 403:
        return (
            "Access denied (HTTP 403). The signed-in identity needs at least the "
            "Reader role on the variable group — Pipelines → Library → (group) → "
            f"Security — in project '{project}'."
        )
    if status_code == 404:
        return f"Project '{project}' not found in organization '{organization}'."
    if status_code == 400:
        return (
            "Azure DevOps rejected the request (HTTP 400) — check the filter values "
            "(group_name, action_filter, top, continuation_token). api-version=7.1."
        )
    return f"Azure DevOps returned HTTP {status_code}: {msg}"


def _get_error_message(status_code: int, msg: str, *, project: str, group_id: int) -> str:
    if status_code == 401:
        return (
            "Authentication failed (HTTP 401). Re-check AZDO_AUTH_TYPE / your sign-in; "
            "reading variable groups needs the `vso.variablegroups_read` scope."
        )
    if status_code == 403:
        return (
            "Access denied (HTTP 403). The signed-in identity needs at least the "
            "Reader role on the variable group — Pipelines → Library → (group) → "
            f"Security — in project '{project}'."
        )
    if status_code == 404:
        return (
            f"Variable group {group_id} not found in project '{project}'. It may "
            "not exist, or it may belong to another project and not be shared "
            "into this one."
        )
    if status_code == 400:
        return (
            "Azure DevOps rejected the request (HTTP 400) — check group_id / "
            "continuation_token. api-version=7.1."
        )
    return f"Azure DevOps returned HTTP {status_code}: {msg}"


@mcp.tool(
    name="devops_list_variable_groups",
    annotations={
        "title": "List Variable Groups",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def devops_list_variable_groups(params: ListVariableGroupsInput, ctx: Context) -> str:
    """List variable groups in an Azure DevOps project.

    Discovery tool — returns names, shapes, and per-variable secret flags.
    Variable values are omitted by default (include_values=False); set
    include_values=True to include them (still subject to the same secret
    redaction as devops_get_variable_group). Use devops_get_variable_group
    for a targeted read of one group's values.

    Filter with group_name (supports a trailing '*' wildcard, e.g. 'Prod*').
    Paginates via the continuation_token field: pass the token returned in a
    previous response to fetch the next page.

    Permission-filtered results are not an error: the API only returns
    groups the signed-in identity can read, so fewer results than the UI
    shows is a permissions symptom, not a bug.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        url = build_url(organization, project, "distributedtask/variablegroups")

        query_params = build_params(
            groupName=params.group_name,
            queryOrder=params.query_order,
            **{"$top": params.top},
        )
        if params.continuation_token is not None:
            query_params["continuationToken"] = params.continuation_token

        response = await request_with_retry(
            app_ctx.http_client,
            "GET",
            url,
            headers=await build_headers(app_ctx),
            params=query_params,
        )

        content_type = response.headers.get("content-type", "")
        if "text/html" in content_type.lower():
            return finalize_response({"error": True, "message": _HTML_SIGNIN_MESSAGE})

        response.raise_for_status()
        data = response.json()
        groups = data.get("value") or []

        projected_groups = []
        for group in groups:
            variables = group.get("variables") or {}
            projected_vars = [
                _project_variable(name, vv, params.include_values) for name, vv in variables.items()
            ]
            secret_count = sum(1 for v in projected_vars if v.get("is_secret"))
            projected_groups.append({
                "id": group.get("id"),
                "name": group.get("name"),
                "type": group.get("type"),
                "description": group.get("description"),
                "is_shared": group.get("isShared"),
                "created_on": group.get("createdOn"),
                "modified_on": group.get("modifiedOn"),
                "variable_count": len(projected_vars),
                "secret_variable_count": secret_count,
                "variables": projected_vars,
            })

        result: dict = {
            "variable_groups": projected_groups,
            "count": len(projected_groups),
        }
        next_token = response.headers.get("x-ms-continuationtoken")
        if next_token:
            result["continuation_token"] = next_token

        return finalize_response(result)

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
        logger.exception("Unexpected error in devops_list_variable_groups")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


@mcp.tool(
    name="devops_get_variable_group",
    annotations={
        "title": "Get Variable Group",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def devops_get_variable_group(params: GetVariableGroupInput, ctx: Context) -> str:
    """Get details of a specific Azure DevOps variable group by ID, including values.

    Secret variables (isSecret=true) never have their value returned —
    value_available=False is reported instead, regardless of what the server
    sends back. Non-secret variables whose name matches a credential-like
    pattern (e.g., 'DB_PASSWORD', 'api_key') are withheld too and flagged
    redacted='name_heuristic' as a safety net for values the author forgot to
    mark secret; redacted_variable_count reports how many were caught this way.
    Set include_values=False to return only names and flags.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        url = build_url(organization, project, f"distributedtask/variablegroups/{params.group_id}")

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

        # Undocumented whether an unknown-but-well-formed id 404s or returns
        # 200 with an empty/null body — handle both the same way.
        if not data:
            return finalize_response({
                "error": True,
                "message": (
                    f"Variable group {params.group_id} not found in project "
                    f"'{project}'. It may not exist, or it may belong to another "
                    "project and not be shared into this one."
                ),
            })

        variables = data.get("variables") or {}
        projected_vars = [
            _project_variable(name, vv, params.include_values) for name, vv in variables.items()
        ]
        secret_count = sum(1 for v in projected_vars if v.get("is_secret"))
        redacted_count = sum(1 for v in projected_vars if v.get("redacted") == "name_heuristic")

        result: dict = {
            "id": data.get("id"),
            "name": data.get("name"),
            "type": data.get("type"),
            "description": data.get("description"),
            "is_shared": data.get("isShared"),
            "created_on": data.get("createdOn"),
            "created_by": project_identity(data.get("createdBy")),
            "modified_on": data.get("modifiedOn"),
            "modified_by": project_identity(data.get("modifiedBy")),
            "provider_data": _project_provider_data(data.get("providerData")),
            "project_references": _project_group_references(data.get("variableGroupProjectReferences")),
            "variable_count": len(projected_vars),
            "secret_variable_count": secret_count,
            "redacted_variable_count": redacted_count,
            "variables": projected_vars,
        }

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
            group_id=params.group_id,
        )
        return finalize_response({"error": True, "message": message})
    except Exception as e:
        logger.exception("Unexpected error in devops_get_variable_group")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})

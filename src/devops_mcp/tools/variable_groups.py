"""Variable group tools for Azure DevOps MCP.

`variables` values and `providerData` are credential-adjacent — every tool here
projects the API response onto explicit shapes and applies the same
secret-redaction ladder rather than passing the API response through. See
devops_mcp.redaction for the full rationale.

DIRECTION OF FLOW — the invariant this module is built around. Azure DevOps has
no variable-level write API: the only update verb is a full-replace PUT of the
whole group, so the write tools do the read-merge-write themselves. That merge
MUST be built from the RAW GET body. `_project_variable` drops a secret's value
and nulls any value whose NAME merely looks credential-like, so merging onto the
projected view and PUTting it would write redaction placeholders over live
values — secret and non-secret alike. Hence:

    RAW flows IN to the merge (_fetch_group_raw -> _merge_variables ->
    _build_update_body). PROJECTED flows OUT to the model (_project_group of a
    SERVER response, never of a body this module constructed). The two never
    meet; `_build_update_body` carries a tripwire that raises if they do.

Routing is asymmetric and is the second-easiest thing to get wrong: the GET is
PROJECT-scoped, while every write — PUT, POST and DELETE — is ORGANIZATION-scoped
(`build_org_url`). The resolved project still matters on a write, but as a
payload value (`variableGroupProjectReferences`) or a query parameter
(`projectIds` on the DELETE), not as a path segment.

Deleting a group is PERMANENT. Azure DevOps Library has no recycle bin, no soft
delete and no restore — unlike this repo's saved queries and unlike work items —
so nothing here may borrow their "recoverable" wording.

Concurrency: VariableGroup has no rev, no ETag, and the PUT accepts no
If-Match — there is nothing to mitigate with. A read-merge-write therefore has a
genuine lost-update window: a variable another actor adds between the GET and
the PUT is silently erased. The window is kept as short as the code allows and
`modified_on` is returned so a caller can spot an unexpected change.

Pagination: single page + explicit x-ms-continuationtoken cursor, mirroring
devops_list_advanced_security_alerts. Deliberately NOT client.paginate_results()
— that helper discards the trailing continuation token, which here is a small
integer the model can hand back to resume; discarding it would leave has_more
with no way forward.

Security note: never log response bodies from this module.
"""

import logging

import httpx
from mcp.server.mcpserver import Context

from devops_mcp._app import delete_tool, mcp, write_tool
from devops_mcp.client import (
    AppContext,
    build_headers,
    build_org_url,
    build_params,
    build_url,
    extract_error_message,
    finalize_response,
    request_with_retry,
    resolve_org,
    resolve_project,
)
from devops_mcp.models import (
    CreateVariableGroupInput,
    DeleteVariableGroupInput,
    GetVariableGroupInput,
    ListVariableGroupsInput,
    RemoveVariableGroupVariablesInput,
    SetVariableGroupVariablesInput,
    VariableGroupVariableInput,
    empty_secret_value_error,
)
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


def _project_group(data: dict, include_values: bool) -> dict:
    """Project one VariableGroup API response onto the shape returned to the model.

    The single projection every group-returning tool shares — get, set and
    remove all return this, so a write can be confirmed in the same shape a read
    produces. *data* must always be a SERVER response (a GET or the PUT's echo),
    never a body this module constructed: projecting our own merge result would
    report what we intended rather than what the service stored.
    """
    variables = data.get("variables") or {}
    projected_vars = [_project_variable(name, vv, include_values) for name, vv in variables.items()]
    secret_count = sum(1 for v in projected_vars if v.get("is_secret"))
    redacted_count = sum(1 for v in projected_vars if v.get("redacted") == "name_heuristic")

    return {
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
        # NOT the live path on Azure DevOps Services: an unknown group id AND a
        # group belonging to another project both answer HTTP 200 with a body of
        # `null` (verified live 2026-08-26), which the `if not data:` guard in
        # the tool turns into this same wording. Kept for Azure DevOps Server
        # (on-prem), which may well 404 — the arm costs nothing.
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

        # THE live path for a bad id: verified 2026-08-26, an unknown group id
        # and a group owned by another project both answer HTTP 200 with a body
        # of `null` — never 404. This guard, not the 404 arm below, is what
        # produces the not-found message on Azure DevOps Services.
        if not data:
            return finalize_response({
                "error": True,
                "message": (
                    f"Variable group {params.group_id} not found in project "
                    f"'{project}'. It may not exist, or it may belong to another "
                    "project and not be shared into this one."
                ),
            })

        return finalize_response(_project_group(data, params.include_values))

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


# ---------------------------------------------------------------------------
# Write path — read-merge-write helpers
#
# Everything below builds a FULL-REPLACE PUT body. Read the DIRECTION OF FLOW
# note at the top of this module before changing any of it.
# ---------------------------------------------------------------------------

# VariableGroupParameters has exactly these six properties. The write body is an
# ALLOWLIST projection onto them (redaction Rule 1, applied to an outbound body):
# id / createdBy / createdOn / modifiedBy / modifiedOn / isShared are server-owned
# and dropped, and an unknown future GET field can never leak into a write.
_UPDATE_BODY_KEYS = frozenset({
    "name",
    "description",
    "type",
    "providerData",
    "variableGroupProjectReferences",
    "variables",
})

# Snake-cased keys that only ever exist on a _project_variable() output. Seeing
# one inside a write body's `variables` means the redacted projection reached the
# merge — the exact failure that overwrites live values with placeholders.
_PROJECTED_VARIABLE_KEYS = frozenset({
    "is_secret",
    "is_readonly",
    "is_enabled",
    "value_available",
    "redacted",
    "truncated",
})

# Only classic ("Vsts") groups hold values this server can merge. See
# _ensure_vsts_group.
_VSTS_GROUP_TYPE = "vsts"

# Cap on how many existing variable names an error message lists back.
_MAX_REPORTED_NAMES = 50

# Live-verified 2026-08-26: a duplicate group name answers HTTP **409** carrying
# typeKey `VariableGroupExistsException` — not the 400 the design assumed. Match
# the CODE, not the status (the same lesson the saved-query tools learned), and
# never the "already exists" message text: extract_error_message() prefixes the
# typeKey onto the message, so this holds at whatever status the code rides in on.
_DUPLICATE_NAME_TYPE_KEY = "VariableGroupExistsException"

# Live-verified 2026-08-26: removing every variable is refused with HTTP 400
# `ArgumentException: Variable group must have at least one variable defined.`
# The typeKey there is the generic ArgumentException, which any other bad
# argument also rides, so the message text is the only distinguishing signal.
# Match the trailing "defined" too: the missing-reference 400 one arm below reads
# "ATLEAST one variable group project reference is required" (Microsoft's typo),
# and a fix to that typo upstream would otherwise collide with this marker.
_EMPTY_GROUP_MARKER = "at least one variable defined"


def _raise_on_signin_html(response: httpx.Response) -> None:
    """Turn an HTML sign-in page into the standard ValueError-carried message."""
    if "text/html" in response.headers.get("content-type", "").lower():
        raise ValueError(_HTML_SIGNIN_MESSAGE)


def _assert_raw_variables(variables: object, *, where: str) -> None:
    """THE TRIPWIRE. Refuse anything that is not a raw API `variables` mapping.

    A raw GET's `variables` is {name: {"value": …, "isSecret": …}}; the projected
    view is a LIST of {"name": …, "is_secret": …, "value_available": …}. If the
    projected shape ever reaches a write body, secrets and name-heuristic hits
    are replaced by placeholders and the PUT destroys them. This raises instead —
    a loud failure in place of a silent, unrecoverable one. Do not downgrade it
    to a warning.
    """
    if not isinstance(variables, dict):
        raise ValueError(
            f"Internal error: {where} is a {type(variables).__name__}, not a "
            "mapping of variable name to VariableValue. That is the shape of a "
            "REDACTED projection (see _project_group), which must never be used "
            "to build a write body — doing so overwrites live variable values "
            "with redaction placeholders. Refusing to write."
        )
    for name, entry in variables.items():
        if not isinstance(entry, dict):
            raise ValueError(
                f"Internal error: variable {name!r} in {where} is a "
                f"{type(entry).__name__}, not a VariableValue object. Refusing to write."
            )
        leaked = sorted(_PROJECTED_VARIABLE_KEYS.intersection(entry))
        if leaked:
            raise ValueError(
                f"Internal error: variable {name!r} in {where} carries projected "
                f"key(s) {', '.join(leaked)}. The write body must be built from the "
                "RAW GET response, not from the redacted projection — otherwise "
                "secret and name-heuristic values are overwritten with placeholders. "
                "Refusing to write."
            )


def _carry_forward(entry: dict | None) -> dict:
    """Copy one raw VariableValue into a write body.

    A non-secret entry is carried through byte-identical — unknown keys and all.
    A secret entry is copied with the `value` key REMOVED (the GET returns
    `value: null` for it, and echoing that null back is the single most dangerous
    thing this module could do: an omitted value provably preserves the stored
    secret — it is what the Azure CLI's read-merge-write sends — while what the
    service does with an explicit null is undocumented and could read as "clear").
    """
    carried = dict(entry or {})
    if carried.get("isSecret"):
        carried.pop("value", None)
    return carried


def _index_by_lower(variables: dict) -> dict[str, list[str]]:
    """Group stored variable names by their lower-cased form."""
    index: dict[str, list[str]] = {}
    for name in variables:
        index.setdefault(name.lower(), []).append(name)
    return index


def _ambiguous_name_error(name: str, matches: list[str], group_id: int) -> ValueError:
    return ValueError(
        f"Variable group {group_id} contains {len(matches)} variables whose names "
        f"differ only by case and all match '{name}': {', '.join(sorted(matches))}. "
        "Azure DevOps treats variable names as case-insensitive, so this cannot be "
        "resolved safely here — fix the collision in Pipelines -> Library first."
    )


def _known_names_hint(variables: dict) -> str:
    names = sorted(variables)
    shown = names[:_MAX_REPORTED_NAMES]
    hint = ", ".join(shown) if shown else "(the group has no variables)"
    if len(names) > len(shown):
        hint += f", … ({len(names) - len(shown)} more)"
    return hint


def _merge_variables(
    raw_variables: dict,
    upserts: list[VariableGroupVariableInput],
    *,
    group_id: int,
) -> tuple[dict, list[str], list[str]]:
    """Upsert *upserts* into the RAW variables mapping. Returns (merged, added, updated).

    Every variable the caller did not name is carried through untouched, which is
    what makes a full-replace PUT safe to use for variable-level intent. Name
    matching is case-insensitive and an upsert preserves the STORED key's casing,
    so this can never end up with both `Foo` and `foo` in one group.
    """
    _assert_raw_variables(raw_variables, where=f"variable group {group_id}'s raw variables")

    merged = {name: _carry_forward(vv) for name, vv in raw_variables.items()}
    by_lower = _index_by_lower(merged)

    added: list[str] = []
    updated: list[str] = []

    for item in upserts:
        matches = by_lower.get(item.name.lower(), [])
        if len(matches) > 1:
            raise _ambiguous_name_error(item.name, matches, group_id)

        if matches:
            key = matches[0]
            entry = merged[key]
            was_secret = bool((raw_variables.get(key) or {}).get("isSecret"))
            if was_secret and item.is_secret is False and item.value is None:
                raise ValueError(
                    f"Variable '{key}' in variable group {group_id} is currently a "
                    "secret, and a secret's value cannot be read back. Turning it "
                    "into a plain variable without a value would silently destroy "
                    "that value — pass 'value' in the same call to set the new "
                    "plaintext, or leave 'is_secret' unset to keep it secret."
                )
            # The entry ends up secret unless this call demotes it. The input model
            # already refuses an explicit is_secret=true with an empty value; this
            # arm catches the inherited flag, where only the STORED entry says the
            # variable is a secret. See models.empty_secret_value_error.
            stays_secret = item.is_secret is True or (was_secret and item.is_secret is None)
            if item.value == "" and stays_secret:
                raise empty_secret_value_error(key)
            updated.append(key)
        else:
            if item.value is None:
                raise ValueError(
                    f"Variable '{item.name}' does not exist in variable group "
                    f"{group_id}, so there is no existing value to keep — supply "
                    "'value' to create it. Existing variables: "
                    f"{_known_names_hint(raw_variables)}"
                )
            key = item.name
            # Be explicit rather than leaning on the server's default.
            entry = {"isSecret": False}
            added.append(key)

        if item.is_secret is not None:
            entry["isSecret"] = item.is_secret
        if item.is_readonly is not None:
            entry["isReadOnly"] = item.is_readonly
        if item.value is not None:
            entry["value"] = item.value
        # value is None on an existing variable: leave whatever _carry_forward
        # produced. For a plain -> secret promotion that is the existing
        # plaintext, which the service then encrypts in place; for an untouched
        # secret it is no `value` key at all.
        merged[key] = entry
        by_lower.setdefault(key.lower(), [key])

    return merged, added, updated


def _remove_variables(
    raw_variables: dict,
    names: list[str],
    *,
    ignore_missing: bool,
    group_id: int,
) -> tuple[dict, list[str], list[str]]:
    """Drop *names* from the RAW variables mapping. Returns (merged, removed, missing).

    All-or-nothing: a name that is not present raises unless *ignore_missing*,
    because a partially-applied removal on a full-replace API is worse than none.
    """
    _assert_raw_variables(raw_variables, where=f"variable group {group_id}'s raw variables")

    by_lower = _index_by_lower(raw_variables)
    to_remove: list[str] = []
    missing: list[str] = []

    for name in names:
        matches = by_lower.get(name.lower(), [])
        if len(matches) > 1:
            raise _ambiguous_name_error(name, matches, group_id)
        if matches:
            to_remove.append(matches[0])
        else:
            missing.append(name)

    if missing and not ignore_missing:
        raise ValueError(
            f"Variable(s) not found in variable group {group_id}: "
            f"{', '.join(missing)}. Nothing was removed — this tool is "
            "all-or-nothing. Pass ignore_missing=True to skip absent names "
            "(useful when retrying). Existing variables: "
            f"{_known_names_hint(raw_variables)}"
        )

    dropped = set(to_remove)
    merged = {
        name: _carry_forward(vv) for name, vv in raw_variables.items() if name not in dropped
    }
    return merged, to_remove, missing


def _build_update_body(
    raw_group: dict,
    variables: dict,
    *,
    project_reference: dict | None = None,
) -> dict:
    """Build a VariableGroupParameters body from a RAW group plus merged variables.

    Pure. *raw_group* must be the un-redacted GET body (or, on create, a small
    dict carrying name/description/type). The body is an allowlist projection —
    see _UPDATE_BODY_KEYS — and both `variables` mappings pass the tripwire.

    `variableGroupProjectReferences` is carried through VERBATIM when the group
    has one: the PUT is a full replace, so rebuilding the array from the resolved
    project would silently un-share the group from every other project. It is
    synthesized from *project_reference* only when the group has none.
    """
    if not isinstance(raw_group, dict):
        raise ValueError(
            "Internal error: the write body must be built from the raw "
            f"VariableGroup response; got {type(raw_group).__name__}."
        )
    if raw_group.get("variables") is not None:
        _assert_raw_variables(raw_group.get("variables"), where="the source group's variables")
    _assert_raw_variables(variables, where="the merged variables")

    body: dict = {
        "name": raw_group.get("name"),
        "type": raw_group.get("type") or "Vsts",
        "variables": variables,
    }
    if "description" in raw_group:
        body["description"] = raw_group.get("description")
    if raw_group.get("providerData") is not None:
        body["providerData"] = raw_group.get("providerData")

    references = raw_group.get("variableGroupProjectReferences") or []
    if not references:
        if not project_reference:
            raise ValueError(
                "Internal error: the variable group carries no "
                "variableGroupProjectReferences and none was supplied. The "
                "organization-scoped write route rejects a body without one "
                "(HTTP 400 'Atleast one variable group project reference is required')."
            )
        references = [project_reference]
    body["variableGroupProjectReferences"] = references

    unknown = set(body) - _UPDATE_BODY_KEYS
    if unknown:
        raise ValueError(
            "Internal error: write body carries key(s) outside "
            f"VariableGroupParameters: {', '.join(sorted(unknown))}."
        )
    return body


def _ensure_vsts_group(raw_group: dict, group_id: int) -> None:
    """Refuse any group that is not a classic ('Vsts') variable group.

    A Key Vault-backed group's variables are REFERENCES to vault secrets, not
    values: they are always {"value": null, "isSecret": true} and carry
    undocumented keys alongside a providerData whose schema Microsoft documents as
    an object with no properties. A full-replace PUT built from a shape we cannot
    verify risks breaking every remaining reference. Allowlist, not denylist — an
    unknown future type is refused for the same reason.
    """
    group_type = raw_group.get("type") or ""
    if group_type.lower() != _VSTS_GROUP_TYPE:
        raise ValueError(
            f"Variable group {group_id} has type '{group_type}', not 'Vsts'. Its "
            "variables are references to an external store (e.g. Azure Key Vault), "
            "not values held by Azure DevOps, so they cannot be edited here — "
            "change the secret in the backing vault, or manage the group's "
            "reference list in Pipelines -> Library."
        )


async def _fetch_group_raw(
    app_ctx: AppContext,
    organization: str,
    project: str,
    group_id: int,
) -> dict:
    """Fetch one variable group's RAW, UN-REDACTED body. Project-scoped GET.

    THIS DICT MUST NEVER BE RETURNED TO THE MODEL. Never pass it (or anything
    derived from it other than a write body) to finalize_response, never log it.
    It exists solely to build a full-replace write body: the projection returned
    by devops_get_variable_group has secret values removed and name-heuristic
    values nulled, so merging onto that and PUTting it would overwrite live
    values with placeholders.

    Deliberately not a tool: no @mcp.tool, no Context parameter.
    """
    response = await request_with_retry(
        app_ctx.http_client,
        "GET",
        build_url(organization, project, f"distributedtask/variablegroups/{group_id}"),
        headers=await build_headers(app_ctx),
        params=build_params(),
    )
    _raise_on_signin_html(response)
    response.raise_for_status()
    data = response.json()
    if not data:
        # Verified live 2026-08-26: an unknown id, and an id belonging to another
        # project, both answer HTTP 200 with a body of `null` (4 bytes) — the
        # write paths' not-found error comes from here, not from a 404.
        raise ValueError(
            f"Variable group {group_id} not found in project '{project}'. It may "
            "not exist, or it may belong to another project and not be shared "
            "into this one."
        )
    return data


async def _resolve_project_id(
    app_ctx: AppContext,
    organization: str,
    project: str,
) -> tuple[str, str]:
    """Resolve a project name (or ID) to its (GUID, name).

    Needed because `variableGroupProjectReferences.projectReference.id` must be a
    GUID — a project name there is not reliable.

    An HTTP failure here is re-raised as a ValueError so it cannot be mistaken
    for a failure on the variable group itself by the caller's error mapper.
    """
    response = await request_with_retry(
        app_ctx.http_client,
        "GET",
        build_org_url(organization, f"projects/{project}"),
        headers=await build_headers(app_ctx),
        params=build_params(),
    )
    _raise_on_signin_html(response)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise ValueError(
            f"Could not resolve project '{project}' in organization '{organization}' "
            f"(HTTP {e.response.status_code}: {extract_error_message(e.response)}). "
            "The variable group has no project reference of its own, so the project "
            "GUID is required to write it. Check the project name with devops_list_projects."
        ) from e
    data = response.json() or {}
    project_id = data.get("id")
    if not project_id:
        raise ValueError(
            f"Could not resolve the ID of project '{project}' in organization "
            f"'{organization}'. Check the project name with devops_list_projects."
        )
    return project_id, data.get("name") or project


def _updated_group_result(response: httpx.Response, group_id: int) -> dict:
    """Project a successful write response into the caller-facing payload.

    Values are omitted (include_values=False): the caller supplied them, and
    echoing them back would re-expose plaintext the redaction ladder exists to
    withhold. A 2xx with no body is still a success — but nothing can be read
    back from it, and projecting the body we just SENT would report our intent
    rather than what the service stored, so say so instead.
    """
    try:
        body = response.json() if response.content else None
    except ValueError:
        body = None
    if not body:
        return {
            "id": group_id,
            "updated": True,
            "note": (
                "Azure DevOps returned no body for the update. The write "
                "succeeded; call devops_get_variable_group to read the group back."
            ),
        }
    return _project_group(body, include_values=False)


def _write_error_message(
    status_code: int,
    msg: str,
    *,
    subject: str,
    project: str,
    organization: str,
    stage: str,
) -> str:
    """Map an HTTP failure on a variable-group write. *stage* is 'read' or 'write'.

    404 means two different things either side of the pre-read: the GET is
    project-scoped ("not in this project, or not shared into it"), the PUT is
    organization-scoped ("no such group in the organization at all").

    The first two arms match on the SERVICE'S OWN CODE, never on the status —
    both were observed live at a status the reference does not document, and
    nesting either under a status check makes it dead code.
    """
    if _DUPLICATE_NAME_TYPE_KEY in (msg or ""):
        return (
            f"A variable group with that name already exists in project '{project}', and "
            "Azure DevOps refuses duplicate names. Pick a different name, or change the "
            "existing group with devops_set_variable_group_variables (find its id with "
            f"devops_list_variable_groups). Azure DevOps said: {msg}"
        )
    if _EMPTY_GROUP_MARKER in (msg or "").lower():
        return (
            f"{subject} must keep at least one variable — Azure DevOps refuses a variable "
            "group with none, so nothing was removed. Leave one variable in place, or, if "
            "you meant to get rid of the group itself, delete it with "
            "devops_delete_variable_group (permanent — Library has no recycle bin). "
            f"Azure DevOps said: {msg}"
        )
    if status_code == 401:
        return (
            "Authentication failed (HTTP 401). Re-check AZDO_AUTH_TYPE / your sign-in; "
            "writing variable groups needs the `vso.variablegroups_manage` scope — the "
            "read scope is not enough."
        )
    if status_code == 403:
        return (
            "Access denied (HTTP 403). The signed-in identity needs the Administrator "
            "role on the variable group — Pipelines -> Library -> (group) -> Security — "
            f"in project '{project}'. Reader is enough to read it and not to change it."
        )
    if status_code == 404:
        if stage == "read":
            # As in _get_error_message: on Azure DevOps Services the pre-read
            # never 404s — an unknown or cross-project group id answers 200 with
            # a `null` body (verified live 2026-08-26) and _fetch_group_raw
            # raises the same wording from its own guard. Kept for on-prem.
            return (
                f"{subject} not found in project '{project}'. It may not exist, or it "
                "may belong to another project and not be shared into this one."
            )
        return (
            f"{subject} not found in organization '{organization}' (HTTP 404 on the "
            "organization-scoped write route — note this is a different check from the "
            "project-scoped read: the group id does not exist at all)."
        )
    if status_code == 400:
        lowered = (msg or "").lower()
        if "project reference" in lowered:
            return (
                "Azure DevOps rejected the write because the body carried no variable "
                "group project reference (HTTP 400). This is a bug in devops-mcp, not "
                f"in your input — please report it. Service message: {msg}"
            )
        return f"Azure DevOps rejected the request (HTTP 400): {msg} (api-version=7.1)"
    return f"Azure DevOps returned HTTP {status_code}: {msg}"


@write_tool(
    name="devops_set_variable_group_variables",
    annotations={
        "title": "Set Variable Group Variables",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def devops_set_variable_group_variables(
    params: SetVariableGroupVariablesInput, ctx: Context
) -> str:
    """Create or update variables in an Azure DevOps variable group.

    Only the variables you name are touched — every other variable in the group,
    including secrets, is carried through unchanged. Azure DevOps has no
    variable-level write API (the only update verb replaces the whole group), so
    this tool reads the group, merges your changes, and writes it back.

    Names are matched case-insensitively and an update keeps the stored name's
    casing, so you cannot end up with both 'Foo' and 'foo'.

    - Omit 'value' to leave an existing variable's value alone (changing only its
      flags). A variable that does not exist yet must be given a value.
    - is_secret=true plus a value stores a secret. is_secret=true without a value
      encrypts the existing value in place.
    - An empty 'value' is accepted on a plain variable but refused on a secret:
      Azure DevOps ignores an empty value there and keeps the stored secret, so
      accepting it would report a write that did not happen. Use
      devops_remove_variable_group_variables to get rid of the variable.
    - Turning an existing SECRET back into a plain variable is refused unless you
      supply 'value' in the same call: a secret's value cannot be read back, so
      the tool would otherwise destroy it silently.
    - Key Vault-backed groups (type != 'Vsts') are refused — their variables are
      references to the vault, not values stored here.

    Returns the updated group in the same redacted shape as
    devops_get_variable_group, without values; call that tool to read values.

    Concurrency: Azure DevOps offers no ETag or revision check on this API, so a
    change another actor makes between this tool's read and its write is lost.
    Compare modified_on if that matters to you.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    project = params.project or ""
    organization = params.organization or ""
    stage = "read"
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)

        raw_group = await _fetch_group_raw(app_ctx, organization, project, params.group_id)
        _ensure_vsts_group(raw_group, params.group_id)

        merged, added, updated = _merge_variables(
            raw_group.get("variables") or {},
            params.variables,
            group_id=params.group_id,
        )

        # Only reached when the GET returned no references at all — which, live
        # on Azure DevOps Services, never happens: the project-scoped GET always
        # carries variableGroupProjectReferences (verified 2026-08-26). Kept for
        # the shapes the 7.1 sample response leaves open. Every real call goes
        # straight from the GET to the PUT, keeping the lost-update window as
        # short as the API allows.
        project_reference = None
        if not (raw_group.get("variableGroupProjectReferences") or []):
            project_id, project_name = await _resolve_project_id(app_ctx, organization, project)
            project_reference = {
                "projectReference": {"id": project_id, "name": project_name},
                "name": raw_group.get("name"),
                "description": raw_group.get("description"),
            }

        body = _build_update_body(raw_group, merged, project_reference=project_reference)

        stage = "write"
        response = await request_with_retry(
            app_ctx.http_client,
            "PUT",
            build_org_url(organization, f"distributedtask/variablegroups/{params.group_id}"),
            headers=await build_headers(app_ctx, include_content_type=True),
            params=build_params(),
            json=body,
        )
        _raise_on_signin_html(response)
        response.raise_for_status()

        result = _updated_group_result(response, params.group_id)
        result["variables_added"] = added
        result["variables_updated"] = updated
        return finalize_response(result)

    except ValueError as e:
        return finalize_response({"error": True, "message": str(e)})
    except httpx.HTTPStatusError as e:
        raw_msg = extract_error_message(e.response)
        logger.error("Azure DevOps HTTP %d: %s", e.response.status_code, raw_msg)
        return finalize_response({
            "error": True,
            "message": _write_error_message(
                e.response.status_code,
                raw_msg,
                subject=f"Variable group {params.group_id}",
                project=project,
                organization=organization,
                stage=stage,
            ),
        })
    except Exception as e:
        logger.exception("Unexpected error in devops_set_variable_group_variables")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


@delete_tool(
    name="devops_remove_variable_group_variables",
    annotations={
        "title": "Remove Variable Group Variables",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def devops_remove_variable_group_variables(
    params: RemoveVariableGroupVariablesInput, ctx: Context
) -> str:
    """Remove variables from an Azure DevOps variable group. This destroys data.

    A removed variable's value is gone — there is no recycle bin for library
    items, and a removed SECRET cannot be recreated from anything this server can
    read. Every variable you do not name is carried through unchanged.

    All-or-nothing: if any requested name is absent, nothing is removed and the
    error lists what is missing. Pass ignore_missing=true to skip absent names
    instead, which makes a retried removal idempotent; if every name is already
    gone, no write is issued at all.

    Names are matched case-insensitively. Key Vault-backed groups (type !=
    'Vsts') are refused — remove the reference in Pipelines -> Library instead.

    Returns the updated group in the same redacted shape as
    devops_get_variable_group. Concurrency: Azure DevOps offers no ETag or
    revision check here, so a change another actor makes between this tool's read
    and its write is lost.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    project = params.project or ""
    organization = params.organization or ""
    stage = "read"
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)

        raw_group = await _fetch_group_raw(app_ctx, organization, project, params.group_id)
        _ensure_vsts_group(raw_group, params.group_id)

        merged, removed, missing = _remove_variables(
            raw_group.get("variables") or {},
            params.names,
            ignore_missing=params.ignore_missing,
            group_id=params.group_id,
        )

        if not removed:
            # Nothing to do — every requested name was already absent under
            # ignore_missing. Issue no write at all rather than a no-op
            # full-replace PUT that could lose a concurrent change.
            result = _project_group(raw_group, include_values=False)
            result["removed"] = []
            result["skipped_missing"] = missing
            return finalize_response(result)

        project_reference = None
        if not (raw_group.get("variableGroupProjectReferences") or []):
            project_id, project_name = await _resolve_project_id(app_ctx, organization, project)
            project_reference = {
                "projectReference": {"id": project_id, "name": project_name},
                "name": raw_group.get("name"),
                "description": raw_group.get("description"),
            }

        body = _build_update_body(raw_group, merged, project_reference=project_reference)

        stage = "write"
        response = await request_with_retry(
            app_ctx.http_client,
            "PUT",
            build_org_url(organization, f"distributedtask/variablegroups/{params.group_id}"),
            headers=await build_headers(app_ctx, include_content_type=True),
            params=build_params(),
            json=body,
        )
        _raise_on_signin_html(response)
        response.raise_for_status()

        result = _updated_group_result(response, params.group_id)
        result["removed"] = removed
        if params.ignore_missing:
            result["skipped_missing"] = missing
        return finalize_response(result)

    except ValueError as e:
        return finalize_response({"error": True, "message": str(e)})
    except httpx.HTTPStatusError as e:
        raw_msg = extract_error_message(e.response)
        logger.error("Azure DevOps HTTP %d: %s", e.response.status_code, raw_msg)
        return finalize_response({
            "error": True,
            "message": _write_error_message(
                e.response.status_code,
                raw_msg,
                subject=f"Variable group {params.group_id}",
                project=project,
                organization=organization,
                stage=stage,
            ),
        })
    except Exception as e:
        logger.exception("Unexpected error in devops_remove_variable_group_variables")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


# ---------------------------------------------------------------------------
# Group create / delete — organization-scoped POST and DELETE
# ---------------------------------------------------------------------------


def _new_variables(variables: list[VariableGroupVariableInput]) -> dict:
    """Build a RAW variables mapping for a brand-new group. Pure.

    There is nothing to carry forward on a create, so every entry must supply
    its own value. The input model refuses a missing one first; this is the
    helper's own guard, so the seam is safe for a direct caller too.
    """
    built: dict = {}
    for item in variables:
        if item.value is None:
            raise ValueError(
                f"Variable '{item.name}' has no value. A new variable group has no "
                "existing value to keep — supply 'value' for every variable."
            )
        if item.is_secret and item.value == "":
            # The service takes an empty secret value as "no change" (200, stored
            # as null), so a group created this way would hold an empty variable
            # the caller was told carried a secret. Same guard as the model's.
            raise empty_secret_value_error(item.name)
        entry: dict = {"isSecret": bool(item.is_secret), "value": item.value}
        if item.is_readonly is not None:
            entry["isReadOnly"] = item.is_readonly
        built[item.name] = entry
    return built


def _find_project_reference(references: list | None, project: str) -> tuple[str, str] | None:
    """Locate *project*'s (GUID, name) in a group's reference list.

    The resolved project may be given as either a name or a GUID, so match on
    both. Returning a hit here saves a round-trip to {org}/_apis/projects/{name}
    on the delete path.
    """
    wanted = project.strip().lower()
    for ref in references or []:
        project_ref = (ref or {}).get("projectReference") or {}
        project_id = project_ref.get("id")
        project_name = project_ref.get("name")
        if not project_id:
            continue
        if str(project_id).lower() == wanted or (project_name or "").lower() == wanted:
            return str(project_id), project_name or project
    return None


@write_tool(
    name="devops_create_variable_group",
    annotations={
        "title": "Create Variable Group",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def devops_create_variable_group(params: CreateVariableGroupInput, ctx: Context) -> str:
    """Create a new variable group in an Azure DevOps project.

    Every variable must carry a value — a new group has nothing to inherit one
    from. Mark a variable with is_secret=true to store it encrypted; its value
    can never be read back afterwards, by this server or the portal, and it may
    not be an empty string (Azure DevOps ignores an empty value on a secret).

    The group is created as a classic ('Vsts') group and is owned by the
    resolved project. Key Vault-backed groups are not created here — they need a
    service connection and a vault, which this server does not manage.

    Not idempotent: calling this twice makes two groups, or fails on the
    duplicate name. Use devops_set_variable_group_variables to change an
    existing group.

    Returns the new group in the same redacted shape as
    devops_get_variable_group, without values — the new id is in the response.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    project = params.project or ""
    organization = params.organization or ""
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)

        variables = _new_variables(params.variables)

        # The route carries no project, so the reference list is the only thing
        # telling the service who owns the group — and projectReference.id must
        # be a GUID.
        project_id, project_name = await _resolve_project_id(app_ctx, organization, project)
        project_reference = {
            "projectReference": {"id": project_id, "name": project_name},
            "name": params.name,
            "description": params.description,
        }

        # A synthetic "raw group": _build_update_body is an allowlist projection
        # onto VariableGroupParameters, and create needs exactly the same body.
        synthetic_group: dict = {"name": params.name, "type": "Vsts"}
        if params.description is not None:
            synthetic_group["description"] = params.description

        body = _build_update_body(synthetic_group, variables, project_reference=project_reference)

        # request_with_retry deliberately does NOT retry a POST on 5xx: the
        # create may already have committed, and a retry would make a second group.
        response = await request_with_retry(
            app_ctx.http_client,
            "POST",
            build_org_url(organization, "distributedtask/variablegroups"),
            headers=await build_headers(app_ctx, include_content_type=True),
            params=build_params(),
            json=body,
        )
        _raise_on_signin_html(response)
        response.raise_for_status()

        try:
            created = response.json() if response.content else None
        except ValueError:
            created = None
        # Live-verified 2026-08-26: the POST answers HTTP 200 with the full
        # created group, so this branch does not fire on Azure DevOps Services.
        # Kept anyway — it costs nothing and a bodiless 2xx is still a success.
        if not created:
            return finalize_response({
                "created": True,
                "name": params.name,
                "note": (
                    "Azure DevOps returned no body for the create. The group was "
                    "created; find its id with devops_list_variable_groups "
                    f"(group_name='{params.name}')."
                ),
            })

        return finalize_response(_project_group(created, include_values=False))

    except ValueError as e:
        return finalize_response({"error": True, "message": str(e)})
    except httpx.HTTPStatusError as e:
        raw_msg = extract_error_message(e.response)
        logger.error("Azure DevOps HTTP %d: %s", e.response.status_code, raw_msg)
        return finalize_response({
            "error": True,
            "message": _write_error_message(
                e.response.status_code,
                raw_msg,
                subject=f"Variable group '{params.name}'",
                project=project,
                organization=organization,
                stage="write",
            ),
        })
    except Exception as e:
        logger.exception("Unexpected error in devops_create_variable_group")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


# idempotentHint stays True, and the caveat is recorded here rather than in the
# annotation: a REPEAT call does not answer "already gone, fine" — the pre-read
# finds nothing and the tool returns a not-found error (live-verified: the second
# delete's GET answers 200 `null`). What idempotentHint claims is that repeating
# the call has no ADDITIONAL effect on the environment, and it does not — the end
# state after one delete and after five is identical. Both of this repo's other
# delete tools, devops_delete_work_item and devops_delete_query, behave exactly
# the same way and both declare True; diverging here would make the hint mean
# something different on one tool than on its neighbours.
@delete_tool(
    name="devops_delete_variable_group",
    annotations={
        "title": "Delete Variable Group",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def devops_delete_variable_group(params: DeleteVariableGroupInput, ctx: Context) -> str:
    """Permanently delete an Azure DevOps variable group. This cannot be undone.

    Azure DevOps Library has no recycle bin: there is no soft delete and no
    restore, in the portal or anywhere else. Every variable in the group,
    including secrets nothing can read back, is gone for good — only an external
    backup recovers one. Confirm with the user before calling this.

    Scope: the delete is scoped to the resolved project. all_projects=true scopes
    it to every project the group is shared into, which removes it outright. For
    a shared group, whether scoping to one project removes the group everywhere
    or only un-shares it there is not documented by Azure DevOps — the response
    reports which projects were passed and which references were left behind so
    the outcome can be checked rather than assumed.

    Key Vault-backed groups can be deleted; only the group is removed, never
    anything in the vault.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    project = params.project or ""
    organization = params.organization or ""
    stage = "read"
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)

        # Pre-read for the sharing list, and so an unknown id fails with a clean
        # project-scoped 404 before anything is destroyed.
        raw_group = await _fetch_group_raw(app_ctx, organization, project, params.group_id)
        references = raw_group.get("variableGroupProjectReferences") or []

        targets: list[tuple[str, str]] = []
        if params.all_projects and references:
            for ref in references:
                project_ref = (ref or {}).get("projectReference") or {}
                project_id = project_ref.get("id")
                if project_id:
                    targets.append((str(project_id), project_ref.get("name") or ""))
        else:
            found = _find_project_reference(references, project)
            if found is None:
                # Either the group carries no references at all, or it is shared
                # in under an id/name the pre-read does not spell the same way.
                found = await _resolve_project_id(app_ctx, organization, project)
            targets = [found]

        if not targets:
            raise ValueError(
                f"Could not determine which project to delete variable group "
                f"{params.group_id} from. The organization-scoped delete route "
                "requires at least one project id."
            )

        sent_ids = [pid for pid, _ in targets]
        query_params = build_params()
        # projectIds is an ARRAY parameter that must arrive as ONE comma-joined
        # value. Handing httpx a list emits repeated projectIds= keys, which the
        # service ignores — the delete then silently does not scope as asked.
        query_params["projectIds"] = ",".join(sent_ids)

        stage = "write"
        response = await request_with_retry(
            app_ctx.http_client,
            "DELETE",
            build_org_url(organization, f"distributedtask/variablegroups/{params.group_id}"),
            headers=await build_headers(app_ctx),
            params=query_params,
        )
        _raise_on_signin_html(response)
        # The reference documents 200; live it is a bodiless **204** (verified
        # 2026-08-26). Any 2xx is a success and nothing is read back from the
        # body — the group no longer exists.
        response.raise_for_status()

        sent_lookup = {pid.lower() for pid in sent_ids}
        remaining = [
            ref
            for ref in _project_group_references(references)
            if str(ref.get("project_id") or "").lower() not in sent_lookup
        ]
        note = (
            "Deleted permanently. Azure DevOps Library has no recycle bin, so "
            "this group and its variables cannot be restored."
        )
        if not remaining:
            note += " The group no longer exists in any project."
        else:
            # What a SUBSET delete does to a shared group — drop just those
            # references, or remove the group outright — is undocumented and not
            # verified. Report what was sent rather than assert an outcome.
            note += (
                f" The delete was scoped to {len(sent_ids)} of "
                f"{len(sent_ids) + len(remaining)} referencing project(s); Azure "
                "DevOps does not document whether the group survives in the "
                "others, so confirm with devops_list_variable_groups there. Pass "
                "all_projects=true to remove it everywhere."
            )

        return finalize_response({
            "group_id": params.group_id,
            "name": raw_group.get("name"),
            "deleted": True,
            "recoverable": False,
            "projects_deleted_from": [
                {"project_id": pid, "project_name": pname} for pid, pname in targets
            ],
            "remaining_project_references": remaining,
            "note": note,
        })

    except ValueError as e:
        return finalize_response({"error": True, "message": str(e)})
    except httpx.HTTPStatusError as e:
        raw_msg = extract_error_message(e.response)
        logger.error("Azure DevOps HTTP %d: %s", e.response.status_code, raw_msg)
        return finalize_response({
            "error": True,
            "message": _write_error_message(
                e.response.status_code,
                raw_msg,
                subject=f"Variable group {params.group_id}",
                project=project,
                organization=organization,
                stage=stage,
            ),
        })
    except Exception as e:
        logger.exception("Unexpected error in devops_delete_variable_group")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})

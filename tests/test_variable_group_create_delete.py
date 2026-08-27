"""Unit tests for the variable group create and delete tools.

Both routes are ORGANIZATION-scoped (`https://dev.azure.com/{org}/_apis/...`),
unlike the project-scoped read one module away, and both are asserted on the
wire here:

- POST carries no group id and no project segment; the resolved project survives
  only as `variableGroupProjectReferences`, which the service requires despite
  the schema marking nothing required.
- DELETE carries `projectIds` as ONE COMMA-JOINED VALUE. Handing httpx a list
  emits repeated `projectIds=` keys, which the service ignores — the delete then
  silently does not scope the way the caller asked. The test asserts the raw
  query string, so a repeated-key serialization fails it.

Deletion is PERMANENT: Azure DevOps Library has no recycle bin, no soft delete
and no restore, so the payload's `recoverable: false` and its note are part of
the contract rather than decoration.

The registration gates (`AZDO_ALLOW_WRITE` / `AZDO_ALLOW_DELETE`) are read once
at import of `devops_mcp._app`, so they cannot be flipped in-process without
corrupting the shared registry other tests rely on. All four write tools are
probed in a child process at the foot of this file.

All HTTP is intercepted by a capturing transport — no network, no credentials.
Generic fake org/project identifiers are used throughout.
"""

import json
import os
import subprocess
import sys
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from devops_mcp.models import (
    CreateVariableGroupInput,
    DeleteVariableGroupInput,
    VariableGroupVariableInput,
)
from devops_mcp.tools.variable_groups import (
    _find_project_reference,
    _new_variables,
    _write_error_message,
    devops_create_variable_group,
    devops_delete_variable_group,
)

FAKE_ORG = "testorg"
FAKE_PROJECT = "TestProject"
FAKE_PROJECT_ID = "11111111-2222-3333-4444-555555555555"
OTHER_PROJECT = "OtherProject"
OTHER_PROJECT_ID = "99999999-8888-7777-6666-555555555555"
FAKE_GROUP_ID = 42
FAKE_BEARER = "SUPER-SECRET-BEARER-TOKEN-should-never-appear-anywhere"

PLANTED_PLAIN = "PLANTED-PLAIN-a1b2c3"
PLANTED_SECRET = "PLANTED-SECRET-d4e5f6"

GROUP_NAME = "Shared Config"

_VARIABLE_GROUP_PARAMETERS_KEYS = {
    "name",
    "description",
    "type",
    "providerData",
    "variableGroupProjectReferences",
    "variables",
}


class CapturingTransport(httpx.AsyncBaseTransport):
    """Intercept every HTTP request; dispatch to a handler."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self._handler = handler
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)


def _json_response(status: int, body, request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        headers={"Content-Type": "application/json"},
        content=json.dumps(body).encode(),
        request=request,
    )


def _html_response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        status_code=200,
        headers={"Content-Type": "text/html; charset=utf-8"},
        content=b"<html>sign in</html>",
        request=request,
    )


def _project_reference(project_id: str = FAKE_PROJECT_ID, name: str = FAKE_PROJECT) -> dict:
    return {
        "projectReference": {"id": project_id, "name": name},
        "name": GROUP_NAME,
        "description": "Shared configuration",
    }


def _group_body(
    *,
    references: list | None = None,
    include_references: bool = True,
    group_type: str = "Vsts",
    variables: dict | None = None,
) -> dict:
    body: dict = {
        "id": FAKE_GROUP_ID,
        "type": group_type,
        "name": GROUP_NAME,
        "description": "Shared configuration",
        "isShared": False,
        "createdBy": {"id": "aaaa", "displayName": "Test User"},
        "createdOn": "2026-01-01T00:00:00Z",
        "modifiedBy": {"id": "aaaa", "displayName": "Test User"},
        "modifiedOn": "2026-02-02T00:00:00Z",
        "variables": (
            {
                "PLAIN": {"value": PLANTED_PLAIN, "isSecret": False},
                "DEPLOY_SECRET": {"value": None, "isSecret": True},
            }
            if variables is None
            else variables
        ),
    }
    if include_references:
        body["variableGroupProjectReferences"] = (
            [_project_reference()] if references is None else references
        )
    return body


def _make_handler(
    *,
    group: dict | None = None,
    group_status: int = 200,
    group_body_override=None,
    group_null: bool = False,
    post_status: int = 200,
    post_body=None,
    post_empty: bool = False,
    delete_status: int = 204,
    delete_body=None,
    project_status: int = 200,
    html_on: str | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    group = _group_body() if group is None else group

    def _handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and "/_apis/projects/" in path:
            if project_status >= 400:
                return _json_response(project_status, {"message": "no such project"}, req)
            return _json_response(
                project_status,
                {"id": FAKE_PROJECT_ID, "name": FAKE_PROJECT, "state": "wellFormed"},
                req,
            )
        if req.method == "GET":
            if html_on == "get":
                return _html_response(req)
            if group_null:
                # THE live shape for a bad group id: HTTP 200, body `null`.
                return _json_response(200, None, req)
            if group_status >= 400:
                return _json_response(group_status, {"message": "boom"}, req)
            return _json_response(
                group_status,
                group_body_override if group_body_override is not None else group,
                req,
            )
        if req.method == "POST":
            if html_on == "post":
                return _html_response(req)
            if post_empty:
                return httpx.Response(status_code=post_status, request=req)
            if post_status >= 400:
                return _json_response(post_status, post_body or {"message": "boom"}, req)
            if post_body is not None:
                return _json_response(post_status, post_body, req)
            # The real Add operation answers with the created group.
            sent = json.loads(req.content)
            return _json_response(
                post_status,
                {**group, **sent, "id": FAKE_GROUP_ID, "createdOn": "2026-08-26T00:00:00Z"},
                req,
            )
        if req.method == "DELETE":
            if html_on == "delete":
                return _html_response(req)
            if delete_status >= 400:
                return _json_response(delete_status, delete_body or {"message": "boom"}, req)
            if delete_body is not None:
                return _json_response(delete_status, delete_body, req)
            # Documented as 200 with a body; observed as a bodiless 2xx.
            return httpx.Response(status_code=delete_status, request=req)
        raise AssertionError(f"Unexpected request: {req.method} {req.url}")

    return _handler


@pytest.fixture()
def make_transport_and_ctx():
    def _factory(**handler_kwargs):
        transport = CapturingTransport(_make_handler(**handler_kwargs))
        app_ctx = MagicMock()
        app_ctx.organization = FAKE_ORG
        app_ctx.project = FAKE_PROJECT
        app_ctx.http_client = httpx.AsyncClient(transport=transport)
        mcp_ctx = MagicMock()
        mcp_ctx.request_context.lifespan_context = app_ctx
        return transport, mcp_ctx

    return _factory


def _auth_patches():
    fake_headers = {"Authorization": f"Bearer {FAKE_BEARER}", "Accept": "application/json"}
    return [
        patch(
            "devops_mcp.tools.variable_groups.build_headers",
            new=AsyncMock(return_value=fake_headers),
        ),
        patch("devops_mcp.tools.variable_groups.resolve_org", return_value=FAKE_ORG),
        patch("devops_mcp.tools.variable_groups.resolve_project", return_value=FAKE_PROJECT),
    ]


async def _call(tool, params, mcp_ctx) -> dict:
    patches = _auth_patches()
    for p in patches:
        p.start()
    try:
        return json.loads(await tool(params, mcp_ctx))
    finally:
        for p in patches:
            p.stop()


def _create_input(**kwargs) -> CreateVariableGroupInput:
    kwargs.setdefault("name", GROUP_NAME)
    kwargs.setdefault("variables", [{"name": "PLAIN", "value": PLANTED_PLAIN}])
    return CreateVariableGroupInput(**kwargs)


def _delete_input(**kwargs) -> DeleteVariableGroupInput:
    kwargs.setdefault("group_id", FAKE_GROUP_ID)
    return DeleteVariableGroupInput(**kwargs)


def _requests_of(transport: CapturingTransport, method: str) -> list[httpx.Request]:
    return [r for r in transport.requests if r.method == method]


def _one_request(transport: CapturingTransport, method: str) -> httpx.Request:
    matching = _requests_of(transport, method)
    assert len(matching) == 1, f"Expected exactly one {method}, got {len(matching)}"
    return matching[0]


def _post_body(transport: CapturingTransport) -> dict:
    return json.loads(_one_request(transport, "POST").content)


# ---------------------------------------------------------------------------
# Create — routing and body construction
# ---------------------------------------------------------------------------


async def test_post_is_org_scoped_with_no_group_id(make_transport_and_ctx):
    transport, ctx = make_transport_and_ctx()
    result = await _call(devops_create_variable_group, _create_input(), ctx)
    assert result.get("error") is not True, result

    req = _one_request(transport, "POST")
    assert str(req.url) == (
        f"https://dev.azure.com/{FAKE_ORG}/_apis/distributedtask/variablegroups?api-version=7.1"
    )
    assert f"/{FAKE_PROJECT}/" not in req.url.path, "the POST route has no project segment"
    assert not req.url.path.rstrip("/").endswith(str(FAKE_GROUP_ID))
    assert req.headers["content-type"].startswith("application/json")


async def test_create_body_carries_only_variable_group_parameters_keys(make_transport_and_ctx):
    transport, ctx = make_transport_and_ctx()
    await _call(devops_create_variable_group, _create_input(description="Config"), ctx)

    body = _post_body(transport)
    assert set(body) <= _VARIABLE_GROUP_PARAMETERS_KEYS
    for server_owned in ("id", "createdBy", "createdOn", "modifiedBy", "modifiedOn", "isShared"):
        assert server_owned not in body
    assert body["name"] == GROUP_NAME
    assert body["description"] == "Config"
    assert body["type"] == "Vsts", "type is not caller-settable; creation is always classic"


async def test_create_omits_description_when_none_is_given(make_transport_and_ctx):
    transport, ctx = make_transport_and_ctx()
    await _call(devops_create_variable_group, _create_input(), ctx)
    assert "description" not in _post_body(transport)


async def test_create_resolves_the_project_guid_for_the_reference(make_transport_and_ctx):
    """The org-scoped route has no project, so the reference is the only owner signal."""
    transport, ctx = make_transport_and_ctx()
    await _call(devops_create_variable_group, _create_input(description="Config"), ctx)

    lookups = [r for r in transport.requests if "/_apis/projects/" in r.url.path]
    assert len(lookups) == 1
    assert lookups[0].url.path == f"/{FAKE_ORG}/_apis/projects/{FAKE_PROJECT}"

    assert _post_body(transport)["variableGroupProjectReferences"] == [
        {
            "projectReference": {"id": FAKE_PROJECT_ID, "name": FAKE_PROJECT},
            "name": GROUP_NAME,
            "description": "Config",
        }
    ]


async def test_create_sends_variables_with_flags(make_transport_and_ctx):
    transport, ctx = make_transport_and_ctx()
    await _call(
        devops_create_variable_group,
        _create_input(
            variables=[
                {"name": "PLAIN", "value": PLANTED_PLAIN},
                {"name": "TOKEN", "value": PLANTED_SECRET, "is_secret": True},
                {"name": "PINNED", "value": "1", "is_readonly": True},
            ]
        ),
        ctx,
    )

    assert _post_body(transport)["variables"] == {
        "PLAIN": {"isSecret": False, "value": PLANTED_PLAIN},
        "TOKEN": {"isSecret": True, "value": PLANTED_SECRET},
        "PINNED": {"isSecret": False, "value": "1", "isReadOnly": True},
    }


async def test_create_unresolvable_project_fails_before_the_write(make_transport_and_ctx):
    transport, ctx = make_transport_and_ctx(project_status=404)
    result = await _call(devops_create_variable_group, _create_input(), ctx)

    assert result["error"] is True
    assert "Could not resolve project" in result["message"]
    assert _requests_of(transport, "POST") == []


# ---------------------------------------------------------------------------
# Create — input validation
# ---------------------------------------------------------------------------


def test_create_without_a_value_is_an_input_error():
    """A new group has no existing value to inherit, so 'value' is mandatory."""
    with pytest.raises(ValueError, match="no 'value'"):
        _create_input(variables=[{"name": "GHOST", "is_secret": True}])


def test_new_variables_helper_refuses_a_missing_value():
    """The helper guards its own seam, not just the model that normally feeds it."""
    with pytest.raises(ValueError, match="has no value"):
        _new_variables([VariableGroupVariableInput(name="GHOST")])


def test_new_variables_helper_refuses_an_empty_secret_value():
    """The helper guards its own seam here too, not just the model that feeds it."""
    # model_construct bypasses the model's own refusal, which is the point: the
    # helper must not depend on having been fed a validated model.
    item = VariableGroupVariableInput.model_construct(name="TOKEN", value="", is_secret=True)
    with pytest.raises(ValueError, match="ignores an empty value on a secret"):
        _new_variables([item])


def test_create_rejects_case_colliding_names():
    with pytest.raises(ValueError, match="Duplicate variable name"):
        _create_input(variables=[{"name": "Foo", "value": "a"}, {"name": "foo", "value": "b"}])


def test_create_rejects_a_blank_group_name():
    with pytest.raises(ValueError):
        _create_input(name="   ")


def test_create_accepts_an_empty_string_value():
    created = _create_input(variables=[{"name": "EMPTY", "value": ""}])
    assert created.variables[0].value == ""


def test_create_refuses_an_empty_string_value_on_a_secret():
    """Azure DevOps ignores an empty value on a secret (live-verified 2026-08-26).

    It answers 200 and stores nothing, so a created 'secret' would be an empty
    variable the caller was told had a value. Refuse it at the input model.
    """
    with pytest.raises(ValueError, match="ignores an empty value on a secret"):
        _create_input(variables=[{"name": "TOKEN", "value": "", "is_secret": True}])


# ---------------------------------------------------------------------------
# Create — response and errors
# ---------------------------------------------------------------------------


async def test_create_returns_the_redacted_projection_with_the_new_id(make_transport_and_ctx):
    _, ctx = make_transport_and_ctx()
    result = await _call(
        devops_create_variable_group,
        _create_input(
            variables=[
                {"name": "PLAIN", "value": PLANTED_PLAIN},
                {"name": "TOKEN", "value": PLANTED_SECRET, "is_secret": True},
            ]
        ),
        ctx,
    )

    assert result.get("error") is not True, result
    assert result["id"] == FAKE_GROUP_ID
    assert result["name"] == GROUP_NAME
    assert result["variable_count"] == 2
    assert result["secret_variable_count"] == 1

    serialized = json.dumps(result)
    for planted in (PLANTED_PLAIN, PLANTED_SECRET, FAKE_BEARER):
        assert planted not in serialized, f"PLANTED VALUE LEAKED: {planted!r}"


async def test_create_reports_a_duplicate_group_name(make_transport_and_ctx):
    """The live duplicate-name answer is HTTP 409, not the documented-ish 400.

    Verified live 2026-08-26. The mapper keys on the typeKey
    `VariableGroupExistsException`, so nesting this under a status check (as an
    earlier revision did under 400) makes the tailored guidance dead code.
    """
    _, ctx = make_transport_and_ctx(
        post_status=409,
        post_body={
            "message": f"Variable group '{GROUP_NAME}' already exists.",
            "typeKey": "VariableGroupExistsException",
            "eventId": 3000,
        },
    )
    result = await _call(devops_create_variable_group, _create_input(), ctx)

    assert result["error"] is True
    assert "already exists" in result["message"]
    assert FAKE_PROJECT in result["message"]
    # Actionable: pick another name, or edit the group that is already there.
    assert "devops_set_variable_group_variables" in result["message"]


@pytest.mark.parametrize("status", [409, 400])
def test_duplicate_name_is_matched_on_the_type_key_not_the_status(status):
    """Match the CODE, not the status — the repo's saved-query lesson.

    The same typeKey has to produce the same guidance whatever status carries
    it, so a service that moves the duplicate-name error between 409 and 400
    cannot silently disable the message again.
    """
    message = _write_error_message(
        status,
        f"VariableGroupExistsException: Variable group '{GROUP_NAME}' already exists.",
        subject=f"Variable group '{GROUP_NAME}'",
        project=FAKE_PROJECT,
        organization=FAKE_ORG,
        stage="write",
    )
    assert "already exists" in message
    assert FAKE_PROJECT in message
    assert f"HTTP {status}" not in message, "the status is not the signal here"


async def test_create_401_names_the_manage_scope(make_transport_and_ctx):
    _, ctx = make_transport_and_ctx(post_status=401)
    result = await _call(devops_create_variable_group, _create_input(), ctx)

    assert result["error"] is True
    assert "vso.variablegroups_manage" in result["message"]


async def test_create_403_names_the_administrator_role(make_transport_and_ctx):
    _, ctx = make_transport_and_ctx(post_status=403)
    result = await _call(devops_create_variable_group, _create_input(), ctx)

    assert result["error"] is True
    assert "Administrator" in result["message"]


async def test_create_with_an_empty_response_body_is_still_a_success(make_transport_and_ctx):
    """Defensive only: live, the POST answers 200 with the full created group
    (verified 2026-08-26), so this branch never fires on Azure DevOps Services."""
    _, ctx = make_transport_and_ctx(post_empty=True, post_status=204)
    result = await _call(devops_create_variable_group, _create_input(), ctx)

    assert result.get("error") is not True, result
    assert result["created"] is True
    assert result["name"] == GROUP_NAME
    assert "devops_list_variable_groups" in result["note"]


async def test_create_reports_an_html_signin_page(make_transport_and_ctx):
    _, ctx = make_transport_and_ctx(html_on="post")
    result = await _call(devops_create_variable_group, _create_input(), ctx)

    assert result["error"] is True
    assert "HTML sign-in page" in result["message"]


# ---------------------------------------------------------------------------
# Delete — routing, and the comma-joined projectIds
# ---------------------------------------------------------------------------


async def test_delete_is_org_scoped(make_transport_and_ctx):
    transport, ctx = make_transport_and_ctx()
    result = await _call(devops_delete_variable_group, _delete_input(), ctx)
    assert result.get("error") is not True, result

    req = _one_request(transport, "DELETE")
    assert req.url.path == f"/{FAKE_ORG}/_apis/distributedtask/variablegroups/{FAKE_GROUP_ID}"
    assert f"/{FAKE_PROJECT}/" not in req.url.path, "the DELETE route has no project segment"
    assert req.url.params["api-version"] == "7.1"

    # The pre-read, by contrast, IS project-scoped.
    get_req = _one_request(transport, "GET")
    assert get_req.url.path == (
        f"/{FAKE_ORG}/{FAKE_PROJECT}/_apis/distributedtask/variablegroups/{FAKE_GROUP_ID}"
    )


async def test_project_ids_reach_the_wire_as_one_comma_joined_value(make_transport_and_ctx):
    """A list would serialize as repeated keys and the filter would not apply."""
    references = [
        _project_reference(),
        _project_reference(project_id=OTHER_PROJECT_ID, name=OTHER_PROJECT),
    ]
    transport, ctx = make_transport_and_ctx(group=_group_body(references=references))
    await _call(devops_delete_variable_group, _delete_input(all_projects=True), ctx)

    req = _one_request(transport, "DELETE")
    assert req.url.params.get_list("projectIds") == [f"{FAKE_PROJECT_ID},{OTHER_PROJECT_ID}"]
    # One key on the raw query string — a repeated-key serialization fails here.
    assert str(req.url).count("projectIds=") == 1


async def test_delete_defaults_to_the_resolved_project_only(make_transport_and_ctx):
    references = [
        _project_reference(),
        _project_reference(project_id=OTHER_PROJECT_ID, name=OTHER_PROJECT),
    ]
    transport, ctx = make_transport_and_ctx(group=_group_body(references=references))
    result = await _call(devops_delete_variable_group, _delete_input(), ctx)

    req = _one_request(transport, "DELETE")
    assert req.url.params["projectIds"] == FAKE_PROJECT_ID
    assert OTHER_PROJECT_ID not in str(req.url)

    assert result["projects_deleted_from"] == [
        {"project_id": FAKE_PROJECT_ID, "project_name": FAKE_PROJECT}
    ]
    assert [r["project_id"] for r in result["remaining_project_references"]] == [OTHER_PROJECT_ID]
    assert "all_projects=true" in result["note"]
    # Subset-delete semantics on a shared group are undocumented — the note must
    # report what was sent, never assert that the group survived elsewhere.
    assert "scoped to 1 of 2" in result["note"]
    # No extra project lookup: the reference list already carried the GUID.
    assert [r for r in transport.requests if "/_apis/projects/" in r.url.path] == []


async def test_delete_all_projects_sends_every_reference(make_transport_and_ctx):
    references = [
        _project_reference(),
        _project_reference(project_id=OTHER_PROJECT_ID, name=OTHER_PROJECT),
    ]
    _, ctx = make_transport_and_ctx(group=_group_body(references=references))
    result = await _call(devops_delete_variable_group, _delete_input(all_projects=True), ctx)

    assert result["remaining_project_references"] == []
    assert "no longer exists in any project" in result["note"]
    assert {p["project_id"] for p in result["projects_deleted_from"]} == {
        FAKE_PROJECT_ID,
        OTHER_PROJECT_ID,
    }


async def test_delete_falls_back_to_the_projects_api_without_references(make_transport_and_ctx):
    transport, ctx = make_transport_and_ctx(group=_group_body(include_references=False))
    result = await _call(devops_delete_variable_group, _delete_input(), ctx)

    lookups = [r for r in transport.requests if "/_apis/projects/" in r.url.path]
    assert len(lookups) == 1
    assert _one_request(transport, "DELETE").url.params["projectIds"] == FAKE_PROJECT_ID
    assert result["remaining_project_references"] == []


def test_find_project_reference_matches_name_or_guid():
    references = [_project_reference(project_id=OTHER_PROJECT_ID, name=OTHER_PROJECT)]
    assert _find_project_reference(references, "otherproject") == (
        OTHER_PROJECT_ID,
        OTHER_PROJECT,
    )
    assert _find_project_reference(references, OTHER_PROJECT_ID.upper()) == (
        OTHER_PROJECT_ID,
        OTHER_PROJECT,
    )
    assert _find_project_reference(references, "Nope") is None
    assert _find_project_reference(None, FAKE_PROJECT) is None


# ---------------------------------------------------------------------------
# Delete — semantics
# ---------------------------------------------------------------------------


async def test_delete_reports_that_it_is_permanent(make_transport_and_ctx):
    _, ctx = make_transport_and_ctx()
    result = await _call(devops_delete_variable_group, _delete_input(), ctx)

    assert result["deleted"] is True
    assert result["recoverable"] is False
    assert result["group_id"] == FAKE_GROUP_ID
    assert "no recycle bin" in result["note"]
    assert "cannot be restored" in result["note"]
    # Library items have no undelete path — nothing here may offer one.
    assert "devops_update" not in json.dumps(result)


async def test_delete_accepts_a_bodiless_2xx(make_transport_and_ctx):
    """Documented as 200 with a body; live it is a bodiless 204 (2026-08-26)."""
    _, ctx = make_transport_and_ctx(delete_status=204)
    result = await _call(devops_delete_variable_group, _delete_input(), ctx)
    assert result.get("error") is not True, result
    assert result["deleted"] is True


async def test_delete_accepts_a_200_with_a_body(make_transport_and_ctx):
    _, ctx = make_transport_and_ctx(delete_status=200, delete_body=_group_body())
    result = await _call(devops_delete_variable_group, _delete_input(), ctx)
    assert result.get("error") is not True, result
    assert result["deleted"] is True
    # Nothing is read back from the body — the group is gone.
    assert "variables" not in result


async def test_delete_does_not_refuse_a_key_vault_group(make_transport_and_ctx):
    """Deleting a KV-backed group is legitimate and touches nothing in the vault."""
    transport, ctx = make_transport_and_ctx(group=_group_body(group_type="AzureKeyVault"))
    result = await _call(devops_delete_variable_group, _delete_input(), ctx)

    assert result.get("error") is not True, result
    assert _requests_of(transport, "DELETE")


async def test_delete_leaks_no_planted_value(make_transport_and_ctx):
    _, ctx = make_transport_and_ctx()
    result = await _call(devops_delete_variable_group, _delete_input(), ctx)

    serialized = json.dumps(result)
    for planted in (PLANTED_PLAIN, FAKE_BEARER):
        assert planted not in serialized, f"PLANTED VALUE LEAKED: {planted!r}"


# ---------------------------------------------------------------------------
# Delete — errors
# ---------------------------------------------------------------------------


async def test_delete_pre_read_404_names_the_project(make_transport_and_ctx):
    transport, ctx = make_transport_and_ctx(group_status=404)
    result = await _call(devops_delete_variable_group, _delete_input(), ctx)

    assert result["error"] is True
    assert f"project '{FAKE_PROJECT}'" in result["message"]
    assert _requests_of(transport, "DELETE") == [], "nothing may be deleted after a failed pre-read"


async def test_delete_404_on_the_write_names_the_organization(make_transport_and_ctx):
    _, ctx = make_transport_and_ctx(delete_status=404)
    result = await _call(devops_delete_variable_group, _delete_input(), ctx)

    assert result["error"] is True
    assert f"organization '{FAKE_ORG}'" in result["message"]


async def test_delete_403_names_the_administrator_role(make_transport_and_ctx):
    _, ctx = make_transport_and_ctx(delete_status=403)
    result = await _call(devops_delete_variable_group, _delete_input(), ctx)

    assert result["error"] is True
    assert "Administrator" in result["message"]


async def test_delete_of_an_empty_group_body_reads_as_not_found(make_transport_and_ctx):
    transport, ctx = make_transport_and_ctx(group_body_override={})
    result = await _call(devops_delete_variable_group, _delete_input(), ctx)

    assert result["error"] is True
    assert "not found" in result["message"]
    assert _requests_of(transport, "DELETE") == []


async def test_delete_of_a_null_group_body_reads_as_not_found(make_transport_and_ctx):
    """The live "no such group" answer: HTTP 200 with a body of `null`.

    Verified live 2026-08-26 for both an unknown id and a group owned by another
    project — the pre-read never 404s on Azure DevOps Services, so this guard,
    not the 404 arm, is what stops a delete against a group we cannot see. It is
    also what a REPEATED delete hits, which is why the tool is not a silent
    no-op on a second call.
    """
    transport, ctx = make_transport_and_ctx(group_null=True)
    result = await _call(devops_delete_variable_group, _delete_input(), ctx)

    assert result["error"] is True
    assert "not found" in result["message"]
    assert f"project '{FAKE_PROJECT}'" in result["message"]
    assert _requests_of(transport, "DELETE") == []


@pytest.mark.parametrize("html_on", ["get", "delete"])
async def test_delete_reports_an_html_signin_page(make_transport_and_ctx, html_on):
    _, ctx = make_transport_and_ctx(html_on=html_on)
    result = await _call(devops_delete_variable_group, _delete_input(), ctx)

    assert result["error"] is True
    assert "HTML sign-in page" in result["message"]


async def test_delete_missing_org_is_a_validation_error(make_transport_and_ctx):
    _, ctx = make_transport_and_ctx()
    with patch(
        "devops_mcp.tools.variable_groups.resolve_org",
        side_effect=ValueError("No Azure DevOps organization provided."),
    ):
        result = json.loads(await devops_delete_variable_group(_delete_input(), ctx))

    assert result["error"] is True
    assert "organization" in result["message"]


# ---------------------------------------------------------------------------
# Registration gates (AZDO_ALLOW_WRITE / AZDO_ALLOW_DELETE)
# ---------------------------------------------------------------------------

# Both constants are evaluated once, at import time of devops_mcp._app, so they
# cannot be flipped inside a running interpreter without reloading modules and
# corrupting the shared registry other tests rely on. Probe a child process.
_WRITE_GATED = ("devops_set_variable_group_variables", "devops_create_variable_group")
_DELETE_GATED = ("devops_remove_variable_group_variables", "devops_delete_variable_group")

_PROBE = """
import asyncio, json
import devops_mcp.tools.variable_groups  # noqa: F401 — registration side effect
from devops_mcp._app import mcp

WATCHED = {
    "devops_set_variable_group_variables",
    "devops_create_variable_group",
    "devops_remove_variable_group_variables",
    "devops_delete_variable_group",
}
tools = asyncio.run(mcp.list_tools())
annotations = {}
for t in tools:
    if t.name in WATCHED:
        a = t.annotations
        annotations[t.name] = {
            "readOnlyHint": a.read_only_hint,
            "destructiveHint": a.destructive_hint,
            "idempotentHint": a.idempotent_hint,
            "openWorldHint": a.open_world_hint,
        }
print(json.dumps({"names": sorted(t.name for t in tools), "annotations": annotations}))
"""


def _probe_registry(*, allow_write: str | None = None, allow_delete: str | None = None) -> dict:
    env = dict(os.environ)
    env.pop("AZDO_ALLOW_WRITE", None)
    env.pop("AZDO_ALLOW_DELETE", None)
    if allow_write is not None:
        env["AZDO_ALLOW_WRITE"] = allow_write
    if allow_delete is not None:
        env["AZDO_ALLOW_DELETE"] = allow_delete
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert proc.returncode == 0, f"Probe failed: {proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_no_write_tool_is_registered_when_both_gates_are_unset():
    out = _probe_registry()

    for name in _WRITE_GATED + _DELETE_GATED:
        assert name not in out["names"], f"{name} must be gated"
    # Sanity: the module really did import and register its read tools, so the
    # assertions above are about the gates rather than a failed import.
    assert "devops_get_variable_group" in out["names"]
    assert "devops_list_variable_groups" in out["names"]


def test_allow_write_registers_only_the_write_tools():
    out = _probe_registry(allow_write="true")

    for name in _WRITE_GATED:
        assert name in out["names"]
    for name in _DELETE_GATED:
        assert name not in out["names"], f"{name} needs AZDO_ALLOW_DELETE, not AZDO_ALLOW_WRITE"


def test_allow_delete_registers_only_the_delete_tools():
    out = _probe_registry(allow_delete="true")

    for name in _DELETE_GATED:
        assert name in out["names"]
    for name in _WRITE_GATED:
        assert name not in out["names"], f"{name} needs AZDO_ALLOW_WRITE, not AZDO_ALLOW_DELETE"


def test_both_gates_register_all_four_with_truthful_annotations():
    out = _probe_registry(allow_write="true", allow_delete="true")

    assert out["annotations"] == {
        # Overwrites only what the caller named, and converges on re-send.
        "devops_set_variable_group_variables": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
        # A second call makes a second group (or 400s on the duplicate name).
        "devops_create_variable_group": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": True,
        },
        # Destructive AND idempotent — the two hints are orthogonal.
        "devops_remove_variable_group_variables": {
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": True,
            "openWorldHint": True,
        },
        # Idempotent in the sense the hint means — repeating the call adds no
        # further effect, the end state after one delete and after five is the
        # same — NOT in the sense of a repeat answering success: the second call
        # fails its pre-read (live: HTTP 200, body `null`) and returns an error.
        # devops_delete_work_item and devops_delete_query declare True on exactly
        # the same behaviour.
        "devops_delete_variable_group": {
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    }

"""Unit tests for the variable group write tools (set / remove variables).

Azure DevOps has no variable-level write API: the only update verb is a
FULL-REPLACE PUT of the whole group, so both tools do a read-merge-write. That
makes two things load-bearing and both are asserted on the wire here:

1. The merge is built from the RAW GET body. `_project_variable` removes a
   secret's value AND nulls any value whose *name* merely looks credential-like
   (`is_secret_name` matches bare 'key', 'auth', 'pat', 'sas'), so a merge built
   from the projected view would PUT redaction placeholders over live values.
   Tests plant a real value behind a heuristic-tripping name (`API_KEY`) and
   assert it survives the round-trip verbatim.
2. An untouched secret is re-sent as exactly `{"isSecret": true}` with the
   `value` key ABSENT — never `value: null`, whose meaning to the service is
   undocumented and could plausibly read as "clear it".

Routing is asymmetric and asserted too: the pre-read GET is PROJECT-scoped, the
PUT is ORGANIZATION-scoped (no project segment at all), with the resolved project
surviving only as `variableGroupProjectReferences` in the body — an array that
must be carried through unchanged, or the group is silently un-shared from every
other project that uses it.

All HTTP is intercepted by a capturing transport — no network, no credentials.
Gate-registration probes (AZDO_ALLOW_WRITE / AZDO_ALLOW_DELETE are read once at
import of devops_mcp._app) live with the create/delete tests.

Generic fake org/project identifiers are used throughout.
"""

import json
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from devops_mcp.models import (
    GetVariableGroupInput,
    RemoveVariableGroupVariablesInput,
    SetVariableGroupVariablesInput,
    VariableGroupVariableInput,
)
from devops_mcp.tools.variable_groups import (
    _build_update_body,
    _merge_variables,
    _project_group,
    _remove_variables,
    devops_get_variable_group,
    devops_remove_variable_group_variables,
    devops_set_variable_group_variables,
)

FAKE_ORG = "testorg"
FAKE_PROJECT = "TestProject"
FAKE_PROJECT_ID = "11111111-2222-3333-4444-555555555555"
OTHER_PROJECT_ID = "99999999-8888-7777-6666-555555555555"
FAKE_GROUP_ID = 42
FAKE_BEARER = "SUPER-SECRET-BEARER-TOKEN-should-never-appear-anywhere"

# Planted plaintext values. None of these may appear in a tool's returned JSON;
# the untouched ones MUST appear in the captured PUT body.
PLANTED_PLAIN = "PLANTED-PLAIN-a1b2c3"
PLANTED_APIKEY = "PLANTED-APIKEY-heuristic-d4e5f6"
PLANTED_READONLY = "PLANTED-READONLY-g7h8i9"
PLANTED_NEW_SECRET = "PLANTED-NEWSECRET-j1k2l3"


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


def _raw_variables() -> dict:
    """The RAW `variables` mapping as Azure DevOps returns it.

    - PLAIN: an ordinary value.
    - DEPLOY_SECRET: a secret — the server sends `value: null`.
    - API_KEY: a REAL plaintext value behind a name that trips is_secret_name().
      The read tools withhold it; the write path must carry it through verbatim.
    - READONLY_VAR: carries an extra flag that must survive the round-trip.
    """
    return {
        "PLAIN": {"value": PLANTED_PLAIN, "isSecret": False},
        "DEPLOY_SECRET": {"value": None, "isSecret": True},
        "API_KEY": {"value": PLANTED_APIKEY, "isSecret": False},
        "READONLY_VAR": {"value": PLANTED_READONLY, "isSecret": False, "isReadOnly": True},
    }


def _project_reference(project_id: str = FAKE_PROJECT_ID, name: str = FAKE_PROJECT) -> dict:
    return {
        "projectReference": {"id": project_id, "name": name},
        "name": "Shared Config",
        "description": "Shared configuration",
    }


def _group_body(
    *,
    variables: dict | None = None,
    references: list | None = None,
    group_type: str = "Vsts",
    include_references: bool = True,
) -> dict:
    body: dict = {
        "id": FAKE_GROUP_ID,
        "type": group_type,
        "name": "Shared Config",
        "description": "Shared configuration",
        "isShared": False,
        "createdBy": {"id": "aaaa", "displayName": "Test User", "uniqueName": "test@example.com"},
        "createdOn": "2026-01-01T00:00:00Z",
        "modifiedBy": {"id": "aaaa", "displayName": "Test User", "uniqueName": "test@example.com"},
        "modifiedOn": "2026-02-02T00:00:00Z",
        "variables": _raw_variables() if variables is None else variables,
    }
    if include_references:
        body["variableGroupProjectReferences"] = (
            [_project_reference()] if references is None else references
        )
    return body


def _project_body() -> dict:
    return {"id": FAKE_PROJECT_ID, "name": FAKE_PROJECT, "state": "wellFormed"}


def _make_handler(
    *,
    group: dict | None = None,
    group_status: int = 200,
    group_body_override=None,
    group_null: bool = False,
    put_status: int = 200,
    put_body=None,
    put_empty: bool = False,
    html_on: str | None = None,
    project_status: int = 200,
) -> Callable[[httpx.Request], httpx.Response]:
    """Route the three requests these tools can make onto canned responses."""
    group = _group_body() if group is None else group

    def _handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and "/_apis/projects/" in path:
            if project_status >= 400:
                return _json_response(project_status, {"message": "no such project"}, req)
            return _json_response(project_status, _project_body(), req)
        if req.method == "GET":
            if html_on == "get":
                return httpx.Response(
                    status_code=200,
                    headers={"Content-Type": "text/html; charset=utf-8"},
                    content=b"<html>sign in</html>",
                    request=req,
                )
            if group_null:
                # THE live shape for a bad group id: HTTP 200, body `null` — never
                # 404 (verified 2026-08-26 against Azure DevOps Services).
                return _json_response(200, None, req)
            body = group_body_override if group_body_override is not None else group
            if group_status >= 400:
                return _json_response(group_status, {"message": "boom"}, req)
            return _json_response(group_status, body, req)
        if req.method == "PUT":
            if html_on == "put":
                return httpx.Response(
                    status_code=200,
                    headers={"Content-Type": "text/html; charset=utf-8"},
                    content=b"<html>sign in</html>",
                    request=req,
                )
            if put_empty:
                return httpx.Response(status_code=put_status, request=req)
            if put_status >= 400:
                return _json_response(put_status, put_body or {"message": "boom"}, req)
            if put_body is not None:
                return _json_response(put_status, put_body, req)
            # Echo the submitted body back with the server-owned fields, the way
            # the real Update operation answers.
            sent = json.loads(req.content)
            echoed = {**group, **sent, "modifiedOn": "2026-03-03T00:00:00Z"}
            return _json_response(put_status, echoed, req)
        raise AssertionError(f"Unexpected request: {req.method} {req.url}")

    return _handler


@pytest.fixture()
def make_transport_and_ctx():
    """Factory fixture: build (transport, mcp_ctx) for a given canned scenario."""

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
    """Context managers that bypass real auth and org/project resolution."""
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


def _set_input(**kwargs) -> SetVariableGroupVariablesInput:
    kwargs.setdefault("group_id", FAKE_GROUP_ID)
    return SetVariableGroupVariablesInput(**kwargs)


def _remove_input(**kwargs) -> RemoveVariableGroupVariablesInput:
    kwargs.setdefault("group_id", FAKE_GROUP_ID)
    return RemoveVariableGroupVariablesInput(**kwargs)


def _requests_of(transport: CapturingTransport, method: str) -> list[httpx.Request]:
    return [r for r in transport.requests if r.method == method]


def _put_body(transport: CapturingTransport) -> dict:
    puts = _requests_of(transport, "PUT")
    assert len(puts) == 1, f"Expected exactly one PUT, got {len(puts)}"
    return json.loads(puts[0].content)


# ---------------------------------------------------------------------------
# The redaction boundary — the failure this whole feature is designed around
# ---------------------------------------------------------------------------


async def test_untouched_secret_round_trips_without_a_value_key(make_transport_and_ctx):
    """An untouched secret must be re-sent as exactly {"isSecret": true}."""
    transport, ctx = make_transport_and_ctx()
    result = await _call(
        devops_set_variable_group_variables,
        _set_input(variables=[{"name": "NEW_VAR", "value": "new-value"}]),
        ctx,
    )
    assert result.get("error") is not True, result

    secret = _put_body(transport)["variables"]["DEPLOY_SECRET"]
    assert secret == {"isSecret": True}
    assert "value" not in secret, "value: null would risk clearing the live secret"


async def test_merge_is_not_built_from_the_redacted_projection(make_transport_and_ctx):
    """A name-heuristic variable's REAL value must reach the PUT unharmed.

    API_KEY trips is_secret_name(), so devops_get_variable_group returns
    `value: null, redacted: 'name_heuristic'` for it. Building the merge from
    that projection would write the null over a live, ordinary value.
    """
    transport, ctx = make_transport_and_ctx()
    await _call(
        devops_set_variable_group_variables,
        _set_input(variables=[{"name": "NEW_VAR", "value": "new-value"}]),
        ctx,
    )

    variables = _put_body(transport)["variables"]
    assert variables["API_KEY"] == {"value": PLANTED_APIKEY, "isSecret": False}
    assert variables["PLAIN"] == {"value": PLANTED_PLAIN, "isSecret": False}


async def test_untouched_variables_are_byte_identical_to_the_get(make_transport_and_ctx):
    transport, ctx = make_transport_and_ctx()
    await _call(
        devops_set_variable_group_variables,
        _set_input(variables=[{"name": "PLAIN", "value": "changed"}]),
        ctx,
    )

    variables = _put_body(transport)["variables"]
    raw = _raw_variables()
    for name in ("API_KEY", "READONLY_VAR"):
        assert variables[name] == raw[name]


async def test_tool_output_leaks_no_planted_value(make_transport_and_ctx):
    """The returned JSON carries no plaintext, while the PUT body carries it."""
    transport, ctx = make_transport_and_ctx()
    result = await _call(
        devops_set_variable_group_variables,
        _set_input(variables=[{"name": "NEW_SECRET", "value": PLANTED_NEW_SECRET, "is_secret": True}]),
        ctx,
    )

    serialized = json.dumps(result)
    for planted in (PLANTED_PLAIN, PLANTED_APIKEY, PLANTED_READONLY, PLANTED_NEW_SECRET, FAKE_BEARER):
        assert planted not in serialized, f"PLANTED VALUE LEAKED: {planted!r}"

    sent = json.dumps(_put_body(transport))
    assert PLANTED_APIKEY in sent, "untouched heuristic value must survive the write"
    assert PLANTED_PLAIN in sent


def test_tripwire_raises_when_fed_a_projected_group():
    """_build_update_body must refuse the redacted projection outright."""
    raw = _group_body()
    projected = _project_group(raw, include_values=True)

    with pytest.raises(ValueError, match="REDACTED projection"):
        _build_update_body(projected, projected["variables"])


def test_tripwire_raises_on_projected_variable_entries():
    """A dict of projected ENTRIES (right container, wrong shape) is refused too."""
    projected_variables = {
        "DEPLOY_SECRET": {"name": "DEPLOY_SECRET", "is_secret": True, "value_available": False},
    }
    with pytest.raises(ValueError, match="projected key"):
        _build_update_body(_group_body(), projected_variables)


def test_tripwire_raises_when_the_source_group_is_projected():
    projected = _project_group(_group_body(), include_values=True)
    with pytest.raises(ValueError):
        _build_update_body(projected, {"PLAIN": {"value": "x", "isSecret": False}})


async def test_secret_demotion_without_a_value_is_refused(make_transport_and_ctx):
    """Silent secret destruction is refused before any write is issued."""
    transport, ctx = make_transport_and_ctx()
    result = await _call(
        devops_set_variable_group_variables,
        _set_input(variables=[{"name": "DEPLOY_SECRET", "is_secret": False}]),
        ctx,
    )

    assert result["error"] is True
    assert "value" in result["message"]
    assert _requests_of(transport, "PUT") == []


async def test_setting_a_secret_sends_the_value_with_the_flag(make_transport_and_ctx):
    transport, ctx = make_transport_and_ctx()
    await _call(
        devops_set_variable_group_variables,
        _set_input(variables=[{"name": "NEW_SECRET", "value": PLANTED_NEW_SECRET, "is_secret": True}]),
        ctx,
    )

    assert _put_body(transport)["variables"]["NEW_SECRET"] == {
        "isSecret": True,
        "value": PLANTED_NEW_SECRET,
    }


async def test_empty_value_on_an_existing_secret_is_refused(make_transport_and_ctx):
    """The false-success case: the service ignores an empty value on a secret.

    Live-verified 2026-08-26: `{"isSecret": true, "value": ""}` answers HTTP 200
    and echoes back `{"value": null, "isSecret": true}`, but a pipeline run
    afterwards still resolves the OLD value. Nothing is destroyed and nothing is
    written, so the only wrong thing left to do is report it as an update.
    Here `is_secret` is omitted: the flag is inherited from the stored entry, so
    only the merge can catch it.
    """
    transport, ctx = make_transport_and_ctx()
    result = await _call(
        devops_set_variable_group_variables,
        _set_input(variables=[{"name": "DEPLOY_SECRET", "value": ""}]),
        ctx,
    )

    assert result["error"] is True
    assert "DEPLOY_SECRET" in result["message"]
    assert "devops_remove_variable_group_variables" in result["message"]
    assert _requests_of(transport, "PUT") == []


def test_empty_value_with_an_explicit_secret_flag_is_refused_by_the_input_model():
    """is_secret=true + value='' never reaches the network at all."""
    with pytest.raises(ValueError, match="ignores an empty value on a secret"):
        _set_input(variables=[{"name": "ANY_NAME", "value": "", "is_secret": True}])


async def test_empty_value_on_a_plain_variable_is_written(make_transport_and_ctx):
    """Blanking a NON-secret variable is a real write and stays supported."""
    transport, ctx = make_transport_and_ctx()
    result = await _call(
        devops_set_variable_group_variables,
        _set_input(variables=[{"name": "PLAIN", "value": ""}]),
        ctx,
    )

    assert result.get("error") is not True, result
    assert result["variables_updated"] == ["PLAIN"]
    assert _put_body(transport)["variables"]["PLAIN"] == {"value": "", "isSecret": False}


async def test_empty_value_on_a_new_plain_variable_is_written(make_transport_and_ctx):
    transport, ctx = make_transport_and_ctx()
    await _call(
        devops_set_variable_group_variables,
        _set_input(variables=[{"name": "BRAND_NEW", "value": ""}]),
        ctx,
    )

    assert _put_body(transport)["variables"]["BRAND_NEW"] == {"value": "", "isSecret": False}


async def test_demoting_a_secret_to_an_empty_plain_value_is_allowed(make_transport_and_ctx):
    """The refusal is about SECRETS, not about empty strings.

    With is_secret=false the entry leaves as a plain variable, and an empty value
    on a plain variable is honoured, so this is a genuine write rather than a
    silently-ignored one.
    """
    transport, ctx = make_transport_and_ctx()
    result = await _call(
        devops_set_variable_group_variables,
        _set_input(variables=[{"name": "DEPLOY_SECRET", "value": "", "is_secret": False}]),
        ctx,
    )

    assert result.get("error") is not True, result
    assert _put_body(transport)["variables"]["DEPLOY_SECRET"] == {"value": "", "isSecret": False}


async def test_promoting_a_plain_variable_carries_its_existing_value(make_transport_and_ctx):
    """is_secret=true with no value encrypts the value already stored."""
    transport, ctx = make_transport_and_ctx()
    await _call(
        devops_set_variable_group_variables,
        _set_input(variables=[{"name": "PLAIN", "is_secret": True}]),
        ctx,
    )

    assert _put_body(transport)["variables"]["PLAIN"] == {
        "value": PLANTED_PLAIN,
        "isSecret": True,
    }


# ---------------------------------------------------------------------------
# Body construction — the VariableGroupParameters allowlist
# ---------------------------------------------------------------------------


async def test_put_body_carries_only_variable_group_parameters_keys(make_transport_and_ctx):
    transport, ctx = make_transport_and_ctx()
    await _call(
        devops_set_variable_group_variables,
        _set_input(variables=[{"name": "NEW_VAR", "value": "v"}]),
        ctx,
    )

    body = _put_body(transport)
    assert set(body) <= {
        "name",
        "description",
        "type",
        "providerData",
        "variableGroupProjectReferences",
        "variables",
    }
    for server_owned in ("id", "createdBy", "createdOn", "modifiedBy", "modifiedOn", "isShared"):
        assert server_owned not in body
    assert body["name"] == "Shared Config"
    assert body["description"] == "Shared configuration"
    assert body["type"] == "Vsts"


async def test_shared_project_references_survive_unchanged(make_transport_and_ctx):
    """A full replace replaces the sharing list too — never narrow it."""
    references = [
        _project_reference(),
        _project_reference(project_id=OTHER_PROJECT_ID, name="OtherProject"),
    ]
    transport, ctx = make_transport_and_ctx(group=_group_body(references=references))
    await _call(
        devops_set_variable_group_variables,
        _set_input(variables=[{"name": "NEW_VAR", "value": "v"}]),
        ctx,
    )

    assert _put_body(transport)["variableGroupProjectReferences"] == references


async def test_missing_project_references_are_synthesized_with_a_guid(make_transport_and_ctx):
    transport, ctx = make_transport_and_ctx(group=_group_body(include_references=False))
    await _call(
        devops_set_variable_group_variables,
        _set_input(variables=[{"name": "NEW_VAR", "value": "v"}]),
        ctx,
    )

    project_lookups = [r for r in transport.requests if "/_apis/projects/" in r.url.path]
    assert len(project_lookups) == 1
    assert project_lookups[0].url.path == f"/{FAKE_ORG}/_apis/projects/{FAKE_PROJECT}"

    references = _put_body(transport)["variableGroupProjectReferences"]
    assert references == [
        {
            "projectReference": {"id": FAKE_PROJECT_ID, "name": FAKE_PROJECT},
            "name": "Shared Config",
            "description": "Shared configuration",
        }
    ]


async def test_unresolvable_project_reference_fails_before_the_write(make_transport_and_ctx):
    transport, ctx = make_transport_and_ctx(
        group=_group_body(include_references=False), project_status=404
    )
    result = await _call(
        devops_set_variable_group_variables,
        _set_input(variables=[{"name": "NEW_VAR", "value": "v"}]),
        ctx,
    )

    assert result["error"] is True
    assert "Could not resolve project" in result["message"]
    assert _requests_of(transport, "PUT") == []


async def test_provider_data_is_absent_when_the_group_has_none(make_transport_and_ctx):
    transport, ctx = make_transport_and_ctx()
    await _call(
        devops_set_variable_group_variables,
        _set_input(variables=[{"name": "NEW_VAR", "value": "v"}]),
        ctx,
    )
    assert "providerData" not in _put_body(transport)


def test_build_update_body_carries_provider_data_verbatim():
    raw = _group_body()
    raw["providerData"] = {"vault": "kv", "serviceEndpointId": "abc", "undocumented": 1}
    body = _build_update_body(raw, _raw_variables())
    assert body["providerData"] == raw["providerData"]


def test_build_update_body_without_any_reference_refuses():
    with pytest.raises(ValueError, match="project reference"):
        _build_update_body(_group_body(include_references=False), _raw_variables())


# ---------------------------------------------------------------------------
# Routing — the reads and the writes are scoped differently
# ---------------------------------------------------------------------------


async def test_get_is_project_scoped_and_put_is_org_scoped(make_transport_and_ctx):
    transport, ctx = make_transport_and_ctx()
    await _call(
        devops_set_variable_group_variables,
        _set_input(variables=[{"name": "NEW_VAR", "value": "v"}]),
        ctx,
    )

    get_req = _requests_of(transport, "GET")[0]
    assert get_req.url.path == (
        f"/{FAKE_ORG}/{FAKE_PROJECT}/_apis/distributedtask/variablegroups/{FAKE_GROUP_ID}"
    )
    assert get_req.url.params["api-version"] == "7.1"

    put_req = _requests_of(transport, "PUT")[0]
    assert str(put_req.url) == (
        f"https://dev.azure.com/{FAKE_ORG}/_apis/distributedtask/"
        f"variablegroups/{FAKE_GROUP_ID}?api-version=7.1"
    )
    assert f"/{FAKE_PROJECT}/" not in put_req.url.path, "the PUT route has no project segment"


# ---------------------------------------------------------------------------
# Merge rules
# ---------------------------------------------------------------------------


async def test_upsert_preserves_the_stored_name_casing(make_transport_and_ctx):
    transport, ctx = make_transport_and_ctx()
    await _call(
        devops_set_variable_group_variables,
        _set_input(variables=[{"name": "plain", "value": "changed"}]),
        ctx,
    )

    variables = _put_body(transport)["variables"]
    assert variables["PLAIN"] == {"value": "changed", "isSecret": False}
    assert "plain" not in variables


async def test_new_variable_without_a_value_is_refused(make_transport_and_ctx):
    transport, ctx = make_transport_and_ctx()
    result = await _call(
        devops_set_variable_group_variables,
        _set_input(variables=[{"name": "GHOST", "is_secret": True}]),
        ctx,
    )

    assert result["error"] is True
    assert "GHOST" in result["message"]
    assert _requests_of(transport, "PUT") == []


async def test_set_reports_added_and_updated_names(make_transport_and_ctx):
    _, ctx = make_transport_and_ctx()
    result = await _call(
        devops_set_variable_group_variables,
        _set_input(
            variables=[
                {"name": "plain", "value": "changed"},
                {"name": "BRAND_NEW", "value": "v"},
            ]
        ),
        ctx,
    )

    assert result["variables_updated"] == ["PLAIN"]
    assert result["variables_added"] == ["BRAND_NEW"]


def test_case_colliding_stored_names_are_refused():
    with pytest.raises(ValueError, match="differ only by case"):
        _merge_variables(
            {"Foo": {"value": "a"}, "foo": {"value": "b"}},
            [VariableGroupVariableInput(name="FOO", value="c")],
            group_id=FAKE_GROUP_ID,
        )


def test_case_colliding_stored_names_are_refused_on_removal_too():
    """The removal path has its own ambiguity check — not just the merge path."""
    with pytest.raises(ValueError, match="differ only by case"):
        _remove_variables(
            {"Foo": {"value": "a"}, "foo": {"value": "b"}},
            ["FOO"],
            ignore_missing=False,
            group_id=FAKE_GROUP_ID,
        )


def test_ambiguous_removal_name_is_refused_even_under_ignore_missing():
    """ignore_missing downgrades ABSENT names, never an unresolvable collision."""
    with pytest.raises(ValueError, match="differ only by case"):
        _remove_variables(
            {"Foo": {"value": "a"}, "foo": {"value": "b"}},
            ["foO"],
            ignore_missing=True,
            group_id=FAKE_GROUP_ID,
        )


def test_duplicate_input_names_are_rejected_by_the_model():
    with pytest.raises(ValueError, match="Duplicate variable name"):
        _set_input(variables=[{"name": "Foo", "value": "a"}, {"name": "foo", "value": "b"}])


def test_blank_removal_name_is_rejected_by_the_model():
    with pytest.raises(ValueError):
        _remove_input(names=["PLAIN", "   "])


def test_removal_names_are_deduplicated_case_insensitively():
    assert _remove_input(names=["PLAIN", "plain", "API_KEY"]).names == ["PLAIN", "API_KEY"]


# ---------------------------------------------------------------------------
# Key Vault-backed groups
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("group_type", ["AzureKeyVault", "SomeFutureType"])
async def test_non_vsts_groups_are_refused_by_set(make_transport_and_ctx, group_type):
    transport, ctx = make_transport_and_ctx(group=_group_body(group_type=group_type))
    result = await _call(
        devops_set_variable_group_variables,
        _set_input(variables=[{"name": "NEW_VAR", "value": "v"}]),
        ctx,
    )

    assert result["error"] is True
    assert group_type in result["message"]
    assert _requests_of(transport, "PUT") == []


@pytest.mark.parametrize("group_type", ["AzureKeyVault", "SomeFutureType"])
async def test_non_vsts_groups_are_refused_by_remove(make_transport_and_ctx, group_type):
    """Allowlist, not denylist: an unknown future type is refused the same way."""
    transport, ctx = make_transport_and_ctx(group=_group_body(group_type=group_type))
    result = await _call(
        devops_remove_variable_group_variables,
        _remove_input(names=["PLAIN"]),
        ctx,
    )

    assert result["error"] is True
    assert group_type in result["message"]
    assert _requests_of(transport, "PUT") == []


# ---------------------------------------------------------------------------
# Removal behaviour
# ---------------------------------------------------------------------------


async def test_remove_drops_only_the_named_variables(make_transport_and_ctx):
    transport, ctx = make_transport_and_ctx()
    result = await _call(
        devops_remove_variable_group_variables,
        _remove_input(names=["plain"]),
        ctx,
    )

    assert result.get("error") is not True, result
    assert result["removed"] == ["PLAIN"]

    variables = _put_body(transport)["variables"]
    assert "PLAIN" not in variables
    assert variables["DEPLOY_SECRET"] == {"isSecret": True}
    assert variables["API_KEY"] == {"value": PLANTED_APIKEY, "isSecret": False}


async def test_remove_is_all_or_nothing(make_transport_and_ctx):
    transport, ctx = make_transport_and_ctx()
    result = await _call(
        devops_remove_variable_group_variables,
        _remove_input(names=["PLAIN", "NOPE"]),
        ctx,
    )

    assert result["error"] is True
    assert "NOPE" in result["message"]
    assert "ignore_missing" in result["message"]
    assert _requests_of(transport, "PUT") == []


async def test_remove_with_ignore_missing_skips_absent_names(make_transport_and_ctx):
    transport, ctx = make_transport_and_ctx()
    result = await _call(
        devops_remove_variable_group_variables,
        _remove_input(names=["PLAIN", "NOPE"], ignore_missing=True),
        ctx,
    )

    assert result["removed"] == ["PLAIN"]
    assert result["skipped_missing"] == ["NOPE"]
    assert "PLAIN" not in _put_body(transport)["variables"]


async def test_remove_all_missing_issues_no_write(make_transport_and_ctx):
    transport, ctx = make_transport_and_ctx()
    result = await _call(
        devops_remove_variable_group_variables,
        _remove_input(names=["NOPE", "ALSO_NOPE"], ignore_missing=True),
        ctx,
    )

    assert result["removed"] == []
    assert result["skipped_missing"] == ["NOPE", "ALSO_NOPE"]
    assert _requests_of(transport, "PUT") == []
    assert result["variable_count"] == 4


async def test_removing_the_last_variable_is_passed_through(make_transport_and_ctx):
    single = {"ONLY": {"value": "v", "isSecret": False}}
    transport, ctx = make_transport_and_ctx(group=_group_body(variables=single))
    result = await _call(
        devops_remove_variable_group_variables,
        _remove_input(names=["ONLY"]),
        ctx,
    )

    assert result.get("error") is not True, result
    assert _put_body(transport)["variables"] == {}


# ---------------------------------------------------------------------------
# Response shape
# ---------------------------------------------------------------------------


async def test_write_response_matches_the_get_tool_shape(make_transport_and_ctx):
    transport, ctx = make_transport_and_ctx()
    written = await _call(
        devops_set_variable_group_variables,
        _set_input(variables=[{"name": "NEW_VAR", "value": "v"}]),
        ctx,
    )

    _, read_ctx = make_transport_and_ctx()
    read = await _call(
        devops_get_variable_group,
        GetVariableGroupInput(group_id=FAKE_GROUP_ID, include_values=False),
        read_ctx,
    )

    assert set(read) <= set(written)
    assert set(written) - set(read) == {"variables_added", "variables_updated"}
    # No plaintext comes back: the only 'value' key the projection can emit at
    # include_values=False is the name-heuristic's explicit null.
    assert all(v.get("value") is None for v in written["variables"])
    assert _requests_of(transport, "PUT")


async def test_empty_write_response_is_still_a_success(make_transport_and_ctx):
    _, ctx = make_transport_and_ctx(put_empty=True, put_status=204)
    result = await _call(
        devops_set_variable_group_variables,
        _set_input(variables=[{"name": "NEW_VAR", "value": "v"}]),
        ctx,
    )

    assert result.get("error") is not True, result
    assert result["updated"] is True
    assert result["id"] == FAKE_GROUP_ID


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


async def test_401_names_the_manage_scope(make_transport_and_ctx):
    _, ctx = make_transport_and_ctx(group_status=401)
    result = await _call(
        devops_set_variable_group_variables,
        _set_input(variables=[{"name": "NEW_VAR", "value": "v"}]),
        ctx,
    )
    assert result["error"] is True
    assert "vso.variablegroups_manage" in result["message"]


async def test_403_names_the_administrator_role(make_transport_and_ctx):
    _, ctx = make_transport_and_ctx(put_status=403)
    result = await _call(
        devops_set_variable_group_variables,
        _set_input(variables=[{"name": "NEW_VAR", "value": "v"}]),
        ctx,
    )
    assert result["error"] is True
    assert "Administrator" in result["message"]
    assert FAKE_PROJECT in result["message"]


async def test_404_on_the_pre_read_names_the_project(make_transport_and_ctx):
    _, ctx = make_transport_and_ctx(group_status=404)
    result = await _call(
        devops_set_variable_group_variables,
        _set_input(variables=[{"name": "NEW_VAR", "value": "v"}]),
        ctx,
    )
    assert result["error"] is True
    assert f"project '{FAKE_PROJECT}'" in result["message"]


async def test_404_on_the_write_names_the_organization(make_transport_and_ctx):
    _, ctx = make_transport_and_ctx(put_status=404)
    result = await _call(
        devops_set_variable_group_variables,
        _set_input(variables=[{"name": "NEW_VAR", "value": "v"}]),
        ctx,
    )
    assert result["error"] is True
    assert f"organization '{FAKE_ORG}'" in result["message"]


async def test_empty_get_body_reads_as_not_found(make_transport_and_ctx):
    transport, ctx = make_transport_and_ctx(group_body_override={})
    result = await _call(
        devops_set_variable_group_variables,
        _set_input(variables=[{"name": "NEW_VAR", "value": "v"}]),
        ctx,
    )
    assert result["error"] is True
    assert "not found" in result["message"]
    assert _requests_of(transport, "PUT") == []


async def test_null_get_body_reads_as_not_found_on_the_write_path(make_transport_and_ctx):
    """The live shape of "no such group": HTTP 200 with a body of `null`.

    Verified live 2026-08-26 — an unknown group id and a group belonging to
    another project both answer this way, never 404. The 404 arms in the error
    mappers are on-prem insurance; THIS is the path that runs.
    """
    transport, ctx = make_transport_and_ctx(group_null=True)
    result = await _call(
        devops_set_variable_group_variables,
        _set_input(variables=[{"name": "NEW_VAR", "value": "v"}]),
        ctx,
    )
    assert result["error"] is True
    assert "not found" in result["message"]
    assert f"project '{FAKE_PROJECT}'" in result["message"]
    assert _requests_of(transport, "PUT") == [], "nothing may be written after a null pre-read"


async def test_null_get_body_reads_as_not_found_on_the_read_tool(make_transport_and_ctx):
    _, ctx = make_transport_and_ctx(group_null=True)
    result = await _call(
        devops_get_variable_group,
        GetVariableGroupInput(group_id=FAKE_GROUP_ID),
        ctx,
    )
    assert result["error"] is True
    assert f"Variable group {FAKE_GROUP_ID} not found" in result["message"]


async def test_removing_every_variable_is_refused_with_actionable_guidance(
    make_transport_and_ctx,
):
    """Live: HTTP 400 ArgumentException 'Variable group must have at least one
    variable defined.' The removal does not apply, so say what to do instead."""
    single = {"ONLY": {"value": "v", "isSecret": False}}
    transport, ctx = make_transport_and_ctx(
        group=_group_body(variables=single),
        put_status=400,
        put_body={
            "message": "Variable group must have at least one variable defined.",
            "typeKey": "ArgumentException",
        },
    )
    result = await _call(
        devops_remove_variable_group_variables,
        _remove_input(names=["ONLY"]),
        ctx,
    )

    assert result["error"] is True
    assert "at least one variable" in result["message"]
    assert "nothing was removed" in result["message"]
    # The actionable part: delete the group if that was the intent.
    assert "devops_delete_variable_group" in result["message"]
    assert _requests_of(transport, "PUT"), "the refusal comes from the service, not from us"


async def test_400_missing_project_reference_reads_as_an_internal_bug(make_transport_and_ctx):
    _, ctx = make_transport_and_ctx(
        put_status=400,
        put_body={"message": "Atleast one variable group project reference is required"},
    )
    result = await _call(
        devops_set_variable_group_variables,
        _set_input(variables=[{"name": "NEW_VAR", "value": "v"}]),
        ctx,
    )
    assert result["error"] is True
    assert "devops-mcp" in result["message"]


async def test_other_400_echoes_the_service_message(make_transport_and_ctx):
    _, ctx = make_transport_and_ctx(put_status=400, put_body={"message": "Something specific"})
    result = await _call(
        devops_set_variable_group_variables,
        _set_input(variables=[{"name": "NEW_VAR", "value": "v"}]),
        ctx,
    )
    assert result["error"] is True
    assert "Something specific" in result["message"]


@pytest.mark.parametrize("html_on", ["get", "put"])
async def test_html_signin_page_is_reported(make_transport_and_ctx, html_on):
    _, ctx = make_transport_and_ctx(html_on=html_on)
    result = await _call(
        devops_set_variable_group_variables,
        _set_input(variables=[{"name": "NEW_VAR", "value": "v"}]),
        ctx,
    )
    assert result["error"] is True
    assert "HTML sign-in page" in result["message"]

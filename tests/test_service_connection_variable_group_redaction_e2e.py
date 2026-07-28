"""Adversarial runtime verification for service_connections.py / variable_groups.py.

Drives the actual @mcp.tool() functions with a mocked httpx transport carrying
planted fake secrets, and scans the full serialized JSON output for those
planted strings. Written by an independent tester, not part of the repo.
"""

import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from devops_mcp.models import (
    GetServiceConnectionInput,
    GetVariableGroupInput,
    ListServiceConnectionsInput,
    ListVariableGroupsInput,
)
from devops_mcp.tools.service_connections import (
    devops_get_service_connection,
    devops_list_service_connections,
)
from devops_mcp.tools.variable_groups import (
    devops_get_variable_group,
    devops_list_variable_groups,
)

FAKE_ORG = "fake-org"
FAKE_PROJECT = "fake-project"
FAKE_ENDPOINT_ID = "aaaaaaaa-0000-0000-0000-000000000001"
FAKE_GROUP_ID = 42
FAKE_BEARER = "SUPER-SECRET-BEARER-TOKEN-should-never-appear-anywhere"

PLANTED_PASSWORD = "PLANTED-PASSWORD-9f3a7c21"
PLANTED_APITOKEN = "PLANTED-APITOKEN-8e21bb90"
PLANTED_SPKEY = "PLANTED-SPKEY-11cd44aa"
PLANTED_NOVEL = "PLANTED-NOVELPARAM-77ffab12"
PLANTED_DATA_SECRET = "PLANTED-DATASECRET-aa11bb22"
PLANTED_DATA_CERT = "PLANTED-DATACERT-cc33dd44"
PLANTED_VAR_SECRET_VALUE = "PLANTED-VARSECRET-plaintext-leak-556677"
PLANTED_DB_PASSWORD_VALUE = "PLANTED-DBPASSWORD-plaintext-889900"
PLANTED_SASTOKEN_VALUE = "PLANTED-SASTOKEN-plaintext-112233"
PLANTED_PROVIDERDATA_SECRET = "PLANTED-PROVIDERDATA-secretpointer-445566"
PLANTED_HTML_TOKEN = "PLANTED-HTML-SESSION-TOKEN-778899"

ALL_PLANTED = [
    PLANTED_PASSWORD,
    PLANTED_APITOKEN,
    PLANTED_SPKEY,
    PLANTED_NOVEL,
    PLANTED_DATA_SECRET,
    PLANTED_DATA_CERT,
    PLANTED_VAR_SECRET_VALUE,
    PLANTED_DB_PASSWORD_VALUE,
    PLANTED_SASTOKEN_VALUE,
    PLANTED_PROVIDERDATA_SECRET,
    PLANTED_HTML_TOKEN,
]


def assert_no_planted_secrets(haystack, planted=None):
    for secret in (planted or ALL_PLANTED):
        assert secret not in haystack, "PLANTED SECRET LEAKED: " + repr(secret) + " found in output"


def _json_response(status, body, request, content_type="application/json"):
    return httpx.Response(
        status_code=status,
        headers={"Content-Type": content_type},
        content=json.dumps(body).encode(),
        request=request,
    )


def _html_response(status, html, request):
    return httpx.Response(
        status_code=status,
        headers={"Content-Type": "text/html; charset=utf-8"},
        content=html.encode(),
        request=request,
    )


class CapturingTransport(httpx.AsyncBaseTransport):
    def __init__(self, handler):
        self._handler = handler
        self.requests = []

    async def handle_async_request(self, request):
        self.requests.append(request)
        return self._handler(request)


def _make_app_ctx():
    app_ctx = MagicMock()
    app_ctx.organization = FAKE_ORG
    app_ctx.project = FAKE_PROJECT
    app_ctx._token_cache = {}
    return app_ctx


def _make_mock_ctx(app_ctx):
    ctx = MagicMock()
    ctx.request_context.lifespan_context = app_ctx
    return ctx


def _build_client(handler):
    transport = CapturingTransport(handler)
    return transport, httpx.AsyncClient(transport=transport)


def _auth_patches(module_path):
    fake_headers = {"Authorization": "Bearer " + FAKE_BEARER, "Accept": "application/json"}
    return [
        patch(module_path + ".build_headers", new=AsyncMock(return_value=fake_headers)),
        patch(module_path + ".resolve_org", return_value=FAKE_ORG),
        patch(module_path + ".resolve_project", return_value=FAKE_PROJECT),
    ]


SC_MODULE = "devops_mcp.tools.service_connections"
VG_MODULE = "devops_mcp.tools.variable_groups"


async def _run_with_patches(module_path, coro_fn, *args):
    patches = _auth_patches(module_path)
    for p in patches:
        p.start()
    try:
        return await coro_fn(*args)
    finally:
        for p in patches:
            p.stop()


def _adversarial_endpoint_payload():
    return {
        "id": FAKE_ENDPOINT_ID,
        "name": "my-connection",
        "type": "fictional-endpoint-type-v9",
        "url": "https://example.invalid",
        "description": "adversarial test endpoint",
        "isReady": True,
        "isShared": False,
        "owner": "Library",
        "authorization": {
            "scheme": "ServicePrincipal",
            "parameters": {
                "tenantId": "tenant-123",
                "serviceprincipalid": "spn-456",
                "password": PLANTED_PASSWORD,
                "apitoken": PLANTED_APITOKEN,
                "serviceprincipalkey": PLANTED_SPKEY,
                "vendorNovelSecretField": PLANTED_NOVEL,
            },
        },
        "data": {
            "subscriptionId": "sub-1",
            "scopeLevel": "Subscription",
            "clientSecret": PLANTED_DATA_SECRET,
            "certificate": PLANTED_DATA_CERT,
        },
        "operationStatus": {"state": "Ready", "statusMessage": "ok"},
        "createdBy": {"id": "u1", "displayName": "Jane", "uniqueName": "jane@example.com"},
        "serviceEndpointProjectReferences": [],
    }


@pytest.mark.asyncio
async def test_get_service_connection_redacts_known_and_novel_auth_params():
    payload = _adversarial_endpoint_payload()

    def handler(req):
        assert "serviceendpoint/endpoints" in req.url.path
        return _json_response(200, payload, req)

    transport, http_client = _build_client(handler)
    app_ctx = _make_app_ctx()
    app_ctx.http_client = http_client
    ctx = _make_mock_ctx(app_ctx)

    params = GetServiceConnectionInput(
        organization=FAKE_ORG, project=FAKE_PROJECT, endpoint_id=FAKE_ENDPOINT_ID, include_data=True
    )
    result_json = await _run_with_patches(SC_MODULE, devops_get_service_connection, params, ctx)

    assert_no_planted_secrets(result_json)

    result = json.loads(result_json)
    assert "error" not in result, result

    assert result["auth_parameters"] == {"tenantId": "tenant-123", "serviceprincipalid": "spn-456"}
    dropped = set(result["auth_parameters_dropped"])
    assert dropped == {"password", "apitoken", "serviceprincipalkey", "vendorNovelSecretField"}

    assert result["data"] == {"subscriptionId": "sub-1", "scopeLevel": "Subscription"}
    assert set(result["data_dropped"]) == {"clientSecret", "certificate"}


@pytest.mark.asyncio
async def test_list_service_connections_never_touches_auth_bag():
    payload = _adversarial_endpoint_payload()

    def handler(req):
        return _json_response(200, {"count": 1, "value": [payload]}, req)

    transport, http_client = _build_client(handler)
    app_ctx = _make_app_ctx()
    app_ctx.http_client = http_client
    ctx = _make_mock_ctx(app_ctx)

    params = ListServiceConnectionsInput(organization=FAKE_ORG, project=FAKE_PROJECT)
    result_json = await _run_with_patches(SC_MODULE, devops_list_service_connections, params, ctx)

    assert_no_planted_secrets(result_json)
    result = json.loads(result_json)
    assert "error" not in result, result
    assert result["count"] == 1
    item = result["service_connections"][0]
    assert "auth_parameters" not in item
    assert "auth_parameters_dropped" not in item
    assert "data" not in item
    assert "data_dropped" not in item
    assert item["auth_scheme"] == "ServicePrincipal"


def _adversarial_group_payload(extra_provider_data=None):
    provider_data = {"vault": "my-vault", "serviceEndpointId": "ep-1", "lastRefreshedOn": "2026-01-01T00:00:00Z"}
    if extra_provider_data:
        provider_data.update(extra_provider_data)
    return {
        "id": FAKE_GROUP_ID,
        "name": "Prod-Secrets",
        "type": "AzureKeyVault",
        "description": "adversarial test group",
        "isShared": False,
        "createdOn": "2026-01-01T00:00:00Z",
        "createdBy": {"id": "u1", "displayName": "Jane", "uniqueName": "jane@example.com"},
        "modifiedOn": "2026-01-02T00:00:00Z",
        "modifiedBy": {"id": "u1", "displayName": "Jane", "uniqueName": "jane@example.com"},
        "providerData": provider_data,
        "variableGroupProjectReferences": [],
        "variables": {
            "MARKED_SECRET_BUT_LEAKY": {
                "isSecret": True,
                "isReadOnly": False,
                "value": PLANTED_VAR_SECRET_VALUE,
            },
            "DB_PASSWORD": {
                "isSecret": False,
                "isReadOnly": False,
                "value": PLANTED_DB_PASSWORD_VALUE,
            },
            "sasToken": {
                "isSecret": False,
                "isReadOnly": False,
                "value": PLANTED_SASTOKEN_VALUE,
            },
            "ENVIRONMENT": {
                "isSecret": False,
                "isReadOnly": False,
                "value": "staging",
            },
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("include_values", [True, False])
async def test_get_variable_group_secret_value_key_absent(include_values):
    payload = _adversarial_group_payload(extra_provider_data={"secretPointer": PLANTED_PROVIDERDATA_SECRET})

    def handler(req):
        return _json_response(200, payload, req)

    transport, http_client = _build_client(handler)
    app_ctx = _make_app_ctx()
    app_ctx.http_client = http_client
    ctx = _make_mock_ctx(app_ctx)

    params = GetVariableGroupInput(
        organization=FAKE_ORG, project=FAKE_PROJECT, group_id=FAKE_GROUP_ID, include_values=include_values
    )
    result_json = await _run_with_patches(VG_MODULE, devops_get_variable_group, params, ctx)

    assert_no_planted_secrets(result_json)
    result = json.loads(result_json)
    assert "error" not in result, result

    variables = {v["name"]: v for v in result["variables"]}

    secret_var = variables["MARKED_SECRET_BUT_LEAKY"]
    assert "value" not in secret_var, "'value' key must be ABSENT, got: " + repr(secret_var)
    assert secret_var["value_available"] is False
    assert secret_var["is_secret"] is True

    db_pw = variables["DB_PASSWORD"]
    assert db_pw["value"] is None
    assert db_pw["redacted"] == "name_heuristic"

    sas = variables["sasToken"]
    assert sas["value"] is None
    assert sas["redacted"] == "name_heuristic"

    env = variables["ENVIRONMENT"]
    if include_values:
        assert env["value"] == "staging"
    else:
        assert "value" not in env

    assert result["provider_data"] == {
        "vault": "my-vault",
        "service_endpoint_id": "ep-1",
        "last_refreshed_on": "2026-01-01T00:00:00Z",
    }
    assert "secretPointer" not in result_json

    assert result["redacted_variable_count"] == 2
    assert result["secret_variable_count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("include_values", [True, False])
async def test_list_variable_groups_secret_value_key_absent(include_values):
    payload = _adversarial_group_payload()

    def handler(req):
        return _json_response(200, {"count": 1, "value": [payload]}, req)

    transport, http_client = _build_client(handler)
    app_ctx = _make_app_ctx()
    app_ctx.http_client = http_client
    ctx = _make_mock_ctx(app_ctx)

    params = ListVariableGroupsInput(
        organization=FAKE_ORG, project=FAKE_PROJECT, include_values=include_values
    )
    result_json = await _run_with_patches(VG_MODULE, devops_list_variable_groups, params, ctx)

    assert_no_planted_secrets(result_json)
    result = json.loads(result_json)
    assert "error" not in result, result

    group = result["variable_groups"][0]
    variables = {v["name"]: v for v in group["variables"]}

    secret_var = variables["MARKED_SECRET_BUT_LEAKY"]
    assert "value" not in secret_var
    assert secret_var["value_available"] is False

    db_pw = variables["DB_PASSWORD"]
    assert db_pw["value"] is None
    assert db_pw["redacted"] == "name_heuristic"


@pytest.mark.asyncio
async def test_list_service_connections_comma_joins_array_filters():
    captured_urls = []

    def handler(req):
        captured_urls.append(req.url)
        return _json_response(200, {"count": 0, "value": []}, req)

    transport, http_client = _build_client(handler)
    app_ctx = _make_app_ctx()
    app_ctx.http_client = http_client
    ctx = _make_mock_ctx(app_ctx)

    params = ListServiceConnectionsInput(
        organization=FAKE_ORG,
        project=FAKE_PROJECT,
        names=["conn-a", "conn-b", "conn-c"],
        auth_schemes=["ServicePrincipal", "Token"],
    )
    await _run_with_patches(SC_MODULE, devops_list_service_connections, params, ctx)

    assert len(captured_urls) == 1
    url = captured_urls[0]
    endpoint_names_values = url.params.get_list("endpointNames")
    auth_schemes_values = url.params.get_list("authSchemes")
    assert endpoint_names_values == ["conn-a,conn-b,conn-c"], endpoint_names_values
    assert auth_schemes_values == ["ServicePrincipal,Token"], auth_schemes_values


@pytest.mark.asyncio
async def test_variable_groups_pagination_token_roundtrip():
    page1_body = {
        "count": 1,
        "value": [
            {
                "id": 1,
                "name": "Group-Page1",
                "type": "Vsts",
                "description": None,
                "isShared": False,
                "createdOn": None,
                "modifiedOn": None,
                "variables": {},
            }
        ],
    }
    page2_body = {
        "count": 1,
        "value": [
            {
                "id": 2,
                "name": "Group-Page2",
                "type": "Vsts",
                "description": None,
                "isShared": False,
                "createdOn": None,
                "modifiedOn": None,
                "variables": {},
            }
        ],
    }

    captured_requests = []

    def handler(req):
        captured_requests.append(req)
        if len(captured_requests) == 1:
            return httpx.Response(
                status_code=200,
                headers={"Content-Type": "application/json", "x-ms-continuationtoken": "42"},
                content=json.dumps(page1_body).encode(),
                request=req,
            )
        return _json_response(200, page2_body, req)

    transport, http_client = _build_client(handler)
    app_ctx = _make_app_ctx()
    app_ctx.http_client = http_client
    ctx = _make_mock_ctx(app_ctx)

    params1 = ListVariableGroupsInput(organization=FAKE_ORG, project=FAKE_PROJECT)
    result1_json = await _run_with_patches(VG_MODULE, devops_list_variable_groups, params1, ctx)
    result1 = json.loads(result1_json)
    assert result1["continuation_token"] == "42"
    assert result1["variable_groups"][0]["name"] == "Group-Page1"

    params2 = ListVariableGroupsInput(
        organization=FAKE_ORG, project=FAKE_PROJECT, continuation_token=result1["continuation_token"]
    )
    result2_json = await _run_with_patches(VG_MODULE, devops_list_variable_groups, params2, ctx)
    result2 = json.loads(result2_json)
    assert "continuation_token" not in result2
    assert result2["variable_groups"][0]["name"] == "Group-Page2"

    assert len(captured_requests) == 2
    second_req_params = dict(captured_requests[1].url.params)
    assert second_req_params.get("continuationToken") == "42"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 404])
async def test_get_service_connection_error_status_codes(status, caplog):
    def handler(req):
        return _json_response(status, {"message": "denied", "typeKey": "SomeException"}, req)

    transport, http_client = _build_client(handler)
    app_ctx = _make_app_ctx()
    app_ctx.http_client = http_client
    ctx = _make_mock_ctx(app_ctx)

    params = GetServiceConnectionInput(organization=FAKE_ORG, project=FAKE_PROJECT, endpoint_id=FAKE_ENDPOINT_ID)
    with caplog.at_level(logging.DEBUG):
        result_json = await _run_with_patches(SC_MODULE, devops_get_service_connection, params, ctx)

    result = json.loads(result_json)
    assert result["error"] is True
    assert isinstance(result["message"], str) and len(result["message"]) > 0

    assert FAKE_BEARER not in result_json
    for record in caplog.records:
        assert FAKE_BEARER not in record.getMessage()


@pytest.mark.asyncio
async def test_get_variable_group_html_signin_page_not_dumped():
    html = "<html><head><title>Sign in</title></head><body>Please sign in. Session: " + PLANTED_HTML_TOKEN + "</body></html>"

    def handler(req):
        return _html_response(200, html, req)

    transport, http_client = _build_client(handler)
    app_ctx = _make_app_ctx()
    app_ctx.http_client = http_client
    ctx = _make_mock_ctx(app_ctx)

    params = GetVariableGroupInput(organization=FAKE_ORG, project=FAKE_PROJECT, group_id=FAKE_GROUP_ID)
    result_json = await _run_with_patches(VG_MODULE, devops_get_variable_group, params, ctx)

    result = json.loads(result_json)
    assert result["error"] is True
    assert PLANTED_HTML_TOKEN not in result_json
    assert "<html" not in result_json.lower()
    assert "sign in" in result["message"].lower() or "sign-in" in result["message"].lower()
    assert FAKE_BEARER not in result_json


@pytest.mark.asyncio
async def test_list_service_connections_html_signin_page_not_dumped():
    html = "<html><body>Sign in required. token=" + PLANTED_HTML_TOKEN + "</body></html>"

    def handler(req):
        return _html_response(203, html, req)

    transport, http_client = _build_client(handler)
    app_ctx = _make_app_ctx()
    app_ctx.http_client = http_client
    ctx = _make_mock_ctx(app_ctx)

    params = ListServiceConnectionsInput(organization=FAKE_ORG, project=FAKE_PROJECT)
    result_json = await _run_with_patches(SC_MODULE, devops_list_service_connections, params, ctx)

    result = json.loads(result_json)
    assert result["error"] is True
    assert PLANTED_HTML_TOKEN not in result_json
    assert "<html" not in result_json.lower()
    assert FAKE_BEARER not in result_json

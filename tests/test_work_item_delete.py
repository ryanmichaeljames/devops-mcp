"""Unit tests for devops_delete_work_item.

This is the only destructive tool in the server, so the tests cover both the
safety gate around it and the two very different operations it can perform.

Contract under test (Work Items - Delete, api-version 7.2-preview.3):
`DELETE {org}/{project}/_apis/wit/workitems/{id}?destroy={bool}&api-version=...`

- The default (destroy=False) sends the work item to the project's RECYCLE BIN
  and is recoverable; destroy=True permanently destroys it and is not.
- `destroy` must actually reach the wire — a tool that accepted the parameter
  and quietly dropped it would either silently refuse a requested permanent
  delete or, far worse in the other direction, be one refactor away from
  sending true when the caller asked for false. Both directions are asserted.
- Microsoft's reference documents a `200 OK` carrying a WorkItemDelete body
  (id/name/type/project/deletedBy/deletedDate/resource) while its own sample
  response is a bodiless `204` — so a successful delete must be reported as a
  success under both shapes, with the optional fields surfaced when present and
  simply absent when not.
- 404 must read as "not found, and it may already be deleted" (a retried DELETE
  legitimately lands here); 403 must name the specific project-level permission,
  which differs between the recycle-bin delete and the destroy.

All HTTP is intercepted via a capturing transport — no network, no credentials.
Registration is proven in a child process because the AZDO_ALLOW_DELETE gate is
read once, at import time of devops_mcp._app.

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

from devops_mcp.models import DeleteWorkItemInput
from devops_mcp.tools.work_items import _WIT_API_VERSION, devops_delete_work_item

FAKE_ORG = "testorg"
FAKE_PROJECT = "TestProject"
FAKE_WI_ID = 101


class CapturingTransport(httpx.AsyncBaseTransport):
    """Intercept every HTTP request; dispatch to a handler."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self._handler = handler
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)


def _delete_body() -> dict:
    """A WorkItemDelete response body as documented by Microsoft."""
    return {
        "id": FAKE_WI_ID,
        "code": 200,
        "type": "Task",
        "name": "Scratch item from a live test run",
        "project": FAKE_PROJECT,
        "deletedBy": "Test User",
        "deletedDate": "2026-08-04T09:00:00Z",
        "url": f"https://dev.azure.com/{FAKE_ORG}/_apis/wit/recyclebin/{FAKE_WI_ID}",
        "resource": {
            "id": FAKE_WI_ID,
            "rev": 3,
            "fields": {"System.WorkItemType": "Task", "System.Title": "Scratch item"},
        },
    }


def _make_handler(
    status: int = 200,
    body: dict | None = None,
    *,
    empty: bool = False,
) -> Callable[[httpx.Request], httpx.Response]:
    def _handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "DELETE", f"Unexpected method: {req.method}"
        if empty:
            return httpx.Response(status_code=status, request=req)
        return httpx.Response(
            status_code=status,
            headers={"Content-Type": "application/json"},
            content=json.dumps(body if body is not None else _delete_body()).encode(),
            request=req,
        )

    return _handler


@pytest.fixture()
def make_transport_and_ctx():
    """Factory fixture: build (transport, mcp_ctx) for a given canned response."""

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
    fake_headers = {"Authorization": "Bearer fake-token", "Accept": "application/json"}
    return [
        patch("devops_mcp.tools.work_items.build_headers", new=AsyncMock(return_value=fake_headers)),
        patch("devops_mcp.tools.work_items.resolve_org", return_value=FAKE_ORG),
        patch("devops_mcp.tools.work_items.resolve_project", return_value=FAKE_PROJECT),
    ]


async def _call(params, mcp_ctx) -> dict:
    patches = _auth_patches()
    for p in patches:
        p.start()
    try:
        return json.loads(await devops_delete_work_item(params, mcp_ctx))
    finally:
        for p in patches:
            p.stop()


def _only_request(transport: CapturingTransport) -> httpx.Request:
    assert len(transport.requests) == 1, f"Expected exactly one request, got {len(transport.requests)}"
    return transport.requests[0]


# ---------------------------------------------------------------------------
# Recycle bin (default) vs destroy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_delete_goes_to_recycle_bin(make_transport_and_ctx):
    """The default delete is recoverable and says so, and sends destroy=false."""
    transport, mcp_ctx = make_transport_and_ctx()

    params = DeleteWorkItemInput(
        organization=FAKE_ORG,
        project=FAKE_PROJECT,
        work_item_id=FAKE_WI_ID,
    )
    result = await _call(params, mcp_ctx)

    assert "error" not in result, f"Unexpected error: {result}"
    assert result["deleted"] is True
    assert result["destroyed"] is False
    assert result["recoverable"] is True
    assert result["work_item_id"] == FAKE_WI_ID
    assert result["work_item_type"] == "Task"
    assert result["title"] == "Scratch item from a live test run"
    assert result["deleted_by"] == "Test User"
    assert result["deleted_date"] == "2026-08-04T09:00:00Z"
    assert "recycle bin" in result["message"].lower()

    request = _only_request(transport)
    assert request.method == "DELETE"
    assert request.url.path == f"/{FAKE_ORG}/{FAKE_PROJECT}/_apis/wit/workitems/{FAKE_WI_ID}"
    assert request.url.params["api-version"] == _WIT_API_VERSION
    # The default must never ask for a permanent destroy.
    assert request.url.params["destroy"] == "false"


@pytest.mark.asyncio
async def test_destroy_true_sends_destroy_query_param(make_transport_and_ctx):
    """destroy=True must actually reach the wire as destroy=true and be reported back."""
    transport, mcp_ctx = make_transport_and_ctx()

    params = DeleteWorkItemInput(
        organization=FAKE_ORG,
        project=FAKE_PROJECT,
        work_item_id=FAKE_WI_ID,
        destroy=True,
    )
    result = await _call(params, mcp_ctx)

    assert "error" not in result, f"Unexpected error: {result}"
    assert result["deleted"] is True
    assert result["destroyed"] is True
    assert result["recoverable"] is False
    assert "PERMANENTLY DESTROYED" in result["message"]
    assert "did not go to the recycle bin" in result["message"]

    request = _only_request(transport)
    assert request.url.params["destroy"] == "true"
    assert request.url.params["api-version"] == _WIT_API_VERSION


@pytest.mark.asyncio
async def test_destroy_defaults_to_false_when_omitted():
    """The input model's destroy flag defaults to the recoverable mode."""
    params = DeleteWorkItemInput(work_item_id=FAKE_WI_ID)
    assert params.destroy is False


# ---------------------------------------------------------------------------
# Response shapes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_204_empty_body_is_still_a_success(make_transport_and_ctx):
    """The documented sample response is a bodiless 204 — that is a success, not an error."""
    transport, mcp_ctx = make_transport_and_ctx(status=204, empty=True)

    params = DeleteWorkItemInput(
        organization=FAKE_ORG,
        project=FAKE_PROJECT,
        work_item_id=FAKE_WI_ID,
    )
    result = await _call(params, mcp_ctx)

    assert "error" not in result, f"Unexpected error: {result}"
    assert result["deleted"] is True
    assert result["destroyed"] is False
    assert result["work_item_id"] == FAKE_WI_ID
    assert result["project"] == FAKE_PROJECT
    # Nothing to report, so nothing is invented.
    assert "title" not in result
    assert "work_item_type" not in result


@pytest.mark.asyncio
async def test_falls_back_to_resource_fields_for_type_and_title(make_transport_and_ctx):
    """When the envelope omits name/type, they are read off the embedded work item."""
    body = _delete_body()
    del body["name"]
    del body["type"]
    transport, mcp_ctx = make_transport_and_ctx(body=body)

    params = DeleteWorkItemInput(
        organization=FAKE_ORG,
        project=FAKE_PROJECT,
        work_item_id=FAKE_WI_ID,
    )
    result = await _call(params, mcp_ctx)

    assert "error" not in result, f"Unexpected error: {result}"
    assert result["work_item_type"] == "Task"
    assert result["title"] == "Scratch item"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_404_reports_not_found_and_possible_prior_deletion(make_transport_and_ctx):
    """A 404 must be actionable: wrong id, wrong project, or already deleted."""
    transport, mcp_ctx = make_transport_and_ctx(
        status=404,
        body={
            "message": "TF401232: Work item 101 does not exist, or you do not have permissions to read it.",
            "typeKey": "WorkItemNotFoundException",
        },
    )

    params = DeleteWorkItemInput(
        organization=FAKE_ORG,
        project=FAKE_PROJECT,
        work_item_id=FAKE_WI_ID,
    )
    result = await _call(params, mcp_ctx)

    assert result.get("error") is True
    assert f"Work item {FAKE_WI_ID} was not found" in result["message"]
    assert "already have been deleted" in result["message"]
    assert "recycle bin" in result["message"].lower()


@pytest.mark.asyncio
async def test_403_names_the_delete_and_restore_permission(make_transport_and_ctx):
    """A 403 on a recycle-bin delete points at the ordinary delete permission."""
    transport, mcp_ctx = make_transport_and_ctx(
        status=403,
        body={"message": "Access denied.", "typeKey": "AccessCheckException"},
    )

    params = DeleteWorkItemInput(
        organization=FAKE_ORG,
        project=FAKE_PROJECT,
        work_item_id=FAKE_WI_ID,
    )
    result = await _call(params, mcp_ctx)

    assert result.get("error") is True
    assert "Not authorized (HTTP 403)" in result["message"]
    assert "Delete and restore work items" in result["message"]
    assert "vso.work_write" in result["message"]


@pytest.mark.asyncio
async def test_403_on_destroy_names_the_permanent_delete_permission(make_transport_and_ctx):
    """A 403 on destroy=true points at the elevated 'Permanently delete' permission."""
    transport, mcp_ctx = make_transport_and_ctx(
        status=403,
        body={"message": "Access denied.", "typeKey": "AccessCheckException"},
    )

    params = DeleteWorkItemInput(
        organization=FAKE_ORG,
        project=FAKE_PROJECT,
        work_item_id=FAKE_WI_ID,
        destroy=True,
    )
    result = await _call(params, mcp_ctx)

    assert result.get("error") is True
    assert "Permanently delete work items" in result["message"]
    assert "Project Administrators" in result["message"]


@pytest.mark.asyncio
async def test_other_http_error_is_reported_as_json(make_transport_and_ctx):
    """Any other status still returns JSON rather than raising."""
    transport, mcp_ctx = make_transport_and_ctx(
        status=400,
        body={"message": "Bad request.", "typeKey": "RuleValidationException"},
    )

    params = DeleteWorkItemInput(
        organization=FAKE_ORG,
        project=FAKE_PROJECT,
        work_item_id=FAKE_WI_ID,
    )
    result = await _call(params, mcp_ctx)

    assert result.get("error") is True
    assert "HTTP 400" in result["message"]


@pytest.mark.asyncio
async def test_missing_org_is_a_validation_error_not_an_exception():
    """resolve_org's ValueError is caught and returned as JSON."""
    app_ctx = MagicMock()
    app_ctx.organization = None
    app_ctx.project = None
    mcp_ctx = MagicMock()
    mcp_ctx.request_context.lifespan_context = app_ctx

    params = DeleteWorkItemInput(work_item_id=FAKE_WI_ID)
    with patch(
        "devops_mcp.tools.work_items.resolve_org",
        side_effect=ValueError("No Azure DevOps organization provided."),
    ):
        result = json.loads(await devops_delete_work_item(params, mcp_ctx))

    assert result.get("error") is True
    assert "organization" in result["message"]


# ---------------------------------------------------------------------------
# Registration gate (AZDO_ALLOW_DELETE)
# ---------------------------------------------------------------------------

# The gate is evaluated once, at import time of devops_mcp._app, so it cannot be
# flipped inside a running interpreter without reloading modules and corrupting
# the shared registry other tests rely on. Probe it in a child process instead.
_PROBE = """
import asyncio, json
import devops_mcp.tools.work_items  # noqa: F401 — registration side effect
from devops_mcp._app import mcp

tools = asyncio.run(mcp.list_tools())
delete_tools = [t for t in tools if t.name == "devops_delete_work_item"]
out = {"names": sorted(t.name for t in tools), "annotations": None}
if delete_tools:
    a = delete_tools[0].annotations
    out["annotations"] = {
        "readOnlyHint": a.read_only_hint,
        "destructiveHint": a.destructive_hint,
        "idempotentHint": a.idempotent_hint,
        "openWorldHint": a.open_world_hint,
    }
print(json.dumps(out))
"""


def _probe_registry(allow_delete: str | None) -> dict:
    env = dict(os.environ)
    env.pop("AZDO_ALLOW_DELETE", None)
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


def test_delete_tool_is_not_registered_when_gate_unset():
    """With AZDO_ALLOW_DELETE unset the tool is invisible to the agent entirely."""
    out = _probe_registry(None)

    assert "devops_delete_work_item" not in out["names"]
    # Sanity: the module really did register its other tools, so the assertion
    # above is about the gate rather than a failed import.
    assert "devops_get_work_item" in out["names"]


def test_delete_tool_is_registered_when_gate_true():
    """AZDO_ALLOW_DELETE=true registers the tool with truthful destructive annotations."""
    out = _probe_registry("true")

    assert "devops_delete_work_item" in out["names"]
    assert out["annotations"] == {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    }

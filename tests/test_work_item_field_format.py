"""Unit tests for large-text work item field format handling.

Azure DevOps saves large-text fields (description, acceptance criteria, repro
steps, the discussion comment) as HTML unless the patch document also carries a
`/multilineFieldsFormat/{fieldRef}` op set to "Markdown".

Acceptance criteria verified here:
- devops_create_work_item / devops_update_work_item emit that op for every
  large-text field they write, by default.
- format='html' suppresses the op entirely (Azure DevOps keeps its HTML default).
- Fields that are not large-text (title, state, custom fields of unknown type)
  never get the op — Azure DevOps rejects it for them.

All HTTP is intercepted via a capturing transport — no network required.
"""

import json
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from devops_mcp.models import CreateWorkItemInput, UpdateWorkItemInput
from devops_mcp.tools.work_items import devops_create_work_item, devops_update_work_item

FAKE_ORG = "fake-org"
FAKE_PROJECT = "fake-project"
FAKE_WI_ID = 101

MARKDOWN_OP_PREFIX = "/multilineFieldsFormat/"


def _json_response(status: int, body: dict, request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        headers={"Content-Type": "application/json"},
        content=json.dumps(body).encode(),
        request=request,
    )


class CapturingTransport(httpx.AsyncBaseTransport):
    """Intercept every HTTP request; dispatch to a handler."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self._handler = handler
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)


def _handler(req: httpx.Request) -> httpx.Response:
    if req.method in ("POST", "PATCH") and "/wit/workitems" in req.url.path:
        return _json_response(200, {"id": FAKE_WI_ID, "rev": 1, "fields": {}}, req)
    raise AssertionError(f"Unexpected request: {req.method} {req.url}")


@pytest.fixture()
def transport_and_ctx():
    """Return (transport, mcp_ctx) with a capturing HTTP client."""
    transport = CapturingTransport(_handler)
    app_ctx = MagicMock()
    app_ctx.organization = FAKE_ORG
    app_ctx.project = FAKE_PROJECT
    app_ctx.http_client = httpx.AsyncClient(transport=transport)
    mcp_ctx = MagicMock()
    mcp_ctx.request_context.lifespan_context = app_ctx
    return transport, mcp_ctx


def _auth_patches():
    """Context managers that bypass real auth and org/project resolution."""
    fake_headers = {"Authorization": "Bearer fake-token", "Accept": "application/json"}
    return [
        patch("devops_mcp.tools.work_items.build_headers", new=AsyncMock(return_value=fake_headers)),
        patch("devops_mcp.tools.work_items.resolve_org", return_value=FAKE_ORG),
        patch("devops_mcp.tools.work_items.resolve_project", return_value=FAKE_PROJECT),
    ]


async def _call_and_get_ops(tool, params, transport, mcp_ctx) -> list[dict]:
    """Invoke *tool* and return the JSON Patch ops it sent."""
    patches = _auth_patches()
    for p in patches:
        p.start()
    try:
        result = json.loads(await tool(params, mcp_ctx))
    finally:
        for p in patches:
            p.stop()
    assert "error" not in result, f"Unexpected error: {result}"
    assert len(transport.requests) == 1
    return json.loads(transport.requests[0].content)


def _format_ops(ops: list[dict]) -> dict[str, str]:
    """Map field ref → format value for every multilineFieldsFormat op."""
    return {
        op["path"].removeprefix(MARKDOWN_OP_PREFIX): op["value"]
        for op in ops
        if op["path"].startswith(MARKDOWN_OP_PREFIX)
    }


@pytest.mark.asyncio
async def test_create_marks_description_as_markdown(transport_and_ctx):
    """A created description must be flagged markdown, and only the description."""
    transport, mcp_ctx = transport_and_ctx

    params = CreateWorkItemInput(
        organization=FAKE_ORG,
        project=FAKE_PROJECT,
        work_item_type="Task",
        title="**not** markdown — title is plain text",
        description="# heading\n\n- item",
    )
    ops = await _call_and_get_ops(devops_create_work_item, params, transport, mcp_ctx)

    assert _format_ops(ops) == {"System.Description": "Markdown"}


@pytest.mark.asyncio
async def test_create_with_html_format_emits_no_format_ops(transport_and_ctx):
    """format='html' must leave Azure DevOps on its HTML default (no format ops)."""
    transport, mcp_ctx = transport_and_ctx

    params = CreateWorkItemInput(
        organization=FAKE_ORG,
        project=FAKE_PROJECT,
        work_item_type="Task",
        title="html work item",
        description="<h1>heading</h1>",
        format="html",
    )
    ops = await _call_and_get_ops(devops_create_work_item, params, transport, mcp_ctx)

    assert _format_ops(ops) == {}


@pytest.mark.asyncio
async def test_update_marks_description_and_discussion_as_markdown(transport_and_ctx):
    """Both the description and the System.History comment must be flagged markdown."""
    transport, mcp_ctx = transport_and_ctx

    params = UpdateWorkItemInput(
        organization=FAKE_ORG,
        project=FAKE_PROJECT,
        work_item_id=FAKE_WI_ID,
        description="## updated",
        comment="**discussion**",
        state="Active",
    )
    ops = await _call_and_get_ops(devops_update_work_item, params, transport, mcp_ctx)

    assert _format_ops(ops) == {
        "System.Description": "Markdown",
        "System.History": "Markdown",
    }


@pytest.mark.asyncio
async def test_known_large_text_field_via_additional_fields_is_marked(transport_and_ctx):
    """A known large-text field supplied through additional_fields is flagged too."""
    transport, mcp_ctx = transport_and_ctx

    params = UpdateWorkItemInput(
        organization=FAKE_ORG,
        project=FAKE_PROJECT,
        work_item_id=FAKE_WI_ID,
        additional_fields={
            "Microsoft.VSTS.Common.AcceptanceCriteria": "- [ ] passes",
            "Microsoft.VSTS.Scheduling.StoryPoints": 5,
            "Custom.SomeField": "value",
        },
    )
    ops = await _call_and_get_ops(devops_update_work_item, params, transport, mcp_ctx)

    # Story points is not a text field and Custom.SomeField's type is unknown, so
    # neither may carry a format op — Azure DevOps rejects it for non-text fields.
    assert _format_ops(ops) == {"Microsoft.VSTS.Common.AcceptanceCriteria": "Markdown"}


@pytest.mark.asyncio
async def test_update_without_large_text_fields_emits_no_format_ops(transport_and_ctx):
    """An update touching no large-text field must not emit any format op."""
    transport, mcp_ctx = transport_and_ctx

    params = UpdateWorkItemInput(
        organization=FAKE_ORG,
        project=FAKE_PROJECT,
        work_item_id=FAKE_WI_ID,
        state="Closed",
        tags="done",
    )
    ops = await _call_and_get_ops(devops_update_work_item, params, transport, mcp_ctx)

    assert _format_ops(ops) == {}
    assert len(ops) == 2

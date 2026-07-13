"""Unit tests for work item comment format handling.

Acceptance criteria verified here:
- devops_add_work_item_comment sends `format=markdown` by default.
- devops_update_work_item_comment sends `format=markdown` by default.
- An explicit format='html' is passed through unchanged.
- Both tools call an api-version that honours the `format` query parameter
  (7.1-preview.4 or later — older versions silently store HTML).

All HTTP is intercepted via a capturing transport — no network required.
Generic fake org/project identifiers are used throughout.
"""

import json
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from devops_mcp.models import AddWorkItemCommentInput, UpdateWorkItemCommentInput
from devops_mcp.tools.work_items import (
    devops_add_work_item_comment,
    devops_update_work_item_comment,
)

FAKE_ORG = "fake-org"
FAKE_PROJECT = "fake-project"
FAKE_WI_ID = 101
FAKE_COMMENT_ID = 50


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
    if req.method in ("POST", "PATCH") and "/comments" in req.url.path:
        body = {
            "workItemId": FAKE_WI_ID,
            "commentId": FAKE_COMMENT_ID,
            "version": 1,
            "text": json.loads(req.content)["text"],
            "format": req.url.params.get("format"),
        }
        return _json_response(200, body, req)
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


async def _call(tool, params, mcp_ctx) -> dict:
    patches = _auth_patches()
    for p in patches:
        p.start()
    try:
        return json.loads(await tool(params, mcp_ctx))
    finally:
        for p in patches:
            p.stop()


def _assert_format_supported(request: httpx.Request) -> None:
    """The api-version must be one that honours `format` (7.1-preview.4+)."""
    api_version = request.url.params.get("api-version")
    assert api_version is not None, "api-version query parameter missing"
    assert api_version >= "7.1-preview.4", (
        f"api-version {api_version!r} predates the `format` query parameter; "
        "comments would be stored as HTML"
    )


@pytest.mark.asyncio
async def test_add_comment_defaults_to_markdown(transport_and_ctx):
    """Adding a comment without an explicit format must send format=markdown."""
    transport, mcp_ctx = transport_and_ctx

    params = AddWorkItemCommentInput(
        organization=FAKE_ORG,
        project=FAKE_PROJECT,
        work_item_id=FAKE_WI_ID,
        text="**bold**",
    )
    result = await _call(devops_add_work_item_comment, params, mcp_ctx)
    assert "error" not in result, f"Unexpected error: {result}"

    assert len(transport.requests) == 1
    req = transport.requests[0]
    assert req.method == "POST"
    assert req.url.params.get("format") == "markdown"
    _assert_format_supported(req)


@pytest.mark.asyncio
async def test_add_comment_honours_explicit_html(transport_and_ctx):
    """An explicit format='html' must be passed through unchanged."""
    transport, mcp_ctx = transport_and_ctx

    params = AddWorkItemCommentInput(
        organization=FAKE_ORG,
        project=FAKE_PROJECT,
        work_item_id=FAKE_WI_ID,
        text="<b>bold</b>",
        format="html",
    )
    result = await _call(devops_add_work_item_comment, params, mcp_ctx)
    assert "error" not in result, f"Unexpected error: {result}"

    assert transport.requests[0].url.params.get("format") == "html"


@pytest.mark.asyncio
async def test_update_comment_defaults_to_markdown(transport_and_ctx):
    """Updating a comment without an explicit format must send format=markdown."""
    transport, mcp_ctx = transport_and_ctx

    params = UpdateWorkItemCommentInput(
        organization=FAKE_ORG,
        project=FAKE_PROJECT,
        work_item_id=FAKE_WI_ID,
        comment_id=FAKE_COMMENT_ID,
        text="- item one\n- item two",
    )
    result = await _call(devops_update_work_item_comment, params, mcp_ctx)
    assert "error" not in result, f"Unexpected error: {result}"

    assert len(transport.requests) == 1
    req = transport.requests[0]
    assert req.method == "PATCH"
    assert req.url.params.get("format") == "markdown"
    _assert_format_supported(req)


@pytest.mark.asyncio
async def test_update_comment_honours_explicit_html(transport_and_ctx):
    """An explicit format='html' on update must be passed through unchanged."""
    transport, mcp_ctx = transport_and_ctx

    params = UpdateWorkItemCommentInput(
        organization=FAKE_ORG,
        project=FAKE_PROJECT,
        work_item_id=FAKE_WI_ID,
        comment_id=FAKE_COMMENT_ID,
        text="<ul><li>item</li></ul>",
        format="html",
    )
    result = await _call(devops_update_work_item_comment, params, mcp_ctx)
    assert "error" not in result, f"Unexpected error: {result}"

    assert transport.requests[0].url.params.get("format") == "html"


@pytest.mark.asyncio
async def test_invalid_format_is_rejected_by_the_model():
    """Only 'markdown' and 'html' are accepted formats."""
    with pytest.raises(ValueError):
        AddWorkItemCommentInput(
            organization=FAKE_ORG,
            project=FAKE_PROJECT,
            work_item_id=FAKE_WI_ID,
            text="hello",
            format="rst",
        )

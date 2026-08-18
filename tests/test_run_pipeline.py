"""Unit tests for devops_run_pipeline's POST body construction.

The Run Pipeline endpoint takes a RunPipelineParameters body whose optional
members (`resources`, `templateParameters`, `variables`, `stagesToSkip`) are
absent-by-default: every one of them is only written when the corresponding
input is not None, so a caller that omits a field sends no key at all and gets
the pipeline's own defaults.

Acceptance criteria verified here:
- `stagesToSkip` reaches the wire under exactly that key, with the supplied
  stage names in order, when stages_to_skip is provided.
- The `stagesToSkip` key is ABSENT from the body when stages_to_skip is
  omitted — the pre-existing wire shape is unchanged for existing callers.
- An explicit empty list is forwarded as `"stagesToSkip": []` (skip nothing),
  matching how the other optional members treat an explicitly-empty value:
  the None/not-None distinction is the only thing that controls key presence.
- stages_to_skip composes with branch/template_parameters/variables rather
  than displacing them.

All HTTP is intercepted via an httpx.AsyncBaseTransport stub — no network,
no credentials. Generic fake org/project identifiers are used throughout.
"""

import json
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from devops_mcp.models import RunPipelineInput
from devops_mcp.tools.pipelines import devops_run_pipeline

FAKE_ORG = "fake-org"
FAKE_PROJECT = "fake-project"
FAKE_PIPELINE_ID = 42
FAKE_RUN_ID = 4242


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
    if req.method == "POST" and f"/pipelines/{FAKE_PIPELINE_ID}/runs" in req.url.path:
        return _json_response(
            200,
            {
                "id": FAKE_RUN_ID,
                "state": "inProgress",
                "_links": {"web": {"href": "https://dev.azure.com/fake"}},
            },
            req,
        )
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
        patch("devops_mcp.tools.pipelines.build_headers", new=AsyncMock(return_value=fake_headers)),
        patch("devops_mcp.tools.pipelines.resolve_org", return_value=FAKE_ORG),
        patch("devops_mcp.tools.pipelines.resolve_project", return_value=FAKE_PROJECT),
    ]


async def _call(params: RunPipelineInput, mcp_ctx) -> dict:
    patches = _auth_patches()
    for p in patches:
        p.start()
    try:
        return json.loads(await devops_run_pipeline(params, mcp_ctx))
    finally:
        for p in patches:
            p.stop()


def _post_body(transport: CapturingTransport) -> dict:
    posts = [r for r in transport.requests if r.method == "POST"]
    assert len(posts) == 1, f"Expected exactly one POST, got {len(posts)}"
    return json.loads(posts[0].content)


@pytest.mark.asyncio
async def test_stages_to_skip_reaches_the_wire(transport_and_ctx):
    """Supplied stage names are sent under the `stagesToSkip` key, in order."""
    transport, mcp_ctx = transport_and_ctx

    params = RunPipelineInput(
        organization=FAKE_ORG,
        project=FAKE_PROJECT,
        pipeline_id=FAKE_PIPELINE_ID,
        stages_to_skip=["Deploy", "IntegrationTests"],
    )
    result = await _call(params, mcp_ctx)

    assert "error" not in result, f"Unexpected error: {result}"
    body = _post_body(transport)
    assert body["stagesToSkip"] == ["Deploy", "IntegrationTests"]


@pytest.mark.asyncio
async def test_stages_to_skip_absent_when_omitted(transport_and_ctx):
    """Omitting stages_to_skip leaves the key out of the body entirely."""
    transport, mcp_ctx = transport_and_ctx

    params = RunPipelineInput(
        organization=FAKE_ORG,
        project=FAKE_PROJECT,
        pipeline_id=FAKE_PIPELINE_ID,
    )
    result = await _call(params, mcp_ctx)

    assert "error" not in result, f"Unexpected error: {result}"
    body = _post_body(transport)
    assert "stagesToSkip" not in body, (
        f"stagesToSkip must not appear when unset; got keys: {list(body.keys())}"
    )
    assert body == {}, f"An input with no overrides must post an empty body; got {body}"


@pytest.mark.asyncio
async def test_stages_to_skip_empty_list_is_sent_as_empty_array(transport_and_ctx):
    """An explicit empty list is forwarded as `[]` — skip nothing, run every stage.

    Key presence tracks None vs not-None, exactly as it does for the other
    optional members, so an explicit `[]` is not silently rewritten into an
    omission (nor an omission into an `[]`).
    """
    transport, mcp_ctx = transport_and_ctx

    params = RunPipelineInput(
        organization=FAKE_ORG,
        project=FAKE_PROJECT,
        pipeline_id=FAKE_PIPELINE_ID,
        stages_to_skip=[],
    )
    result = await _call(params, mcp_ctx)

    assert "error" not in result, f"Unexpected error: {result}"
    body = _post_body(transport)
    assert body["stagesToSkip"] == []


@pytest.mark.asyncio
async def test_stages_to_skip_composes_with_other_overrides(transport_and_ctx):
    """stages_to_skip is additive — branch, templateParameters and variables survive."""
    transport, mcp_ctx = transport_and_ctx

    params = RunPipelineInput(
        organization=FAKE_ORG,
        project=FAKE_PROJECT,
        pipeline_id=FAKE_PIPELINE_ID,
        branch="main",
        template_parameters={"environment": "staging"},
        variables={"MY_VAR": "my_value"},
        stages_to_skip=["Deploy"],
    )
    result = await _call(params, mcp_ctx)

    assert "error" not in result, f"Unexpected error: {result}"
    body = _post_body(transport)
    assert body["resources"] == {"repositories": {"self": {"refName": "refs/heads/main"}}}
    assert body["templateParameters"] == {"environment": "staging"}
    assert body["variables"] == {"MY_VAR": {"value": "my_value", "isSecret": False}}
    assert body["stagesToSkip"] == ["Deploy"]

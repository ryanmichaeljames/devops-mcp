"""Unit tests for devops_create_pull_request work-item linking behaviour.

Acceptance criteria verified here:
- The PR create POST body NEVER contains a `workItemRefs` key regardless of
  whether work_item_ids is supplied.
- When work_item_ids is supplied, a PATCH to `wit/workitems/{id}` carrying an
  ArtifactLink relation add-op is issued for each work item ID after the PR
  is created.
- When work_item_ids is empty/absent, no WIT PATCH requests are issued.

All HTTP is intercepted via httpx.MockTransport — no network required.
Generic fake org/project/GUIDs are used throughout; no real identifiers.
"""

import json
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

# ---------------------------------------------------------------------------
# Fake infrastructure
# ---------------------------------------------------------------------------

FAKE_ORG = "fake-org"
FAKE_PROJECT = "fake-project"
FAKE_REPO_ID = "aaaaaaaa-0000-0000-0000-000000000001"
FAKE_PROJECT_ID = "bbbbbbbb-0000-0000-0000-000000000002"
FAKE_PR_ID = 42
FAKE_WI_ID_1 = 101
FAKE_WI_ID_2 = 102

# The resolved repository GUID returned by the repo GET (matches what
# _build_pull_request_artifact_uri will encode into the artifact URI).
FAKE_RESOLVED_REPO_ID = "cccccccc-0000-0000-0000-000000000003"

BASE_URL = f"https://dev.azure.com/{FAKE_ORG}/{FAKE_PROJECT}/_apis"


def _json_response(status: int, body: dict, request: httpx.Request) -> httpx.Response:
    """Build an httpx.Response with JSON body."""
    return httpx.Response(
        status_code=status,
        headers={"Content-Type": "application/json"},
        content=json.dumps(body).encode(),
        request=request,
    )


def _make_app_ctx() -> MagicMock:
    """Return a minimal AppContext mock with a captured-request AsyncClient."""
    app_ctx = MagicMock()
    app_ctx.organization = FAKE_ORG
    app_ctx.project = FAKE_PROJECT
    app_ctx._token_cache = {}
    return app_ctx


def _make_mock_ctx(app_ctx) -> MagicMock:
    """Wrap app_ctx in a FastMCP Context-like mock."""
    ctx = MagicMock()
    ctx.request_context.lifespan_context = app_ctx
    return ctx


# ---------------------------------------------------------------------------
# Request-capture transport
# ---------------------------------------------------------------------------

class CapturingTransport(httpx.AsyncBaseTransport):
    """Intercept every HTTP request; dispatch to a per-method/path handler."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self._handler = handler
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)


# ---------------------------------------------------------------------------
# Helpers to build the mock response sequence
# ---------------------------------------------------------------------------

def _build_handler(transport: CapturingTransport) -> Callable[[httpx.Request], httpx.Response]:
    """
    Return a handler that serves deterministic fake responses based on URL path:

    POST  .../pullrequests           → created PR (id=FAKE_PR_ID)
    GET   .../git/repositories/...  → repo details (id, project.id)
    GET   .../wit/workitems/{id}     → work item with no existing relations
    PATCH .../wit/workitems/{id}     → patched work item (204-ish, 200 body)
    """
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path

        # PR create
        if req.method == "POST" and "pullrequests" in path:
            body = {
                "pullRequestId": FAKE_PR_ID,
                "title": "Test PR",
                "status": "active",
            }
            return _json_response(201, body, req)

        # Repository GET
        if req.method == "GET" and "/git/repositories/" in path and "pullrequests" not in path:
            body = {
                "id": FAKE_RESOLVED_REPO_ID,
                "project": {"id": FAKE_PROJECT_ID},
            }
            return _json_response(200, body, req)

        # Work item GET (expand=relations)
        if req.method == "GET" and "/wit/workitems/" in path:
            wi_id = path.rstrip("/").split("/")[-1]
            body = {"id": int(wi_id), "relations": []}
            return _json_response(200, body, req)

        # Work item PATCH (ArtifactLink)
        if req.method == "PATCH" and "/wit/workitems/" in path:
            wi_id = path.rstrip("/").split("/")[-1]
            body = {"id": int(wi_id), "relations": [{"rel": "ArtifactLink"}]}
            return _json_response(200, body, req)

        # Unexpected request — fail fast so tests surface stray calls
        raise AssertionError(f"Unexpected request: {req.method} {req.url}")

    return handler


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def transport_and_ctx():
    """Return (transport, app_ctx, mcp_ctx) with a capturing HTTP client."""
    app_ctx = _make_app_ctx()
    transport = CapturingTransport(lambda req: _json_response(500, {}, req))  # placeholder
    http_client = httpx.AsyncClient(transport=transport)
    transport._handler = _build_handler(transport)
    app_ctx.http_client = http_client
    mcp_ctx = _make_mock_ctx(app_ctx)
    return transport, app_ctx, mcp_ctx


# ---------------------------------------------------------------------------
# Helpers: patch build_headers and resolve_* to avoid real auth
# ---------------------------------------------------------------------------

PATCH_BUILD_HEADERS = "devops_mcp.tools.pull_requests.build_headers"
PATCH_RESOLVE_ORG = "devops_mcp.tools.pull_requests.resolve_org"
PATCH_RESOLVE_PROJECT = "devops_mcp.tools.pull_requests.resolve_project"


def _auth_patches():
    """Context managers that bypass real auth and org/project resolution."""
    fake_headers = {"Authorization": "Bearer fake-token", "Accept": "application/json"}
    return [
        patch(PATCH_BUILD_HEADERS, new=AsyncMock(return_value=fake_headers)),
        patch(PATCH_RESOLVE_ORG, return_value=FAKE_ORG),
        patch(PATCH_RESOLVE_PROJECT, return_value=FAKE_PROJECT),
    ]


# ---------------------------------------------------------------------------
# Import the function under test after patching infrastructure
# ---------------------------------------------------------------------------

from devops_mcp.models import CreatePullRequestInput  # noqa: E402
from devops_mcp.tools.pull_requests import devops_create_pull_request  # noqa: E402

# ---------------------------------------------------------------------------
# Test 1: No workItemRefs in POST body — regardless of work_item_ids
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_pr_post_body_never_contains_work_item_refs(transport_and_ctx):
    """The PR create POST body must not contain `workItemRefs` even when work_item_ids is set."""
    transport, app_ctx, mcp_ctx = transport_and_ctx

    params = CreatePullRequestInput(
        organization=FAKE_ORG,
        project=FAKE_PROJECT,
        repository_id=FAKE_REPO_ID,
        source_ref_name="refs/heads/feature",
        target_ref_name="refs/heads/main",
        title="Test PR",
        work_item_ids=[FAKE_WI_ID_1],
    )

    patches = _auth_patches()
    for p in patches:
        p.start()
    try:
        await devops_create_pull_request(params, mcp_ctx)
    finally:
        for p in patches:
            p.stop()

    # Find the PR create POST request
    pr_posts = [r for r in transport.requests if r.method == "POST" and "pullrequests" in r.url.path]
    assert len(pr_posts) == 1, f"Expected exactly 1 PR POST, got {len(pr_posts)}"

    post_body = json.loads(pr_posts[0].content)
    assert "workItemRefs" not in post_body, (
        f"workItemRefs must not appear in the PR create POST body; got keys: {list(post_body.keys())}"
    )


# ---------------------------------------------------------------------------
# Test 2: ArtifactLink PATCH issued per work item when work_item_ids provided
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_pr_issues_artifact_link_patch_per_work_item(transport_and_ctx):
    """When work_item_ids is supplied, one ArtifactLink PATCH per work item must be issued."""
    transport, app_ctx, mcp_ctx = transport_and_ctx

    params = CreatePullRequestInput(
        organization=FAKE_ORG,
        project=FAKE_PROJECT,
        repository_id=FAKE_REPO_ID,
        source_ref_name="refs/heads/feature",
        target_ref_name="refs/heads/main",
        title="Test PR",
        work_item_ids=[FAKE_WI_ID_1, FAKE_WI_ID_2],
    )

    patches = _auth_patches()
    for p in patches:
        p.start()
    try:
        result_json = await devops_create_pull_request(params, mcp_ctx)
    finally:
        for p in patches:
            p.stop()

    # No error in result
    result = json.loads(result_json)
    assert "error" not in result, f"Unexpected error: {result}"

    # Collect WIT PATCH requests
    wit_patches = [
        r for r in transport.requests
        if r.method == "PATCH" and "/wit/workitems/" in r.url.path
    ]
    assert len(wit_patches) == 2, (
        f"Expected 2 WIT PATCH requests (one per work item), got {len(wit_patches)}"
    )

    # Each PATCH must carry a single ArtifactLink add operation
    for req in wit_patches:
        ops = json.loads(req.content)
        assert len(ops) == 1, f"Expected 1 patch op, got {len(ops)}"
        op = ops[0]
        assert op["op"] == "add"
        assert op["path"] == "/relations/-"
        assert op["value"]["rel"] == "ArtifactLink"
        assert op["value"]["attributes"]["name"] == "Pull Request"
        # Verify the artifact URI encodes the right project/repo/PR IDs
        assert str(FAKE_PR_ID) in op["value"]["url"]
        assert FAKE_PROJECT_ID in op["value"]["url"]
        assert FAKE_RESOLVED_REPO_ID in op["value"]["url"]

    # Confirm the patched work item IDs match what was requested
    patched_wi_ids = set()
    for req in wit_patches:
        wi_id = int(req.url.path.rstrip("/").split("/")[-1])
        patched_wi_ids.add(wi_id)
    assert patched_wi_ids == {FAKE_WI_ID_1, FAKE_WI_ID_2}


# ---------------------------------------------------------------------------
# Test 3: No WIT requests at all when work_item_ids is absent
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_pr_no_wit_requests_when_no_work_item_ids(transport_and_ctx):
    """When work_item_ids is not supplied, no WIT GET or PATCH requests are made."""
    transport, app_ctx, mcp_ctx = transport_and_ctx

    params = CreatePullRequestInput(
        organization=FAKE_ORG,
        project=FAKE_PROJECT,
        repository_id=FAKE_REPO_ID,
        source_ref_name="refs/heads/feature",
        target_ref_name="refs/heads/main",
        title="Test PR",
    )

    patches = _auth_patches()
    for p in patches:
        p.start()
    try:
        result_json = await devops_create_pull_request(params, mcp_ctx)
    finally:
        for p in patches:
            p.stop()

    result = json.loads(result_json)
    assert "error" not in result

    wit_requests = [r for r in transport.requests if "/wit/workitems/" in r.url.path]
    assert len(wit_requests) == 0, (
        f"Expected no WIT requests when work_item_ids absent, got {len(wit_requests)}"
    )

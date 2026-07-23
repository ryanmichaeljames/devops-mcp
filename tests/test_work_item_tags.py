"""Unit tests for devops_update_work_item_tags.

Azure DevOps stores tags as a single semicolon-separated System.Tags string
with no native add/remove operation, so the tool performs a read-modify-write:
GET the current tags + rev, compute the new tag set, and PATCH it back with a
JSON Patch /rev test op for optimistic concurrency.

GOTCHA — verified empirically against the live Azure DevOps API, do not
"simplify" this back to `op: add`: Azure DevOps treats `op: add` on
System.Tags as a UNION MERGE with whatever tags are already stored, not a
replace. That silently defeats every removal (the tag stays server-side) and
can make a pure-removal PATCH a complete server-side no-op (the work item
`rev` does not even advance) while the tool still reports `changed: true`.
`op: replace` is the verified-correct op: it genuinely replaces the field,
including when the field is currently absent (no prior tags) and when the new
value is an empty string (which clears the field back to absent). The mock
handler below models this: it always overwrites fields.System.Tags with the
PATCH's `replace` value (dropping the key entirely for an empty value, mirror-
ing the real API), so a test that regresses back to `op: add` would need a
handler that MERGES instead — a merging handler would make the removal tests
below fail, which is the intended tripwire.

Acceptance criteria verified here:
- Add-only, remove-only, and combined add+remove all compute the expected
  resulting tag set.
- Tag matching (both add and remove) is case-insensitive.
- The 'add' list is de-duplicated case-insensitively before being applied.
- Removing a tag by a different casing than stored preserves the casing of
  any tags that remain.
- Removal wins over add when the same tag appears in both lists.
- A no-op (computed tag set equals the current tag set) skips the PATCH
  entirely and reports changed=False.
- The PATCH body includes a `test /rev` op for optimistic concurrency and a
  `replace` (not `add`) op on /fields/System.Tags.
- Clearing every tag (remove-all) results in an empty tags list/count=0.
- A work item whose System.Tags field is entirely absent (not an empty
  string) is handled the same as an empty tag set.
- The response's 'tags'/'added'/'removed'/'count' are derived from what the
  PATCH response actually persisted (server post-state), not the locally
  predicted value.
- Input validation (no tags supplied, empty/whitespace tag, tag containing
  a semicolon) fails before any HTTP call is made.

All HTTP is intercepted via a capturing transport — no network required.
Generic fake org/project identifiers are used throughout.
"""

import json
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from devops_mcp.models import UpdateWorkItemInput, UpdateWorkItemTagsInput
from devops_mcp.tools.work_items import (
    devops_update_work_item,
    devops_update_work_item_tags,
)

FAKE_ORG = "testorg"
FAKE_PROJECT = "TestProject"
FAKE_WI_ID = 101
FAKE_REV = 7


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


def _make_handler(
    current_tags: str | None = "", rev: int = FAKE_REV
) -> Callable[[httpx.Request], httpx.Response]:
    """Build a handler modelling real `replace` (not `add`/merge) semantics.

    ``current_tags=None`` simulates System.Tags being entirely absent from
    the GET response's fields dict (the real API's baseline for a work item
    with no tags), as opposed to ``""`` which simulates an explicit empty
    string. On PATCH, the handler reads the `replace` op's value and
    overwrites fields.System.Tags with it — dropping the key entirely for an
    empty value, mirroring the real API's behaviour of leaving the field
    absent after it's cleared. This is what makes the removal-focused tests
    below a real tripwire against ever regressing to `op: add` (a merging
    handler would need to be substituted, and the removal assertions would
    then fail).
    """
    state = {"tags": current_tags, "rev": rev}

    def _handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/wit/workitems/" in req.url.path:
            fields = {} if state["tags"] is None else {"System.Tags": state["tags"]}
            return _json_response(
                200,
                {"id": FAKE_WI_ID, "rev": state["rev"], "fields": fields},
                req,
            )
        if req.method == "PATCH" and "/wit/workitems/" in req.url.path:
            ops = json.loads(req.content)
            tags_op = next(op for op in ops if op["path"] == "/fields/System.Tags")
            assert tags_op["op"] == "replace", (
                "PATCH must use `op: replace` on System.Tags — `op: add` performs a "
                "union merge on the real API and silently defeats every removal."
            )
            new_value = tags_op["value"]
            state["rev"] += 1
            state["tags"] = None if new_value == "" else new_value
            fields = {} if state["tags"] is None else {"System.Tags": state["tags"]}
            return _json_response(200, {"id": FAKE_WI_ID, "rev": state["rev"], "fields": fields}, req)
        raise AssertionError(f"Unexpected request: {req.method} {req.url}")

    return _handler


@pytest.fixture()
def make_transport_and_ctx():
    """Factory fixture: build (transport, mcp_ctx) for a given current tag string."""

    def _factory(current_tags: str | None = "", rev: int = FAKE_REV):
        transport = CapturingTransport(_make_handler(current_tags, rev))
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


async def _call(params, mcp_ctx, tool=devops_update_work_item_tags) -> dict:
    patches = _auth_patches()
    for p in patches:
        p.start()
    try:
        return json.loads(await tool(params, mcp_ctx))
    finally:
        for p in patches:
            p.stop()


def _patch_ops(transport: CapturingTransport) -> list[dict]:
    patch_requests = [r for r in transport.requests if r.method == "PATCH"]
    assert len(patch_requests) == 1, f"Expected exactly one PATCH, got {len(patch_requests)}"
    return json.loads(patch_requests[0].content)


@pytest.mark.asyncio
async def test_add_only(make_transport_and_ctx):
    """Adding tags to a work item with no existing tags."""
    transport, mcp_ctx = make_transport_and_ctx(current_tags="")

    params = UpdateWorkItemTagsInput(
        organization=FAKE_ORG,
        project=FAKE_PROJECT,
        work_item_id=FAKE_WI_ID,
        add=["backend", "needs-review"],
    )
    result = await _call(params, mcp_ctx)

    assert "error" not in result, f"Unexpected error: {result}"
    assert result["changed"] is True
    assert result["tags"] == ["backend", "needs-review"]
    assert result["added"] == ["backend", "needs-review"]
    assert result["removed"] == []
    assert result["count"] == 2

    ops = _patch_ops(transport)
    tags_op = next(op for op in ops if op["path"] == "/fields/System.Tags")
    assert tags_op["op"] == "replace"
    assert tags_op["value"] == "backend; needs-review"


@pytest.mark.asyncio
async def test_remove_only(make_transport_and_ctx):
    """Removing an existing tag, leaving others untouched."""
    transport, mcp_ctx = make_transport_and_ctx(current_tags="backend; urgent")

    params = UpdateWorkItemTagsInput(
        organization=FAKE_ORG,
        project=FAKE_PROJECT,
        work_item_id=FAKE_WI_ID,
        remove=["urgent"],
    )
    result = await _call(params, mcp_ctx)

    assert "error" not in result, f"Unexpected error: {result}"
    assert result["changed"] is True
    assert result["tags"] == ["backend"]
    assert result["added"] == []
    assert result["removed"] == ["urgent"]
    assert result["count"] == 1


@pytest.mark.asyncio
async def test_add_and_remove_together(make_transport_and_ctx):
    """Adding and removing different tags in a single call."""
    transport, mcp_ctx = make_transport_and_ctx(current_tags="backend; urgent")

    params = UpdateWorkItemTagsInput(
        organization=FAKE_ORG,
        project=FAKE_PROJECT,
        work_item_id=FAKE_WI_ID,
        add=["frontend"],
        remove=["urgent"],
    )
    result = await _call(params, mcp_ctx)

    assert "error" not in result, f"Unexpected error: {result}"
    assert result["changed"] is True
    assert result["tags"] == ["backend", "frontend"]
    assert result["added"] == ["frontend"]
    assert result["removed"] == ["urgent"]


@pytest.mark.asyncio
async def test_remove_all_tags_clears_field(make_transport_and_ctx):
    """Removing every existing tag clears System.Tags entirely (count 0, empty list)."""
    transport, mcp_ctx = make_transport_and_ctx(current_tags="backend; urgent")

    params = UpdateWorkItemTagsInput(
        organization=FAKE_ORG,
        project=FAKE_PROJECT,
        work_item_id=FAKE_WI_ID,
        remove=["backend", "urgent"],
    )
    result = await _call(params, mcp_ctx)

    assert "error" not in result, f"Unexpected error: {result}"
    assert result["changed"] is True
    assert result["tags"] == []
    assert result["added"] == []
    assert sorted(result["removed"]) == ["backend", "urgent"]
    assert result["count"] == 0

    ops = _patch_ops(transport)
    tags_op = next(op for op in ops if op["path"] == "/fields/System.Tags")
    assert tags_op["op"] == "replace"
    assert tags_op["value"] == ""


@pytest.mark.asyncio
async def test_tags_field_absent_baseline(make_transport_and_ctx):
    """A work item with no tags at all has System.Tags absent (not ""), not merely empty.

    Adding to it must behave the same as an empty-string baseline.
    """
    transport, mcp_ctx = make_transport_and_ctx(current_tags=None)

    params = UpdateWorkItemTagsInput(
        organization=FAKE_ORG,
        project=FAKE_PROJECT,
        work_item_id=FAKE_WI_ID,
        add=["backend"],
    )
    result = await _call(params, mcp_ctx)

    assert "error" not in result, f"Unexpected error: {result}"
    assert result["changed"] is True
    assert result["tags"] == ["backend"]
    assert result["added"] == ["backend"]
    assert result["removed"] == []
    assert result["count"] == 1


@pytest.mark.asyncio
async def test_case_insensitive_dedupe_on_add(make_transport_and_ctx):
    """Duplicate tags in 'add' differing only by case collapse to a single addition."""
    transport, mcp_ctx = make_transport_and_ctx(current_tags="")

    params = UpdateWorkItemTagsInput(
        organization=FAKE_ORG,
        project=FAKE_PROJECT,
        work_item_id=FAKE_WI_ID,
        add=["Backend", "backend", "BACKEND"],
    )
    result = await _call(params, mcp_ctx)

    assert "error" not in result, f"Unexpected error: {result}"
    assert result["changed"] is True
    # First occurrence's casing is preserved.
    assert result["tags"] == ["Backend"]
    assert result["added"] == ["Backend"]
    assert result["count"] == 1


@pytest.mark.asyncio
async def test_case_insensitive_removal_preserves_existing_casing(make_transport_and_ctx):
    """Removing by a different case than stored still preserves the casing of remaining tags."""
    transport, mcp_ctx = make_transport_and_ctx(current_tags="Backend; Urgent")

    params = UpdateWorkItemTagsInput(
        organization=FAKE_ORG,
        project=FAKE_PROJECT,
        work_item_id=FAKE_WI_ID,
        remove=["URGENT"],
    )
    result = await _call(params, mcp_ctx)

    assert "error" not in result, f"Unexpected error: {result}"
    assert result["changed"] is True
    assert result["tags"] == ["Backend"]
    assert result["removed"] == ["Urgent"]


@pytest.mark.asyncio
async def test_no_op_skips_patch(make_transport_and_ctx):
    """Adding a tag that already exists (case-insensitively) is a no-op — no PATCH issued."""
    transport, mcp_ctx = make_transport_and_ctx(current_tags="Backend")

    params = UpdateWorkItemTagsInput(
        organization=FAKE_ORG,
        project=FAKE_PROJECT,
        work_item_id=FAKE_WI_ID,
        add=["backend"],
    )
    result = await _call(params, mcp_ctx)

    assert "error" not in result, f"Unexpected error: {result}"
    assert result["changed"] is False
    assert result["tags"] == ["Backend"]
    assert result["added"] == []
    assert result["removed"] == []

    patch_requests = [r for r in transport.requests if r.method == "PATCH"]
    assert patch_requests == [], "No PATCH should be issued for a no-op update"
    get_requests = [r for r in transport.requests if r.method == "GET"]
    assert len(get_requests) == 1


@pytest.mark.asyncio
async def test_patch_body_includes_rev_test_op_and_replace_op(make_transport_and_ctx):
    """The PATCH body must include a `test /rev` op and a `replace` (not `add`) tags op."""
    transport, mcp_ctx = make_transport_and_ctx(current_tags="", rev=FAKE_REV)

    params = UpdateWorkItemTagsInput(
        organization=FAKE_ORG,
        project=FAKE_PROJECT,
        work_item_id=FAKE_WI_ID,
        add=["backend"],
    )
    result = await _call(params, mcp_ctx)

    assert "error" not in result, f"Unexpected error: {result}"
    ops = _patch_ops(transport)
    assert ops[0] == {"op": "test", "path": "/rev", "value": FAKE_REV}
    tags_op = next(op for op in ops if op["path"] == "/fields/System.Tags")
    assert tags_op["op"] == "replace"


@pytest.mark.asyncio
async def test_no_tags_provided_is_rejected():
    """Neither 'add' nor 'remove' supplied is an actionable validation error."""
    mcp_ctx = MagicMock()

    params = UpdateWorkItemTagsInput(
        organization=FAKE_ORG,
        project=FAKE_PROJECT,
        work_item_id=FAKE_WI_ID,
    )
    result = await _call(params, mcp_ctx)

    assert result.get("error") is True
    assert "No tags to add or remove" in result["message"]


@pytest.mark.asyncio
async def test_tag_containing_semicolon_is_rejected():
    """A tag containing a semicolon must be rejected before any HTTP call."""
    mcp_ctx = MagicMock()

    params = UpdateWorkItemTagsInput(
        organization=FAKE_ORG,
        project=FAKE_PROJECT,
        work_item_id=FAKE_WI_ID,
        add=["backend;urgent"],
    )
    result = await _call(params, mcp_ctx)

    assert result.get("error") is True
    assert "semicolon" in result["message"]


@pytest.mark.asyncio
async def test_whitespace_only_tag_is_rejected():
    """A whitespace-only tag must be rejected before any HTTP call."""
    mcp_ctx = MagicMock()

    params = UpdateWorkItemTagsInput(
        organization=FAKE_ORG,
        project=FAKE_PROJECT,
        work_item_id=FAKE_WI_ID,
        add=["   "],
    )
    result = await _call(params, mcp_ctx)

    assert result.get("error") is True
    assert "empty or whitespace-only" in result["message"]


# ---------------------------------------------------------------------------
# devops_update_work_item's 'tags' field (whole-field replace, not add/remove)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_work_item_tags_uses_replace_op(make_transport_and_ctx):
    """devops_update_work_item's tags field must PATCH with `op: replace`.

    Its docstring promises a full-value replace ("Pass an empty string to
    clear all tags"); `op: add` cannot honour that contract on the real API
    because Azure DevOps merges rather than replaces.
    """
    transport, mcp_ctx = make_transport_and_ctx(current_tags="old-tag")

    params = UpdateWorkItemInput(
        organization=FAKE_ORG,
        project=FAKE_PROJECT,
        work_item_id=FAKE_WI_ID,
        tags="new-tag",
    )
    result = await _call(params, mcp_ctx, tool=devops_update_work_item)

    assert "error" not in result, f"Unexpected error: {result}"

    patch_requests = [r for r in transport.requests if r.method == "PATCH"]
    assert len(patch_requests) == 1
    ops = json.loads(patch_requests[0].content)
    tags_op = next(op for op in ops if op["path"] == "/fields/System.Tags")
    assert tags_op["op"] == "replace"
    assert tags_op["value"] == "new-tag"


@pytest.mark.asyncio
async def test_update_work_item_tags_empty_string_clears(make_transport_and_ctx):
    """devops_update_work_item's tags='' must PATCH a `replace` with an empty value."""
    transport, mcp_ctx = make_transport_and_ctx(current_tags="old-tag")

    params = UpdateWorkItemInput(
        organization=FAKE_ORG,
        project=FAKE_PROJECT,
        work_item_id=FAKE_WI_ID,
        tags="",
    )
    result = await _call(params, mcp_ctx, tool=devops_update_work_item)

    assert "error" not in result, f"Unexpected error: {result}"

    ops = _patch_ops(transport)
    tags_op = next(op for op in ops if op["path"] == "/fields/System.Tags")
    assert tags_op["op"] == "replace"
    assert tags_op["value"] == ""

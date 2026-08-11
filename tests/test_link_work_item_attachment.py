"""Unit tests for devops_link_work_item_attachment.

All HTTP is intercepted with an httpx.AsyncBaseTransport stub — no network, no
credentials, no real organization or project names.

Three groups of assertions are load-bearing:

1. **A caller-supplied url is rebuilt, never stored verbatim.** An unvalidated
   URL written into a relation is *persisted* on the work item and re-read by
   every future reader — a durable injection vector, not a one-shot request.
   The tests assert the refusal happens before any token is minted, and that a
   tolerated-but-odd URL (project-scoped, extra query parameters) is stored in
   canonical org-scoped form.

2. **The no-op path issues no PATCH at all.** "Idempotent" is only true if the
   second call does not write; asserting `changed: false` alone would pass even
   if a pointless revision had been created.

3. **The /rev test op is present**, so a concurrent edit fails the call rather
   than appending a relation to a work item that has moved underneath it.
"""

import json
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import devops_mcp.server  # noqa: F401 — side-effect: registers every tool
from devops_mcp._app import mcp
from devops_mcp.models import LinkWorkItemAttachmentInput
from devops_mcp.tools.attachments import devops_link_work_item_attachment

# ---------------------------------------------------------------------------
# Fake constants — no real organizations, projects, or identifiers
# ---------------------------------------------------------------------------

FAKE_ORG = "fake-org"
FAKE_PROJECT = "fake-project"
FAKE_PROJECT_GUID = "0f1e2d3c-4b5a-4968-8776-655443332211"
GUID_A = "a5cedde4-2dd5-4fcf-befe-fd0977dd3433"
GUID_B = "bb1e6c14-9f4a-4d1e-9a0d-2f8b6b3c5a11"

WORKITEMS_API_VERSION = "7.2-preview.3"


def _org_url(guid: str, file_name: str | None = None) -> str:
    url = f"https://dev.azure.com/{FAKE_ORG}/_apis/wit/attachments/{guid}"
    return f"{url}?fileName={file_name}" if file_name else url


def _relation(guid: str, *, name: str = "diagram.png", comment: str | None = None) -> dict:
    attributes: dict = {"name": name, "resourceSize": 1234}
    if comment is not None:
        attributes["comment"] = comment
    return {"rel": "AttachedFile", "url": _org_url(guid, name), "attributes": attributes}


# ---------------------------------------------------------------------------
# Transport plumbing
# ---------------------------------------------------------------------------


class CapturingTransport(httpx.AsyncBaseTransport):
    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self._handler = handler
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)

    @property
    def methods(self) -> list[str]:
        return [request.method for request in self.requests]


def _make_handler(
    *,
    relations: list[dict] | None = None,
    rev: int | None = 7,
    get_status: int = 200,
    patch_status: int = 200,
    patch_relations: list[dict] | None = None,
    omit_patch_relations: bool = False,
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            if get_status != 200:
                return httpx.Response(
                    status_code=get_status,
                    headers={"Content-Type": "application/json"},
                    content=json.dumps({"message": "TF401232: nope"}).encode(),
                    request=req,
                )
            body: dict = {"id": 42, "fields": {}, "relations": relations or []}
            if rev is not None:
                body["rev"] = rev
            return httpx.Response(
                status_code=200,
                headers={"Content-Type": "application/json"},
                content=json.dumps(body).encode(),
                request=req,
            )
        if req.method == "PATCH":
            if patch_status != 200:
                return httpx.Response(
                    status_code=patch_status,
                    headers={"Content-Type": "application/json"},
                    content=json.dumps({
                        "message": "VS402625: The 'test' operation failed.",
                        "typeKey": "RuleValidationException",
                    }).encode(),
                    request=req,
                )
            sent = json.loads(req.content.decode())
            added = [op["value"] for op in sent if op.get("path") == "/relations/-"]
            body = {"id": 42, "rev": (rev or 0) + 1}
            if not omit_patch_relations:
                body["relations"] = patch_relations if patch_relations is not None else [
                    # Echo the relation with the attributes the service adds.
                    {**value, "attributes": {**value.get("attributes", {}), "resourceSize": 1234}}
                    for value in added
                ]
            return httpx.Response(
                status_code=200,
                headers={"Content-Type": "application/json"},
                content=json.dumps(body).encode(),
                request=req,
            )
        raise AssertionError(f"Unexpected request: {req.method} {req.url}")

    return handler


def _make_ctx(transport: CapturingTransport) -> MagicMock:
    app_ctx = MagicMock()
    app_ctx.organization = FAKE_ORG
    app_ctx.project = FAKE_PROJECT
    app_ctx.http_client = httpx.AsyncClient(transport=transport)
    mcp_ctx = MagicMock()
    mcp_ctx.request_context.lifespan_context = app_ctx
    return mcp_ctx


async def _fake_build_headers(app_ctx, *, include_content_type: bool = False, extra=None) -> dict:
    headers = {"Authorization": "Bearer fake-token", "Accept": "application/json"}
    if include_content_type:
        headers["Content-Type"] = "application/json"
    if extra:
        headers.update(extra)
    return headers


@pytest.fixture()
def build_headers_mock():
    return AsyncMock(side_effect=_fake_build_headers)


@pytest.fixture()
def auth_patches(build_headers_mock):
    patches = [
        patch("devops_mcp.tools.attachments.build_headers", new=build_headers_mock),
        patch("devops_mcp.tools.attachments.resolve_org", return_value=FAKE_ORG),
        patch("devops_mcp.tools.attachments.resolve_project", return_value=FAKE_PROJECT),
    ]
    for p in patches:
        p.start()
    try:
        yield
    finally:
        for p in patches:
            p.stop()


async def _link(transport: CapturingTransport, **kwargs) -> dict:
    raw = await devops_link_work_item_attachment(
        LinkWorkItemAttachmentInput(work_item_id=42, **kwargs), _make_ctx(transport)
    )
    assert isinstance(raw, str)
    return json.loads(raw)


def _patch_ops(transport: CapturingTransport) -> list[dict]:
    patches = [r for r in transport.requests if r.method == "PATCH"]
    assert patches, "expected a PATCH request"
    return json.loads(patches[0].content.decode())


# ---------------------------------------------------------------------------
# Fresh link
# ---------------------------------------------------------------------------


async def test_fresh_link_adds_the_relation(auth_patches):
    transport = CapturingTransport(_make_handler(relations=[]))
    result = await _link(transport, attachment_id=GUID_A, file_name="diagram.png")

    assert transport.methods == ["GET", "PATCH"]
    ops = _patch_ops(transport)
    assert ops[0] == {"op": "test", "path": "/rev", "value": 7}
    assert ops[1]["op"] == "add"
    assert ops[1]["path"] == "/relations/-"
    assert ops[1]["value"]["rel"] == "AttachedFile"
    assert ops[1]["value"]["url"] == _org_url(GUID_A, "diagram.png")

    assert result["changed"] is True
    assert result["attachment_id"] == GUID_A
    assert result["file_name"] == "diagram.png"
    assert result["rev"] == 8
    assert result["relation_read_back"] is True
    # Read back from the PATCH response, not predicted: resourceSize is only
    # ever known server-side.
    assert result["relation"]["attributes"]["resourceSize"] == 1234


async def test_patch_uses_the_json_patch_content_type_and_api_version(auth_patches):
    transport = CapturingTransport(_make_handler(relations=[]))
    await _link(transport, attachment_id=GUID_A)

    request = [r for r in transport.requests if r.method == "PATCH"][0]
    assert request.headers["content-type"] == "application/json-patch+json"
    assert dict(request.url.params)["api-version"] == WORKITEMS_API_VERSION
    assert request.url.path == f"/{FAKE_ORG}/{FAKE_PROJECT}/_apis/wit/workitems/42"


async def test_get_expands_relations(auth_patches):
    transport = CapturingTransport(_make_handler(relations=[]))
    await _link(transport, attachment_id=GUID_A)

    params = dict(transport.requests[0].url.params)
    assert params["$expand"] == "relations"
    assert params["api-version"] == WORKITEMS_API_VERSION


async def test_comment_is_stored_on_the_relation_attributes(auth_patches):
    transport = CapturingTransport(_make_handler(relations=[]))
    result = await _link(transport, attachment_id=GUID_A, comment="design v2")

    assert _patch_ops(transport)[1]["value"]["attributes"] == {"comment": "design v2"}
    assert result["relation"]["attributes"]["comment"] == "design v2"


async def test_no_comment_means_no_attributes_key(auth_patches):
    transport = CapturingTransport(_make_handler(relations=[]))
    await _link(transport, attachment_id=GUID_A)

    assert "attributes" not in _patch_ops(transport)[1]["value"]


async def test_missing_rev_omits_the_test_op_rather_than_failing(auth_patches):
    transport = CapturingTransport(_make_handler(relations=[], rev=None))
    await _link(transport, attachment_id=GUID_A)

    ops = _patch_ops(transport)
    assert len(ops) == 1
    assert ops[0]["path"] == "/relations/-"


async def test_relation_absent_from_the_patch_response_is_reported_honestly(auth_patches):
    transport = CapturingTransport(_make_handler(relations=[], omit_patch_relations=True))
    result = await _link(transport, attachment_id=GUID_A, file_name="a.png")

    assert result["changed"] is True
    assert result["relation_read_back"] is False
    assert result["relation"]["url"] == _org_url(GUID_A, "a.png")
    assert "devops_list_work_item_attachments" in result["message"]


# ---------------------------------------------------------------------------
# URL input is parsed and rebuilt, never passed through
# ---------------------------------------------------------------------------


async def test_url_input_is_rebuilt_not_stored_verbatim(auth_patches):
    """A project-scoped upload url with extra parameters must not be persisted."""
    transport = CapturingTransport(_make_handler(relations=[]))
    supplied = (
        f"https://dev.azure.com/{FAKE_ORG}/{FAKE_PROJECT_GUID}/_apis/wit/attachments/"
        f"{GUID_A}?fileName=pasted%20image.png&download=false"
    )
    result = await _link(transport, url=supplied)

    stored = _patch_ops(transport)[1]["value"]["url"]
    assert stored == (
        f"https://dev.azure.com/{FAKE_ORG}/_apis/wit/attachments/{GUID_A}"
        "?fileName=pasted%20image.png"
    )
    assert FAKE_PROJECT_GUID not in stored
    assert "download" not in stored
    assert result["attachment_id"] == GUID_A
    assert result["file_name"] == "pasted image.png"


async def test_explicit_file_name_overrides_the_url_query(auth_patches):
    transport = CapturingTransport(_make_handler(relations=[]))
    await _link(transport, url=_org_url(GUID_A, "from-url.png"), file_name="override.png")

    assert _patch_ops(transport)[1]["value"]["url"].endswith("?fileName=override.png")


async def test_file_name_is_percent_encoded_into_the_stored_url(auth_patches):
    transport = CapturingTransport(_make_handler(relations=[]))
    await _link(transport, attachment_id=GUID_A, file_name="a & b (1).png")

    stored = _patch_ops(transport)[1]["value"]["url"]
    assert stored.endswith("?fileName=a%20%26%20b%20%281%29.png")


@pytest.mark.parametrize(
    ("bad_url", "why"),
    [
        (f"https://evil.example/_apis/wit/attachments/{GUID_A}", "foreign host"),
        (f"https://dev.azure.com@evil.example/{FAKE_ORG}/_apis/wit/attachments/{GUID_A}", "userinfo"),
        (f"http://dev.azure.com/{FAKE_ORG}/_apis/wit/attachments/{GUID_A}", "plain http"),
        (f"https://dev.azure.com:8443/{FAKE_ORG}/_apis/wit/attachments/{GUID_A}", "non-443 port"),
        (f"https://dev.azure.com/other-org/_apis/wit/attachments/{GUID_A}", "foreign org"),
        (f"https://dev.azure.com/{FAKE_ORG}/_apis/wit/attachments/not-a-guid", "non-guid id"),
    ],
)
async def test_hostile_url_is_refused_before_any_request_or_token(
    auth_patches, build_headers_mock, bad_url, why
):
    transport = CapturingTransport(_make_handler(relations=[]))
    result = await _link(transport, url=bad_url)

    assert result["error"] is True
    # Nothing was read, nothing was written, and no credential was acquired —
    # so a crafted URL can neither be stored nor exfiltrate a token.
    assert transport.requests == []
    assert build_headers_mock.await_count == 0


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


async def test_already_linked_issues_no_patch(auth_patches):
    transport = CapturingTransport(_make_handler(relations=[_relation(GUID_A, name="diagram.png")]))
    result = await _link(transport, attachment_id=GUID_A)

    assert result["changed"] is False
    assert result["relation"]["url"] == _org_url(GUID_A, "diagram.png")
    assert result["relation"]["attributes"]["resourceSize"] == 1234
    # The assertion that makes "idempotent" mean something.
    assert transport.methods == ["GET"]
    assert "PATCH" not in transport.methods


async def test_already_linked_matches_a_differently_shaped_url(auth_patches):
    """Project-scoped, no fileName — same attachment, so still a no-op."""
    existing = {
        "rel": "AttachedFile",
        "url": (
            f"https://dev.azure.com/{FAKE_ORG}/{FAKE_PROJECT_GUID}"
            f"/_apis/wit/attachments/{GUID_A.upper()}"
        ),
        "attributes": {"name": "diagram.png"},
    }
    transport = CapturingTransport(_make_handler(relations=[existing]))
    result = await _link(transport, attachment_id=GUID_A, file_name="diagram.png")

    assert result["changed"] is False
    assert transport.methods == ["GET"]


async def test_a_different_attachment_still_links(auth_patches):
    transport = CapturingTransport(_make_handler(relations=[_relation(GUID_B)]))
    result = await _link(transport, attachment_id=GUID_A)

    assert result["changed"] is True
    assert transport.methods == ["GET", "PATCH"]


async def test_a_foreign_org_relation_is_not_treated_as_already_linked(auth_patches):
    """It must not be echoed back as 'existing' either — it is untrusted."""
    hostile = {
        "rel": "AttachedFile",
        "url": f"https://evil.example/_apis/wit/attachments/{GUID_A}",
        "attributes": {"name": "steal.png"},
    }
    transport = CapturingTransport(_make_handler(relations=[hostile]))
    raw = await devops_link_work_item_attachment(
        LinkWorkItemAttachmentInput(work_item_id=42, attachment_id=GUID_A), _make_ctx(transport)
    )

    assert json.loads(raw)["changed"] is True
    assert "evil.example" not in raw


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [409, 412])
async def test_rev_conflict_is_reported_as_a_concurrency_failure(auth_patches, status):
    transport = CapturingTransport(_make_handler(relations=[], patch_status=status))
    result = await _link(transport, attachment_id=GUID_A)

    assert result["error"] is True
    assert "changed between" in result["message"]
    assert "again" in result["message"]
    assert str(status) in result["message"]


async def test_patch_is_not_retried_on_a_gateway_error(auth_patches):
    transport = CapturingTransport(_make_handler(relations=[], patch_status=503))
    result = await _link(transport, attachment_id=GUID_A)

    assert result["error"] is True
    assert transport.methods == ["GET", "PATCH"]


async def test_get_404_is_reported_as_actionable_json(auth_patches):
    transport = CapturingTransport(_make_handler(get_status=404))
    result = await _link(transport, attachment_id=GUID_A)

    assert result["error"] is True
    assert "42" in result["message"]
    assert FAKE_PROJECT in result["message"]


async def test_403_names_the_permission_context(auth_patches):
    transport = CapturingTransport(_make_handler(get_status=403))
    result = await _link(transport, attachment_id=GUID_A)

    assert result["error"] is True
    assert "403" in result["message"]


async def test_unresolvable_org_is_reported_as_json(build_headers_mock):
    transport = CapturingTransport(_make_handler())
    with patch("devops_mcp.tools.attachments.build_headers", new=build_headers_mock):
        app_ctx = MagicMock()
        app_ctx.organization = None
        app_ctx.project = None
        app_ctx.http_client = httpx.AsyncClient(transport=transport)
        mcp_ctx = MagicMock()
        mcp_ctx.request_context.lifespan_context = app_ctx

        result = json.loads(await devops_link_work_item_attachment(
            LinkWorkItemAttachmentInput(work_item_id=42, attachment_id=GUID_A), mcp_ctx
        ))

    assert result["error"] is True
    assert transport.requests == []


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


async def test_registration_follows_the_write_gate():
    from devops_mcp._app import _ALLOW_WRITE

    names = {tool.name for tool in await mcp.list_tools()}
    assert ("devops_link_work_item_attachment" in names) is _ALLOW_WRITE

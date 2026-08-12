"""Unit tests for devops_list_work_item_attachments.

All HTTP is intercepted with an httpx.AsyncBaseTransport stub — no network, no
credentials, no real organization or project names.

Two groups of assertions are load-bearing rather than nice-to-have:

1. **Inline images are found at all.** A pasted screenshot lives only as a URL
   inside a large-text field; it is not necessarily an AttachedFile relation.
   A relations-only listing would return "no attachments" for a work item that
   visibly contains one, which is the bug this tool exists to prevent.

2. **A hostile URL in a description is dropped, counted, and never echoed.**
   Work item text is user-authored and therefore attacker-influenceable.
   Repeating a rejected URL back to the model would relay exactly the payload
   the guard just caught, so the tests assert the string does not appear
   anywhere in the response — not merely that it is absent from 'attachments'.
"""

import json
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import devops_mcp.server  # noqa: F401 — side-effect: registers every tool
from devops_mcp._app import mcp
from devops_mcp.models import ListWorkItemAttachmentsInput
from devops_mcp.tools.attachments import devops_list_work_item_attachments

# ---------------------------------------------------------------------------
# Fake constants — no real organizations, projects, or identifiers
# ---------------------------------------------------------------------------

FAKE_ORG = "fake-org"
FAKE_PROJECT = "fake-project"
FAKE_PROJECT_GUID = "0f1e2d3c-4b5a-4968-8776-655443332211"
GUID_A = "a5cedde4-2dd5-4fcf-befe-fd0977dd3433"
GUID_B = "bb1e6c14-9f4a-4d1e-9a0d-2f8b6b3c5a11"
GUID_C = "c73f9a02-1d44-4b6e-9f3a-6d2c8e5b7a90"

WORKITEMS_API_VERSION = "7.2-preview.3"
COMMENTS_API_VERSION = "7.1-preview.4"

# A URL naming another organization: the exact shape an injected description
# uses to try to make this server send its token somewhere else.
HOSTILE_URL = f"https://evil.example/_apis/wit/attachments/{GUID_C}?fileName=steal.png"
FOREIGN_ORG_URL = f"https://dev.azure.com/other-org/_apis/wit/attachments/{GUID_C}"


def _org_url(guid: str, file_name: str | None = None) -> str:
    url = f"https://dev.azure.com/{FAKE_ORG}/_apis/wit/attachments/{guid}"
    return f"{url}?fileName={file_name}" if file_name else url


def _relation(guid: str, *, name: str = "diagram.png", size: int = 1234, comment: str | None = None) -> dict:
    attributes: dict = {"name": name, "resourceSize": size}
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


def _make_handler(
    *,
    relations: list[dict] | None = None,
    fields: dict | None = None,
    comments: list[dict] | None = None,
    continuation_token: str | None = None,
    work_item_status: int = 200,
    comments_status: int = 200,
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/comments"):
            if comments_status != 200:
                return httpx.Response(
                    status_code=comments_status,
                    headers={"Content-Type": "application/json"},
                    content=json.dumps({"message": "boom"}).encode(),
                    request=req,
                )
            body: dict = {"comments": comments or [], "count": len(comments or [])}
            if continuation_token:
                body["continuationToken"] = continuation_token
            return httpx.Response(
                status_code=200,
                headers={"Content-Type": "application/json"},
                content=json.dumps(body).encode(),
                request=req,
            )
        if "/wit/workitems/" in req.url.path.lower():
            if work_item_status != 200:
                return httpx.Response(
                    status_code=work_item_status,
                    headers={"Content-Type": "application/json"},
                    content=json.dumps({
                        "message": "TF401232: Work item does not exist.",
                        "typeKey": "WorkItemNotFoundException",
                    }).encode(),
                    request=req,
                )
            return httpx.Response(
                status_code=200,
                headers={"Content-Type": "application/json"},
                content=json.dumps({
                    "id": 42,
                    "rev": 7,
                    "fields": fields if fields is not None else {"System.Title": "A bug"},
                    "relations": relations or [],
                }).encode(),
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


async def _list(transport: CapturingTransport, **kwargs) -> dict:
    raw = await devops_list_work_item_attachments(
        ListWorkItemAttachmentsInput(work_item_id=42, **kwargs), _make_ctx(transport)
    )
    assert isinstance(raw, str)
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Relation source
# ---------------------------------------------------------------------------


async def test_relation_only(auth_patches):
    transport = CapturingTransport(
        _make_handler(relations=[_relation(GUID_A, name="spec.pdf", size=9999, comment="the spec")])
    )
    result = await _list(transport)

    assert result["count"] == 1
    entry = result["attachments"][0]
    assert entry["attachment_id"] == GUID_A
    assert entry["file_name"] == "spec.pdf"
    assert entry["sources"] == ["relation"]
    assert entry["size_bytes"] == 9999
    assert entry["comment"] == "the spec"
    assert entry["url"] == _org_url(GUID_A, "spec.pdf")
    assert result["rejected_urls"] == 0


async def test_non_attachment_relations_are_ignored(auth_patches):
    transport = CapturingTransport(
        _make_handler(relations=[
            {"rel": "ArtifactLink", "url": "vstfs:///Git/PullRequestId/x", "attributes": {}},
            {"rel": "System.LinkTypes.Hierarchy-Reverse", "url": _org_url(GUID_B), "attributes": {}},
            _relation(GUID_A),
        ])
    )
    result = await _list(transport)

    assert [e["attachment_id"] for e in result["attachments"]] == [GUID_A]


async def test_zero_attachments(auth_patches):
    transport = CapturingTransport(
        _make_handler(relations=[], fields={"System.Title": "no pictures here"})
    )
    result = await _list(transport)

    assert result == {
        "work_item_id": 42,
        "attachments": [],
        "count": 0,
        "rejected_urls": 0,
    }


# ---------------------------------------------------------------------------
# Inline source — the surface a relations-only listing misses entirely
# ---------------------------------------------------------------------------


async def test_inline_only_html_description(auth_patches):
    transport = CapturingTransport(
        _make_handler(
            relations=[],
            fields={
                "System.Title": "A bug",
                "System.Description": f'<p>See:</p><img src="{_org_url(GUID_A, "shot.png")}" alt="shot">',
            },
        )
    )
    result = await _list(transport)

    assert result["count"] == 1
    entry = result["attachments"][0]
    assert entry["attachment_id"] == GUID_A
    assert entry["file_name"] == "shot.png"
    assert entry["sources"] == ["inline"]
    assert entry["fields"] == ["System.Description"]
    assert "size_bytes" not in entry  # only a relation knows the size


async def test_inline_markdown_repro_steps(auth_patches):
    transport = CapturingTransport(
        _make_handler(
            relations=[],
            fields={"Microsoft.VSTS.TCM.ReproSteps": f"1. Do it\n\n![x]({_org_url(GUID_B, 'a.png')})"},
        )
    )
    result = await _list(transport)

    assert result["attachments"][0]["attachment_id"] == GUID_B
    assert result["attachments"][0]["fields"] == ["Microsoft.VSTS.TCM.ReproSteps"]


async def test_same_url_in_two_fields_lists_both_fields_once(auth_patches):
    url = _org_url(GUID_A, "shot.png")
    transport = CapturingTransport(
        _make_handler(
            relations=[],
            fields={
                "System.Description": f'<img src="{url}">',
                "Microsoft.VSTS.Common.AcceptanceCriteria": f'<img src="{url}"><img src="{url}">',
            },
        )
    )
    result = await _list(transport)

    assert result["count"] == 1
    assert sorted(result["attachments"][0]["fields"]) == [
        "Microsoft.VSTS.Common.AcceptanceCriteria",
        "System.Description",
    ]


async def test_non_large_text_fields_are_not_scanned(auth_patches):
    """Only the known large-text fields carry rendered content."""
    transport = CapturingTransport(
        _make_handler(
            relations=[],
            fields={"Custom.SomeString": f'<img src="{_org_url(GUID_A)}">'},
        )
    )
    result = await _list(transport)

    assert result["count"] == 0


async def test_non_string_field_values_do_not_crash(auth_patches):
    transport = CapturingTransport(
        _make_handler(
            relations=[],
            fields={
                "System.Description": None,
                "System.AssignedTo": {"displayName": "Someone"},
                "Microsoft.VSTS.TCM.ReproSteps": f'<img src="{_org_url(GUID_A)}">',
            },
        )
    )
    result = await _list(transport)

    assert result["count"] == 1


async def test_the_newest_comment_is_scanned_without_include_comments(auth_patches):
    """System.History carries the most recent comment's text, so the latest
    comment is scanned for free. include_comments is what reaches *older*
    comments and supplies comment ids — the docstring says so, and this pins it."""
    transport = CapturingTransport(
        _make_handler(
            relations=[],
            fields={"System.History": f'<img src="{_org_url(GUID_A, "pasted.png")}">'},
        )
    )
    result = await _list(transport)

    assert len(transport.requests) == 1, "no comments call was made"
    entry = result["attachments"][0]
    assert entry["attachment_id"] == GUID_A
    assert entry["fields"] == ["System.History"]
    assert "comment_ids" not in entry
    assert "comments_scanned" not in result


@pytest.mark.parametrize(
    "src",
    [
        f"/{FAKE_ORG}/_apis/wit/attachments/{GUID_A}?fileName=shot.png",
        f"/{FAKE_ORG}/{FAKE_PROJECT_GUID}/_apis/wit/attachments/{GUID_A}?fileName=shot.png",
        f"//dev.azure.com/{FAKE_ORG}/_apis/wit/attachments/{GUID_A}?fileName=shot.png",
    ],
    ids=["site-relative", "site-relative-project-scoped", "protocol-relative"],
)
async def test_relative_img_src_shapes_are_found(auth_patches, src):
    """These returned zero candidates before — a *silent* miss, not even counted
    in rejected_urls, so the caller was told the work item was clean."""
    transport = CapturingTransport(
        _make_handler(relations=[], fields={"System.Description": f'<img src="{src}">'})
    )
    result = await _list(transport)

    assert result["count"] == 1
    entry = result["attachments"][0]
    assert entry["attachment_id"] == GUID_A
    assert entry["file_name"] == "shot.png"
    # Only ever a canonical URL rebuilt against the resolved organization.
    assert entry["url"] == _org_url(GUID_A, "shot.png")
    assert result["rejected_urls"] == 0


@pytest.mark.parametrize(
    "src",
    [
        f"/other-org/_apis/wit/attachments/{GUID_C}",
        f"/_apis/wit/attachments/{GUID_C}",
        f"//evil.example/{FAKE_ORG}/_apis/wit/attachments/{GUID_C}",
    ],
    ids=["foreign-org", "no-org", "foreign-host"],
)
async def test_relative_shapes_that_fail_validation_are_counted_not_missed(auth_patches, src):
    """The point of matching them: a rejection the caller can see beats a
    silent miss the caller cannot."""
    transport = CapturingTransport(
        _make_handler(relations=[], fields={"System.Description": f'<img src="{src}">'})
    )
    raw = await devops_list_work_item_attachments(
        ListWorkItemAttachmentsInput(work_item_id=42), _make_ctx(transport)
    )
    result = json.loads(raw)

    assert result["count"] == 0
    assert result["rejected_urls"] == 1
    assert "rejected_urls_note" in result
    # Still never echoed back.
    assert src not in raw
    assert GUID_C not in raw
    assert "other-org" not in raw
    assert "evil.example" not in raw


async def test_html_entities_in_an_inline_url_are_decoded(auth_patches):
    """Live symptom on an html-format description storing
    `?fileName=a%20&amp;%20b%20%281%29.png`: the reported file name was the
    literal "a &amp; b (1).png", and the rebuilt URL carried `%26amp%3B`."""
    stored = (
        f"https://dev.azure.com/{FAKE_ORG}/_apis/wit/attachments/{GUID_A}"
        "?fileName=a%20&amp;%20b%20%281%29.png"
    )
    transport = CapturingTransport(
        _make_handler(relations=[], fields={"System.Description": f'<img src="{stored}">'})
    )
    raw = await devops_list_work_item_attachments(
        ListWorkItemAttachmentsInput(work_item_id=42), _make_ctx(transport)
    )
    result = json.loads(raw)

    entry = result["attachments"][0]
    assert entry["file_name"] == "a & b (1).png"
    assert "amp;" not in entry["file_name"]
    assert "%26amp%3B" not in entry["url"]
    assert entry["url"] == _org_url(GUID_A, "a%20%26%20b%20%281%29.png")


# ---------------------------------------------------------------------------
# Dedupe across both surfaces
# ---------------------------------------------------------------------------


async def test_relation_and_inline_dedupe_to_one_entry(auth_patches):
    transport = CapturingTransport(
        _make_handler(
            relations=[_relation(GUID_A, name="shot.png", size=555)],
            fields={"System.Description": f'<img src="{_org_url(GUID_A, "shot.png")}">'},
        )
    )
    result = await _list(transport)

    assert result["count"] == 1
    entry = result["attachments"][0]
    assert entry["sources"] == ["relation", "inline"]
    assert entry["size_bytes"] == 555  # supplied by the relation
    assert entry["fields"] == ["System.Description"]  # supplied by the inline hit


async def test_dedupe_is_case_insensitive_on_the_guid(auth_patches):
    transport = CapturingTransport(
        _make_handler(
            relations=[_relation(GUID_A)],
            fields={"System.Description": f'<img src="{_org_url(GUID_A.upper())}">'},
        )
    )
    result = await _list(transport)

    assert result["count"] == 1
    assert result["attachments"][0]["sources"] == ["relation", "inline"]


async def test_relation_name_wins_over_inline_file_name(auth_patches):
    transport = CapturingTransport(
        _make_handler(
            relations=[_relation(GUID_A, name="official.png")],
            fields={"System.Description": f'<img src="{_org_url(GUID_A, "inline-name.png")}">'},
        )
    )
    result = await _list(transport)

    assert result["attachments"][0]["file_name"] == "official.png"


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------


async def test_comments_are_not_fetched_by_default(auth_patches):
    transport = CapturingTransport(
        _make_handler(relations=[_relation(GUID_A)], comments=[{"id": 3, "text": "hi"}])
    )
    result = await _list(transport)

    assert len(transport.requests) == 1
    assert "comments_scanned" not in result


async def test_include_comments_finds_an_inline_image_in_the_discussion(auth_patches):
    transport = CapturingTransport(
        _make_handler(
            relations=[],
            fields={"System.Title": "A bug"},
            comments=[
                {"id": 3, "text": "no image here"},
                {"id": 9, "text": f'Repro shot: <img src="{_org_url(GUID_B, "c.png")}">'},
            ],
        )
    )
    result = await _list(transport, include_comments=True)

    assert len(transport.requests) == 2
    comments_request = transport.requests[1]
    assert comments_request.url.path.lower().endswith("/wit/workitems/42/comments")
    params = dict(comments_request.url.params)
    assert params["api-version"] == COMMENTS_API_VERSION
    assert params["$top"] == "200"

    assert result["comments_scanned"] == 2
    entry = result["attachments"][0]
    assert entry["attachment_id"] == GUID_B
    assert entry["comment_ids"] == [9]
    assert entry["sources"] == ["inline"]


async def test_include_comments_reports_truncation(auth_patches):
    transport = CapturingTransport(
        _make_handler(relations=[], comments=[{"id": 1, "text": "x"}], continuation_token="abc")
    )
    result = await _list(transport, include_comments=True)

    assert result["comments_truncated"] is True
    assert "200" in result["comments_note"]


async def test_comment_and_field_hit_dedupe_to_one_entry(auth_patches):
    transport = CapturingTransport(
        _make_handler(
            relations=[],
            fields={"System.Description": f'<img src="{_org_url(GUID_A)}">'},
            comments=[{"id": 4, "text": f'again: <img src="{_org_url(GUID_A)}">'}],
        )
    )
    result = await _list(transport, include_comments=True)

    assert result["count"] == 1
    entry = result["attachments"][0]
    assert entry["fields"] == ["System.Description"]
    assert entry["comment_ids"] == [4]


# ---------------------------------------------------------------------------
# Hostile URLs in user-authored text
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("hostile", [HOSTILE_URL, FOREIGN_ORG_URL])
async def test_hostile_url_in_description_is_dropped_counted_and_never_echoed(auth_patches, hostile):
    transport = CapturingTransport(
        _make_handler(
            relations=[],
            fields={"System.Description": f'<img src="{hostile}">'},
        )
    )
    raw = await devops_list_work_item_attachments(
        ListWorkItemAttachmentsInput(work_item_id=42), _make_ctx(transport)
    )
    result = json.loads(raw)

    assert result["count"] == 0
    assert result["attachments"] == []
    assert result["rejected_urls"] == 1
    # The whole point: the payload is not relayed back to the model anywhere in
    # the response — not the URL, not its host, not its GUID.
    assert hostile not in raw
    assert "evil.example" not in raw
    assert "other-org" not in raw
    assert GUID_C not in raw
    assert "rejected_urls_note" in result


async def test_hostile_and_legitimate_urls_coexist(auth_patches):
    transport = CapturingTransport(
        _make_handler(
            relations=[],
            fields={
                "System.Description": (
                    f'<img src="{_org_url(GUID_A, "real.png")}">'
                    f'<img src="{HOSTILE_URL}">'
                ),
            },
        )
    )
    raw = await devops_list_work_item_attachments(
        ListWorkItemAttachmentsInput(work_item_id=42), _make_ctx(transport)
    )
    result = json.loads(raw)

    assert [e["attachment_id"] for e in result["attachments"]] == [GUID_A]
    assert result["rejected_urls"] == 1
    assert "evil.example" not in raw


async def test_repeated_hostile_url_counts_once(auth_patches):
    transport = CapturingTransport(
        _make_handler(
            relations=[],
            fields={
                "System.Description": f'<img src="{HOSTILE_URL}">',
                "Microsoft.VSTS.TCM.ReproSteps": f'<img src="{HOSTILE_URL}">',
            },
        )
    )
    result = await _list(transport)

    assert result["rejected_urls"] == 1


async def test_relation_naming_a_foreign_org_is_dropped_too(auth_patches):
    """A relation is service-stored, but it is still writable through the API."""
    transport = CapturingTransport(
        _make_handler(
            relations=[
                {"rel": "AttachedFile", "url": FOREIGN_ORG_URL, "attributes": {"name": "x.png"}},
                _relation(GUID_A),
            ]
        )
    )
    raw = await devops_list_work_item_attachments(
        ListWorkItemAttachmentsInput(work_item_id=42), _make_ctx(transport)
    )
    result = json.loads(raw)

    assert [e["attachment_id"] for e in result["attachments"]] == [GUID_A]
    assert result["rejected_urls"] == 1
    assert "other-org" not in raw


async def test_no_url_on_a_relation_is_ignored_without_counting(auth_patches):
    transport = CapturingTransport(
        _make_handler(relations=[{"rel": "AttachedFile", "attributes": {"name": "x.png"}}])
    )
    result = await _list(transport)

    assert result["count"] == 0
    assert result["rejected_urls"] == 0


# ---------------------------------------------------------------------------
# Request shape and errors
# ---------------------------------------------------------------------------


async def test_request_expands_relations_with_the_work_item_api_version(auth_patches):
    transport = CapturingTransport(_make_handler(relations=[_relation(GUID_A)]))
    await _list(transport)

    request = transport.requests[0]
    assert request.method == "GET"
    assert request.url.path == f"/{FAKE_ORG}/{FAKE_PROJECT}/_apis/wit/workitems/42"
    params = dict(request.url.params)
    # build_params() would have injected 7.1 here.
    assert params["api-version"] == WORKITEMS_API_VERSION
    assert params["$expand"] == "relations"
    # No `fields` projection — $expand already returns every large-text field.
    assert "fields" not in params


async def test_404_is_reported_as_actionable_json(auth_patches):
    transport = CapturingTransport(_make_handler(work_item_status=404))
    result = await _list(transport)

    assert result["error"] is True
    assert "42" in result["message"]
    assert FAKE_PROJECT in result["message"]


async def test_comments_error_is_reported_not_raised(auth_patches):
    transport = CapturingTransport(_make_handler(relations=[_relation(GUID_A)], comments_status=500))
    result = await _list(transport, include_comments=True)

    assert result["error"] is True


async def test_unresolvable_org_is_reported_as_json(build_headers_mock):
    transport = CapturingTransport(_make_handler())
    with patch("devops_mcp.tools.attachments.build_headers", new=build_headers_mock):
        app_ctx = MagicMock()
        app_ctx.organization = None
        app_ctx.project = None
        app_ctx.http_client = httpx.AsyncClient(transport=transport)
        mcp_ctx = MagicMock()
        mcp_ctx.request_context.lifespan_context = app_ctx

        result = json.loads(await devops_list_work_item_attachments(
            ListWorkItemAttachmentsInput(work_item_id=42), mcp_ctx
        ))

    assert result["error"] is True
    assert transport.requests == []


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


async def test_tool_is_registered_unconditionally():
    names = {tool.name for tool in await mcp.list_tools()}
    assert "devops_list_work_item_attachments" in names

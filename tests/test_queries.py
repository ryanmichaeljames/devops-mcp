"""Unit tests for the saved-query tools (devops_mcp.tools.queries).

The wit/queries group has four failure modes that are silent at runtime, so the
assertions below are mostly *wire* assertions rather than result assertions:

1. **Path encoding.** ``{query}`` may be a multi-segment path whose ``/`` are
   real route separators. Encoding it with ``safe=""`` yields ``%2F`` and the
   service answers 404 for a query that plainly exists — the failure reads as
   "missing query", not "bad encoding". Every addressing test asserts the
   separators survive and that ``%2F`` never appears in the path.
2. **api-version.** ``build_params()`` hard-codes ``api-version=7.1``, which is a
   different revision of these routes that mostly works — so a leak is invisible
   until a response field is missing. There is an explicit negative test that no
   request this module makes carries 7.1.
3. **PATCH content type.** ``wit/queries`` is merge-patch
   (``application/json``); ``wit/workitems``, one module away, requires
   ``application/json-patch+json`` and rejects plain JSON. Asserted both ways.
4. **The tree/oneHop result shape.** ``tree`` and ``oneHop`` runs answer
   ``workItemRelations`` and omit ``workItems`` entirely, so
   ``.get("workItems", [])`` reports zero rows for a query that returned
   hundreds. The tree fixture below deliberately omits ``workItems`` and repeats
   ids across relations, which is the regression test for that.

All HTTP is intercepted by a capturing transport — no network, no credentials.
"""

import json
import os
import subprocess
import sys
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from devops_mcp.models import (
    CreateQueryFolderInput,
    CreateQueryInput,
    DeleteQueryInput,
    GetQueryInput,
    ListQueriesInput,
    RunQueryInput,
    UpdateQueryInput,
)
from devops_mcp.tools.queries import (
    devops_create_query,
    devops_create_query_folder,
    devops_delete_query,
    devops_get_query,
    devops_list_queries,
    devops_run_query,
    devops_update_query,
)

FAKE_ORG = "testorg"
# A space in the project name proves the single-segment (safe="") encoding of
# org/project still applies around the multi-segment query path.
FAKE_PROJECT = "Test Project"
QUERY_GUID = "8a8c8212-15ca-41ed-97aa-1d6fbfbcd581"
DEST_GUID = "1cf0f6b2-1e1d-4b1a-9c0a-6f1d0e5f2a11"
QUERY_PATH = "Shared Queries/Team A/My Query"

QUERIES_API_VERSION = "7.2-preview.2"
WIQL_API_VERSION = "7.2-preview.2"
BATCH_API_VERSION = "7.2-preview.1"


# ---------------------------------------------------------------------------
# Transport / context plumbing
# ---------------------------------------------------------------------------


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


def _make_ctx(handler: Callable[[httpx.Request], httpx.Response]):
    transport = CapturingTransport(handler)
    app_ctx = MagicMock()
    app_ctx.organization = FAKE_ORG
    app_ctx.project = FAKE_PROJECT
    app_ctx.http_client = httpx.AsyncClient(transport=transport)
    mcp_ctx = MagicMock()
    mcp_ctx.request_context.lifespan_context = app_ctx
    return transport, mcp_ctx


def _auth_patches():
    """Bypass real auth and org/project resolution in the queries namespace."""
    fake_headers = {"Authorization": "Bearer fake-token", "Accept": "application/json"}

    async def _build_headers(_app_ctx, *, include_content_type: bool = False, extra=None):
        headers = dict(fake_headers)
        if include_content_type:
            headers["Content-Type"] = "application/json"
        if extra:
            headers.update(extra)
        return headers

    return [
        patch("devops_mcp.tools.queries.build_headers", new=AsyncMock(side_effect=_build_headers)),
        patch("devops_mcp.tools.queries.resolve_org", return_value=FAKE_ORG),
        patch("devops_mcp.tools.queries.resolve_project", return_value=FAKE_PROJECT),
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


def _raw_path(request: httpx.Request) -> str:
    """The percent-encoded path only (raw_path also carries the query string)."""
    return request.url.raw_path.decode().split("?")[0]


def _params(request: httpx.Request) -> dict:
    return dict(request.url.params)


# ---------------------------------------------------------------------------
# Fixtures / payload builders
# ---------------------------------------------------------------------------


def _query_item(**overrides) -> dict:
    item = {
        "id": QUERY_GUID,
        "name": "My Query",
        "path": QUERY_PATH,
        "isFolder": False,
        "isPublic": True,
        "queryType": "flat",
        "wiql": "SELECT [System.Id] FROM WorkItems",
        "createdBy": {
            "displayName": "Ada Lovelace",
            "imageUrl": "https://example.invalid/avatar",
            "descriptor": "aad.abc",
            "uniqueName": "ada@example.invalid",
        },
        "lastModifiedDate": "2026-08-13T10:00:00Z",
        "columns": [
            {"referenceName": "System.Id", "name": "ID", "url": "https://example.invalid/f/1"},
            {"referenceName": "System.Title", "name": "Title", "url": "https://example.invalid/f/2"},
        ],
        "sortColumns": [
            {"field": {"referenceName": "System.Id", "url": "https://example.invalid/f/1"},
             "descending": False}
        ],
        "_links": {
            "self": {"href": "https://example.invalid/self"},
            "html": {"href": "https://dev.azure.com/testorg/_queries/query/8a8c8212"},
            "parent": {"href": "https://example.invalid/parent"},
        },
        "url": "https://example.invalid/url",
    }
    item.update(overrides)
    return item


def _folder_item(name: str, children: list | None = None) -> dict:
    folder = {
        "id": f"folder-{name}",
        "name": name,
        "path": name,
        "isFolder": True,
        "hasChildren": bool(children),
        "isPublic": name == "Shared Queries",
    }
    if children is not None:
        folder["children"] = children
    return folder


def _flat_run_result(ids: list[int]) -> dict:
    """A 'flat' run result: workItems present, workItemRelations ABSENT."""
    return {
        "queryType": "flat",
        "queryResultType": "workItem",
        "asOf": "2026-08-13T10:00:00Z",
        "columns": [
            {"referenceName": "System.Id", "name": "ID"},
            {"referenceName": "System.Title", "name": "Title"},
        ],
        "workItems": [{"id": i, "url": f"https://example.invalid/wi/{i}"} for i in ids],
    }


def _tree_run_result() -> dict:
    """A 'tree' run result: workItemRelations present, workItems ABSENT.

    Row 0 is a ROOT row — the real service sends rel/source as null there. Ids
    repeat across rows (2 and 3 each appear twice), which is what forces the
    de-duplication before batching.
    """
    return {
        "queryType": "tree",
        "queryResultType": "workItemLink",
        "asOf": "2026-08-13T10:00:00Z",
        "columns": [{"referenceName": "System.Id", "name": "ID"}],
        "workItemRelations": [
            {"rel": None, "source": None, "target": {"id": 1, "url": "https://example.invalid/wi/1"}},
            {"rel": "System.LinkTypes.Hierarchy-Forward",
             "source": {"id": 1, "url": "https://example.invalid/wi/1"},
             "target": {"id": 2, "url": "https://example.invalid/wi/2"}},
            {"rel": "System.LinkTypes.Hierarchy-Forward",
             "source": {"id": 1, "url": "https://example.invalid/wi/1"},
             "target": {"id": 3, "url": "https://example.invalid/wi/3"}},
            {"rel": "System.LinkTypes.Related",
             "source": {"id": 2, "url": "https://example.invalid/wi/2"},
             "target": {"id": 3, "url": "https://example.invalid/wi/3"}},
        ],
    }


def _batch_body(request: httpx.Request) -> dict:
    return json.loads(request.content)


def _universal_handler(request: httpx.Request) -> httpx.Response:
    """Answer every route this module touches, plausibly."""
    path = _raw_path(request)
    if "/wit/workitemsbatch" in path:
        ids = _batch_body(request)["ids"]
        return _json_response(
            200,
            {"count": len(ids), "value": [{"id": i, "fields": {"System.Title": f"WI {i}"}} for i in ids]},
            request,
        )
    if "/wit/wiql/" in path:
        return _json_response(200, _flat_run_result([1, 2]), request)
    if "/wit/queries" in path:
        if request.method == "GET":
            if path.endswith("/wit/queries"):
                return _json_response(
                    200,
                    {"count": 2, "value": [_folder_item("My Queries"), _folder_item("Shared Queries")]},
                    request,
                )
            return _json_response(200, _query_item(), request)
        if request.method == "POST":
            return _json_response(201, _query_item(), request)
        if request.method == "PATCH":
            return _json_response(200, _query_item(), request)
        if request.method == "DELETE":
            return httpx.Response(204, request=request)
    raise AssertionError(f"Unexpected request: {request.method} {request.url}")


# ---------------------------------------------------------------------------
# Path encoding — the highest-value assertions in this file
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_path_separators_survive_encoding():
    """A query path's '/' are route separators; its spaces become %20, not %2F."""
    transport, mcp_ctx = _make_ctx(_universal_handler)

    result = await _call(devops_get_query, GetQueryInput(query=QUERY_PATH), mcp_ctx)

    assert "error" not in result, result
    path = _raw_path(transport.requests[0])
    assert path.endswith("/_apis/wit/queries/Shared%20Queries/Team%20A/My%20Query"), path
    assert "%2F" not in path, (
        "Encoding the query path with safe='' turns its separators into %2F and the "
        "service answers 404 for a query that exists."
    )
    # Three literal separators inside the query reference itself.
    assert path.split("/_apis/wit/queries/")[1].count("/") == 2


@pytest.mark.asyncio
async def test_query_path_special_characters_are_encoded():
    """'&' and '#' in a query name are percent-encoded; separators stay literal."""
    transport, mcp_ctx = _make_ctx(_universal_handler)

    result = await _call(
        devops_get_query, GetQueryInput(query="Shared Queries/R&D/Bugs #1"), mcp_ctx
    )

    assert "error" not in result, result
    path = _raw_path(transport.requests[0])
    assert path.endswith("/_apis/wit/queries/Shared%20Queries/R%26D/Bugs%20%231"), path
    assert "%2F" not in path


async def _exercise_every_path_addressed_tool(mcp_ctx) -> list[dict]:
    """Every tool, addressing a QUERY PATH (never a GUID) wherever it can."""
    return [
        await _call(devops_list_queries, ListQueriesInput(), mcp_ctx),
        await _call(devops_get_query, GetQueryInput(query=QUERY_PATH), mcp_ctx),
        await _call(devops_run_query, RunQueryInput(query=QUERY_PATH), mcp_ctx),
        await _call(
            devops_create_query,
            CreateQueryInput(parent="Shared Queries/Team A", name="Q",
                             wiql="SELECT [System.Id] FROM WorkItems"),
            mcp_ctx,
        ),
        await _call(
            devops_create_query_folder,
            CreateQueryFolderInput(parent="Shared Queries/Team A", name="F"),
            mcp_ctx,
        ),
        await _call(
            devops_update_query,
            UpdateQueryInput(query=QUERY_PATH, name="R", move_to="Shared Queries/Archive"),
            mcp_ctx,
        ),
        await _call(devops_delete_query, DeleteQueryInput(query=QUERY_PATH), mcp_ctx),
    ]


@pytest.mark.asyncio
async def test_no_request_path_is_ever_double_encoded():
    """A space must reach the wire as %20 — never %2520, never %2F.

    One live call (the first of a session, never reproduced across 6+ identical
    runs afterwards) went out with 'Shared%2520Queries' and 404'd: a double
    encoding of a path the encoder builds deterministically. The cause was never
    found and neither _queries_url nor httpx was shown to re-encode, so this is a
    cheap wire-level tripwire rather than a fix — if a '%25' (an encoded '%')
    ever appears in a path this module builds, it fails here instead of in
    production.
    """
    transport, mcp_ctx = _make_ctx(_universal_handler)

    results = await _exercise_every_path_addressed_tool(mcp_ctx)
    assert all("error" not in r for r in results), results

    assert transport.requests, "expected the tools to issue requests"
    for request in transport.requests:
        path = _raw_path(request)
        assert "%25" not in path, f"double-encoded path: {path}"
        assert "%2F" not in path, f"path separator encoded away: {path}"
        assert "%2f" not in path, f"path separator encoded away: {path}"
    assert any("Shared%20Queries" in _raw_path(r) for r in transport.requests)


@pytest.mark.asyncio
async def test_org_and_project_are_single_segment_encoded():
    """org/project keep safe='' encoding even though the query path does not."""
    transport, mcp_ctx = _make_ctx(_universal_handler)

    await _call(devops_get_query, GetQueryInput(query=QUERY_PATH), mcp_ctx)

    path = _raw_path(transport.requests[0])
    assert path.startswith("/testorg/Test%20Project/_apis/"), path


@pytest.mark.asyncio
async def test_query_guid_is_used_verbatim():
    """A GUID reference addresses the same route with no path segments."""
    transport, mcp_ctx = _make_ctx(_universal_handler)

    await _call(devops_get_query, GetQueryInput(query=QUERY_GUID), mcp_ctx)

    assert _raw_path(transport.requests[0]).endswith(f"/_apis/wit/queries/{QUERY_GUID}")


@pytest.mark.parametrize(
    "bad_ref, expected_fragment",
    [
        ("..", "'.' or '..'"),
        ("Shared Queries/../..", "'.' or '..'"),
        ("/Shared Queries", "start or end with '/'"),
        ("Shared Queries/", "start or end with '/'"),
        ("Shared Queries//Sub", "empty path segment"),
        ("Junk/Thing", "'Shared Queries' or 'My Queries'"),
        ("Personal", "'Shared Queries' or 'My Queries'"),
    ],
)
@pytest.mark.asyncio
async def test_invalid_query_reference_is_rejected_before_any_http_call(bad_ref, expected_fragment):
    """A malformed reference is a client-side error — the transport sees nothing."""
    transport, mcp_ctx = _make_ctx(_universal_handler)

    result = await _call(devops_get_query, GetQueryInput(query=bad_ref), mcp_ctx)

    assert result.get("error") is True, result
    assert expected_fragment in result["message"], result["message"]
    assert transport.requests == [], "No HTTP request may be made for an invalid reference"


@pytest.mark.asyncio
async def test_invalid_move_destination_is_rejected_before_any_http_call():
    """move_to is validated with the same rules as query, before the PATCH."""
    transport, mcp_ctx = _make_ctx(_universal_handler)

    result = await _call(
        devops_update_query,
        UpdateQueryInput(query=QUERY_GUID, name="Renamed", move_to="Junk/Folder"),
        mcp_ctx,
    )

    assert result.get("error") is True
    assert "'move_to'" in result["message"]
    assert transport.requests == []


# ---------------------------------------------------------------------------
# api-version — including the build_params()/7.1 leak negative test
# ---------------------------------------------------------------------------


async def _exercise_every_tool(mcp_ctx) -> list[dict]:
    return [
        await _call(devops_list_queries, ListQueriesInput(), mcp_ctx),
        await _call(devops_list_queries, ListQueriesInput(name_filter="bugs"), mcp_ctx),
        await _call(devops_get_query, GetQueryInput(query=QUERY_PATH), mcp_ctx),
        await _call(devops_run_query, RunQueryInput(query=QUERY_PATH), mcp_ctx),
        await _call(devops_create_query, CreateQueryInput(name="Q", wiql="SELECT [System.Id] FROM WorkItems"), mcp_ctx),
        await _call(devops_create_query_folder, CreateQueryFolderInput(name="F"), mcp_ctx),
        await _call(devops_update_query, UpdateQueryInput(query=QUERY_GUID, name="R", move_to=DEST_GUID), mcp_ctx),
        await _call(devops_delete_query, DeleteQueryInput(query=QUERY_GUID), mcp_ctx),
    ]


@pytest.mark.asyncio
async def test_every_request_carries_the_right_api_version():
    """Each route gets its own documented revision — asserted as a literal string."""
    transport, mcp_ctx = _make_ctx(_universal_handler)

    results = await _exercise_every_tool(mcp_ctx)
    assert all("error" not in r for r in results), results

    assert transport.requests, "expected the tools to issue requests"
    for request in transport.requests:
        path = _raw_path(request)
        version = _params(request).get("api-version")
        if "/wit/workitemsbatch" in path:
            expected = BATCH_API_VERSION
        elif "/wit/wiql/" in path:
            expected = WIQL_API_VERSION
        else:
            expected = QUERIES_API_VERSION
        assert version == expected, f"{request.method} {path} sent api-version={version!r}"


@pytest.mark.asyncio
async def test_no_request_carries_api_version_7_1():
    """build_params() hard-codes 7.1; using it here is silently wrong, not an error."""
    transport, mcp_ctx = _make_ctx(_universal_handler)

    await _exercise_every_tool(mcp_ctx)

    leaked = [
        f"{r.method} {_raw_path(r)}"
        for r in transport.requests
        if _params(r).get("api-version") == "7.1"
    ]
    assert leaked == [], f"build_params() leaked api-version=7.1 into: {leaked}"


# ---------------------------------------------------------------------------
# '$'-prefixed parameters (and the one without a '$')
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_hierarchy_parameters():
    """$depth / $expand / $includeDeleted all reach the wire WITH the '$'."""
    transport, mcp_ctx = _make_ctx(_universal_handler)

    result = await _call(
        devops_list_queries,
        ListQueriesInput(depth=2, expand="wiql", include_deleted=True),
        mcp_ctx,
    )

    assert "error" not in result, result
    sent = _params(transport.requests[0])
    assert sent["$depth"] == "2"
    assert sent["$expand"] == "wiql"
    assert sent["$includeDeleted"] == "true"
    assert "$filter" not in sent
    assert "depth" not in sent and "expand" not in sent and "includeDeleted" not in sent
    assert result["mode"] == "hierarchy"


@pytest.mark.asyncio
async def test_list_search_parameters_and_shape():
    """$filter switches the route to search; $top rides along; hasMore is surfaced."""

    def handler(request):
        return _json_response(
            200,
            {"count": 1, "value": [_query_item(name="Active Bugs")], "hasMore": True},
            request,
        )

    transport, mcp_ctx = _make_ctx(handler)

    result = await _call(
        devops_list_queries, ListQueriesInput(name_filter="bug", top=25), mcp_ctx
    )

    sent = _params(transport.requests[0])
    assert sent["$filter"] == "bug"
    assert sent["$top"] == "25"
    assert "$depth" not in sent, "depth is meaningless in search mode"
    assert result["mode"] == "search"
    assert result["count"] == 1
    assert result["has_more"] is True
    assert "narrow" in result["note"]


@pytest.mark.asyncio
async def test_get_query_parameters():
    """$expand / $depth / $includeDeleted on Get."""
    transport, mcp_ctx = _make_ctx(_universal_handler)

    await _call(
        devops_get_query,
        GetQueryInput(query=QUERY_GUID, expand="clauses", depth=1, include_deleted=True),
        mcp_ctx,
    )

    sent = _params(transport.requests[0])
    assert sent["$expand"] == "clauses"
    assert sent["$depth"] == "1"
    assert sent["$includeDeleted"] == "true"


@pytest.mark.asyncio
async def test_validate_wiql_only_has_no_dollar_prefix():
    """validateWiqlOnly is the ONE parameter in this group without a '$'."""
    transport, mcp_ctx = _make_ctx(_universal_handler)

    result = await _call(
        devops_create_query,
        CreateQueryInput(name="Q", wiql="SELECT [System.Id] FROM WorkItems", validate_only=True),
        mcp_ctx,
    )

    sent = _params(transport.requests[0])
    assert sent["validateWiqlOnly"] == "true"
    assert "$validateWiqlOnly" not in sent, "a '$' here is silently ignored — the query is created"
    assert result["validated_only"] is True


@pytest.mark.asyncio
async def test_validate_only_with_an_empty_body_still_reports_success():
    """A validate-only create may answer without a body — not a row of nulls."""
    _transport, mcp_ctx = _make_ctx(lambda request: httpx.Response(200, request=request))

    result = await _call(
        devops_create_query,
        CreateQueryInput(name="Q", wiql="SELECT [System.Id] FROM WorkItems", validate_only=True),
        mcp_ctx,
    )

    assert result["validated_only"] is True
    assert "Nothing was created" in result["message"]
    assert "id" not in result


def _undelete_handler(is_deleted: bool | None, *, precheck_status: int = 200):
    """GET answers the pre-undelete state; PATCH answers the restored item.

    ``is_deleted=None`` models a body that simply omits the key.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            if precheck_status != 200:
                return _json_response(
                    precheck_status, {"message": "TF401243: query does not exist"}, request
                )
            body = _query_item()
            if is_deleted is not None:
                body["isDeleted"] = is_deleted
            return _json_response(200, body, request)
        if request.method == "PATCH":
            return _json_response(200, _query_item(), request)
        raise AssertionError(f"Unexpected request: {request.method} {_raw_path(request)}")

    return handler


@pytest.mark.asyncio
async def test_undelete_descendants_has_a_dollar_prefix():
    """$undeleteDescendants keeps its '$'; the PATCH body is the undelete flag."""
    transport, mcp_ctx = _make_ctx(_undelete_handler(True))

    result = await _call(
        devops_update_query,
        UpdateQueryInput(query=QUERY_GUID, undelete=True, undelete_descendants=True),
        mcp_ctx,
    )

    request = next(r for r in transport.requests if r.method == "PATCH")
    assert _params(request)["$undeleteDescendants"] == "true"
    assert json.loads(request.content) == {"isDeleted": False}
    assert result["undeleted"] is True
    assert "NOT restored" in result["note"]


@pytest.mark.asyncio
async def test_undelete_reads_the_deleted_state_before_patching():
    """The pre-check is a GET with $includeDeleted — it is what makes the report true."""
    transport, mcp_ctx = _make_ctx(_undelete_handler(True))

    result = await _call(
        devops_update_query, UpdateQueryInput(query=QUERY_GUID, undelete=True), mcp_ctx
    )

    assert [r.method for r in transport.requests] == ["GET", "PATCH"]
    assert _params(transport.requests[0])["$includeDeleted"] == "true"
    assert result["undelete_state"] == "restored"
    assert result["undeleted"] is True


@pytest.mark.asyncio
async def test_undelete_of_an_already_live_item_does_not_claim_a_restore():
    """Undelete only cascades on the deleted -> live TRANSITION.

    Re-issuing undelete + undelete_descendants against a live folder answers 200
    and leaves its descendants deleted. Reporting 'restored' off that 200 is a
    confidently wrong answer, so the state is read first.
    """
    _transport, mcp_ctx = _make_ctx(_undelete_handler(False))

    result = await _call(
        devops_update_query,
        UpdateQueryInput(query=QUERY_GUID, undelete=True, undelete_descendants=True),
        mcp_ctx,
    )

    assert result["undeleted"] is False
    assert result["undelete_state"] == "already_live"
    assert "Nothing was restored" in result["note"]
    assert "NO effect" in result["note"], "the descendants cascade must not be implied"
    assert "Content was restored" not in result["note"]


@pytest.mark.asyncio
async def test_restoring_a_folder_without_descendants_says_they_are_still_deleted():
    """The cascade fires only on the transition — which has now already happened.

    Restoring a folder without undelete_descendants leaves every query inside it
    deleted, and a second call with the flag will NOT fix it. Saying so is the
    difference between a caller recovering the queries and quietly losing them.
    """
    _transport, mcp_ctx = _make_ctx(_undelete_handler(True))

    result = await _call(
        devops_update_query,
        UpdateQueryInput(query=QUERY_GUID, undelete=True, undelete_descendants=False),
        mcp_ctx,
    )

    assert result["undelete_state"] == "restored"
    assert result["undeleted"] is True
    assert "STILL deleted" in result["note"]
    assert "will not fix that" in result["note"]
    assert "devops_list_queries(include_deleted=True)" in result["note"], (
        "the note must name the only way to reach the descendants: their GUIDs"
    )


@pytest.mark.asyncio
async def test_undelete_treats_a_missing_isdeleted_key_as_live():
    """$includeDeleted exists to surface the key — absent means not deleted."""
    _transport, mcp_ctx = _make_ctx(_undelete_handler(None))

    result = await _call(
        devops_update_query, UpdateQueryInput(query=QUERY_GUID, undelete=True), mcp_ctx
    )

    assert result["undelete_state"] == "already_live"


@pytest.mark.asyncio
async def test_a_failed_undelete_precheck_hedges_instead_of_failing_the_update():
    """The pre-check is best-effort: it must not turn a working undelete into an error."""
    transport, mcp_ctx = _make_ctx(_undelete_handler(True, precheck_status=404))

    result = await _call(
        devops_update_query, UpdateQueryInput(query=QUERY_GUID, undelete=True), mcp_ctx
    )

    assert "error" not in result, result
    assert [r.method for r in transport.requests] == ["GET", "PATCH"]
    assert result["undelete_state"] == "unverified"
    assert "could not be read" in result["note"]
    # The deliberate contract: 'undeleted' reports what was DONE (the PATCH was
    # issued and accepted), matching renamed/wiql_updated/moved beside it;
    # 'undelete_state' is the field that reports what is KNOWN. The one case that
    # flips 'undeleted' to False is 'already_live', where the service told us
    # plainly that nothing was restored.
    assert result["undeleted"] is True


@pytest.mark.asyncio
async def test_a_rename_by_path_pays_for_no_pre_read_at_all():
    """A path ref answers both pre-checks for free: no undelete GET, no root GET.

    Only a root has a single-segment path, so a multi-segment one is provably not
    a root; and without undelete there is no state to read.
    """
    transport, mcp_ctx = _make_ctx(_universal_handler)

    await _call(devops_update_query, UpdateQueryInput(query=QUERY_PATH, name="R"), mcp_ctx)

    assert [r.method for r in transport.requests] == ["PATCH"]


@pytest.mark.asyncio
async def test_a_rename_by_guid_pays_for_exactly_one_pre_read():
    """A GUID hides what it points at, so the root guard costs one GET — only one.

    The undelete pre-check and the root check share that single read; a rename
    that is not an undelete must not add a second.
    """
    transport, mcp_ctx = _make_ctx(_universal_handler)

    await _call(devops_update_query, UpdateQueryInput(query=QUERY_GUID, name="R"), mcp_ctx)

    assert [r.method for r in transport.requests] == ["GET", "PATCH"]


@pytest.mark.asyncio
async def test_an_undelete_and_rename_by_guid_share_one_pre_read():
    """Both pre-checks read the same item — that is one GET, not two."""
    transport, mcp_ctx = _make_ctx(_undelete_handler(True))

    result = await _call(
        devops_update_query,
        UpdateQueryInput(query=QUERY_GUID, name="R", undelete=True),
        mcp_ctx,
    )

    assert [r.method for r in transport.requests] == ["GET", "PATCH"]
    assert result["undelete_state"] == "restored"
    assert result["renamed"] is True


@pytest.mark.asyncio
async def test_a_wiql_edit_alone_pays_for_no_pre_read():
    """Only a rename or a move changes an item's address — a WIQL edit does not."""
    transport, mcp_ctx = _make_ctx(_universal_handler)

    await _call(
        devops_update_query,
        UpdateQueryInput(query=QUERY_GUID, wiql="SELECT [System.Id] FROM WorkItems"),
        mcp_ctx,
    )

    assert [r.method for r in transport.requests] == ["PATCH"]


@pytest.mark.asyncio
async def test_run_query_parameters():
    """$top keeps its '$'; timePrecision does not have one."""
    transport, mcp_ctx = _make_ctx(_universal_handler)

    await _call(
        devops_run_query,
        RunQueryInput(query=QUERY_GUID, top=5, time_precision=True, fetch_details=False),
        mcp_ctx,
    )

    sent = _params(transport.requests[0])
    assert sent["$top"] == "5"
    assert sent["timePrecision"] == "true"
    assert "$timePrecision" not in sent


# ---------------------------------------------------------------------------
# PATCH is merge-patch, NOT JSON Patch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_uses_merge_patch_content_type_and_partial_body():
    """PATCH wit/queries takes application/json with only the changed keys.

    The neighbouring wit/workitems PATCH requires application/json-patch+json and
    rejects plain JSON — copying it here produces a 400 whose message says
    nothing about content type.
    """
    transport, mcp_ctx = _make_ctx(_universal_handler)

    result = await _call(
        devops_update_query, UpdateQueryInput(query=QUERY_GUID, name="Website"), mcp_ctx
    )

    assert "error" not in result, result
    # requests[0] is the root-folder guard's read of what the GUID points at.
    request = transport.requests[1]
    assert request.method == "PATCH"
    assert request.headers["content-type"] == "application/json"
    body = json.loads(request.content)
    assert body == {"name": "Website"}, "absent keys must be absent, not null"
    assert not isinstance(body, list), "a JSON-Patch array is the wrong shape for this route"
    assert result["renamed"] is True
    assert result["wiql_updated"] is False
    assert result["moved"] is False


@pytest.mark.asyncio
async def test_module_never_sends_json_patch_content_type():
    """No request from this module may declare application/json-patch+json."""
    transport, mcp_ctx = _make_ctx(_universal_handler)

    await _exercise_every_tool(mcp_ctx)

    offenders = [
        f"{r.method} {_raw_path(r)}"
        for r in transport.requests
        if "json-patch" in r.headers.get("content-type", "")
    ]
    assert offenders == [], offenders


@pytest.mark.asyncio
async def test_update_wiql_only_sends_wiql():
    transport, mcp_ctx = _make_ctx(_universal_handler)

    result = await _call(
        devops_update_query,
        UpdateQueryInput(query=QUERY_PATH, wiql="SELECT [System.Id] FROM WorkItems WHERE [System.State] = 'Active'"),
        mcp_ctx,
    )

    body = json.loads(transport.requests[0].content)
    assert list(body) == ["wiql"]
    assert result["wiql_updated"] is True
    # queryType is echoed back so a flat -> tree flip is visible to the caller.
    assert result["query_type"] == "flat"


# ---------------------------------------------------------------------------
# Create bodies
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_query_body_is_name_and_wiql_only():
    """queryType/columns/sortColumns are derived server-side — never sent."""
    transport, mcp_ctx = _make_ctx(_universal_handler)

    wiql = "SELECT [System.Id] FROM WorkItems WHERE [System.WorkItemType] = 'Bug'"
    result = await _call(
        devops_create_query,
        CreateQueryInput(parent="Shared Queries/Team A", name="All Bugs", wiql=wiql),
        mcp_ctx,
    )

    request = transport.requests[0]
    assert request.method == "POST"
    assert _raw_path(request).endswith("/_apis/wit/queries/Shared%20Queries/Team%20A")
    assert json.loads(request.content) == {"name": "All Bugs", "wiql": wiql}
    assert request.headers["content-type"] == "application/json"
    # 201 (the documented sample) is accepted without branching on the status.
    assert result["id"] == QUERY_GUID
    assert result["validated_only"] is False
    assert result["columns"] == ["System.Id", "System.Title"]


@pytest.mark.asyncio
async def test_create_folder_body_is_name_and_is_folder_only():
    """A folder must not carry wiql; a query must not set isFolder."""
    transport, mcp_ctx = _make_ctx(
        lambda request: _json_response(
            201, _query_item(name="Website team", path="Shared Queries/Website team",
                             isFolder=True, wiql=None, queryType=None), request
        )
    )

    result = await _call(
        devops_create_query_folder, CreateQueryFolderInput(name="Website team"), mcp_ctx
    )

    body = json.loads(transport.requests[0].content)
    assert body == {"name": "Website team", "isFolder": True}
    assert "wiql" not in body
    assert result["is_folder"] is True


@pytest.mark.asyncio
async def test_create_returns_201_and_move_returns_200():
    """Same endpoint, two success statuses — neither is branched on."""
    statuses = {"POST": [201, 200]}

    def handler(request):
        if request.method == "POST":
            return _json_response(statuses["POST"].pop(0), _query_item(), request)
        return _json_response(200, _query_item(), request)

    _transport, mcp_ctx = _make_ctx(handler)

    created = await _call(
        devops_create_query,
        CreateQueryInput(name="Q", wiql="SELECT [System.Id] FROM WorkItems"),
        mcp_ctx,
    )
    moved = await _call(
        devops_update_query, UpdateQueryInput(query=QUERY_GUID, move_to=DEST_GUID), mcp_ctx
    )

    assert "error" not in created and created["id"] == QUERY_GUID
    assert "error" not in moved and moved["moved"] is True


# ---------------------------------------------------------------------------
# Move = POST to Create on the destination
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_move_only_posts_id_to_destination_and_issues_no_patch():
    transport, mcp_ctx = _make_ctx(_universal_handler)

    result = await _call(
        devops_update_query, UpdateQueryInput(query=QUERY_GUID, move_to="Shared Queries/Archive"), mcp_ctx
    )

    assert "error" not in result, result
    assert [r.method for r in transport.requests] == ["GET", "POST"], (
        "a move is a POST to Create on the destination — not a PATCH of 'path'; "
        "the GET is the root-folder guard reading what the GUID points at"
    )
    request = transport.requests[1]
    assert _raw_path(request).endswith("/_apis/wit/queries/Shared%20Queries/Archive")
    assert json.loads(request.content) == {"id": QUERY_GUID}
    assert result["moved"] is True


@pytest.mark.asyncio
async def test_move_by_path_resolves_the_guid_first():
    """Moving a path-addressed item needs its GUID, so one resolve GET precedes."""
    transport, mcp_ctx = _make_ctx(_universal_handler)

    result = await _call(
        devops_update_query, UpdateQueryInput(query=QUERY_PATH, move_to=DEST_GUID), mcp_ctx
    )

    assert "error" not in result, result
    assert [r.method for r in transport.requests] == ["GET", "POST"]
    assert json.loads(transport.requests[1].content) == {"id": QUERY_GUID}


@pytest.mark.asyncio
async def test_rename_then_move_uses_the_patch_response_id():
    """PATCH first (so the returned path is final), and no extra resolve GET."""
    transport, mcp_ctx = _make_ctx(_universal_handler)

    result = await _call(
        devops_update_query,
        UpdateQueryInput(query=QUERY_PATH, name="Renamed", move_to=DEST_GUID),
        mcp_ctx,
    )

    assert [r.method for r in transport.requests] == ["PATCH", "POST"]
    assert json.loads(transport.requests[1].content) == {"id": QUERY_GUID}
    assert result["renamed"] is True and result["moved"] is True


@pytest.mark.asyncio
async def test_a_bodiless_patch_never_re_resolves_by_the_stale_pre_rename_path():
    """The rename has already moved the path's leaf — looking up the old one 404s."""

    def handler(request):
        if request.method == "PATCH":
            return httpx.Response(204, request=request)  # documented body, absent in practice
        if request.method == "GET":
            path = _raw_path(request)
            assert path.endswith("/_apis/wit/queries/Shared%20Queries/Team%20A/Renamed"), (
                f"resolved by a stale, pre-rename reference: {path}"
            )
            return _json_response(200, _query_item(name="Renamed"), request)
        if request.method == "POST":
            return _json_response(200, _query_item(name="Renamed"), request)
        raise AssertionError(f"Unexpected request: {request.method}")

    transport, mcp_ctx = _make_ctx(handler)

    result = await _call(
        devops_update_query,
        UpdateQueryInput(query=QUERY_PATH, name="Renamed", move_to=DEST_GUID),
        mcp_ctx,
    )

    assert "error" not in result, result
    assert [r.method for r in transport.requests] == ["PATCH", "GET", "POST"]
    assert json.loads(transport.requests[2].content) == {"id": QUERY_GUID}


@pytest.mark.asyncio
async def test_a_bodiless_patch_on_a_guid_ref_re_resolves_by_that_same_guid():
    """A GUID never goes stale, so a rename must not rewrite it into a name.

    (The bodiless PATCH itself is a contract allowance, not an observed one: live,
    a rename PATCH always answers 200 with the full item — see the comment on the
    fallback in devops_update_query.)
    """
    seen: list[str] = []

    def handler(request):
        path = _raw_path(request)
        if request.method == "GET":
            seen.append(path)
            return _json_response(200, _query_item(), request)
        if request.method == "PATCH":
            return httpx.Response(204, request=request)
        if request.method == "POST":
            return _json_response(200, _query_item(name="Renamed"), request)
        raise AssertionError(f"Unexpected request: {request.method}")

    transport, mcp_ctx = _make_ctx(handler)

    result = await _call(
        devops_update_query,
        UpdateQueryInput(query=QUERY_GUID, name="Renamed", move_to=DEST_GUID),
        mcp_ctx,
    )

    assert "error" not in result, result
    # GET (root guard), PATCH (bodiless), POST (move). There is NO re-resolve GET:
    # _renamed_ref hands the GUID back untouched — it must never rewrite it into
    # the new NAME — and a GUID needs no resolve hop.
    assert [r.method for r in transport.requests] == ["GET", "PATCH", "POST"]
    assert all(p.endswith(f"/_apis/wit/queries/{QUERY_GUID}") for p in seen), seen
    assert json.loads(transport.requests[2].content) == {"id": QUERY_GUID}


@pytest.mark.asyncio
async def test_a_move_id_that_is_not_a_guid_is_refused():
    def handler(request):
        if request.method == "GET":
            return _json_response(200, {"id": "folder-Team A", "name": "Team A"}, request)
        raise AssertionError(f"Unexpected request: {request.method}")

    transport, mcp_ctx = _make_ctx(handler)

    result = await _call(
        devops_update_query, UpdateQueryInput(query=QUERY_PATH, move_to=DEST_GUID), mcp_ctx
    )

    assert result["error"] is True
    assert "no usable id" in result["message"]
    assert not [r for r in transport.requests if r.method == "POST"]


# ---------------------------------------------------------------------------
# The root-folder guard
# ---------------------------------------------------------------------------
#
# Verified live: PATCH wit/queries/Shared Queries with {"name": "whatever"}
# answers HTTP 200 and really renames the root. Nothing beneath it is addressable
# by path afterwards — a query path must start with 'Shared Queries' or 'My
# Queries' — so the whole hierarchy is reachable only by GUID from then on, and
# the only way back is renaming the root, by GUID, to a valid root name.

ROOT_GUID = "3f2b1a0c-9d8e-4f7a-8b6c-5d4e3f2a1b0c"


def _root_folder_item(name: str = "Shared Queries") -> dict:
    """A hierarchy root, exactly as the service reports one (live-observed shape).

    The renamed case is the same shape with a different name/path — which is the
    whole reason the guard cannot key off the name.
    """
    return {
        "id": ROOT_GUID,
        "name": name,
        "path": name,
        "isFolder": True,
        "hasChildren": True,
        "isPublic": True,
    }


@pytest.mark.parametrize(
    "item, is_root",
    [
        (_root_folder_item(), True),
        # A root that was ALREADY renamed still is one; a name check misses it.
        (_root_folder_item("whatever"), True),
        # A folder one level down carries its parent in the path.
        ({"isFolder": True, "path": "Shared Queries/Team A", "name": "Team A"}, False),
        # A query, not a folder.
        ({"isFolder": False, "path": "Shared Queries", "name": "Shared Queries"}, False),
        ({}, False),
    ],
)
def test_root_folder_predicate(item, is_root):
    """isFolder + a single-segment path is the durable signal, not the name."""
    from devops_mcp.tools.queries import _is_root_folder

    assert _is_root_folder(item) is is_root


@pytest.mark.parametrize("root", ["Shared Queries", "My Queries"])
@pytest.mark.asyncio
async def test_renaming_a_root_by_path_is_refused_before_any_http_call(root):
    transport, mcp_ctx = _make_ctx(_universal_handler)

    result = await _call(
        devops_update_query, UpdateQueryInput(query=root, name="whatever"), mcp_ctx
    )

    assert result["error"] is True
    assert "ROOT folder" in result["message"]
    assert "breaks PATH addressing" in result["message"]
    assert transport.requests == [], "the PATCH must never be attempted"


@pytest.mark.asyncio
async def test_renaming_a_root_by_guid_is_refused_after_one_read():
    """A GUID says nothing about what it points at — hence the pre-read."""
    transport, mcp_ctx = _make_ctx(
        lambda request: _json_response(200, _root_folder_item(), request)
    )

    result = await _call(
        devops_update_query, UpdateQueryInput(query=ROOT_GUID, name="whatever"), mcp_ctx
    )

    assert result["error"] is True
    assert "ROOT folder" in result["message"]
    assert [r.method for r in transport.requests] == ["GET"]


@pytest.mark.asyncio
async def test_an_already_renamed_root_is_still_recognised_as_a_root():
    """The damaged case: name and path no longer look like a root, but it is one."""
    transport, mcp_ctx = _make_ctx(
        lambda request: _json_response(200, _root_folder_item("whatever"), request)
    )

    result = await _call(
        devops_update_query, UpdateQueryInput(query=ROOT_GUID, name="something else"), mcp_ctx
    )

    assert result["error"] is True
    assert "ROOT folder 'whatever'" in result["message"]
    assert [r.method for r in transport.requests] == ["GET"], "no second rename may go out"


def _damaged_root_handler(
    repair_name: str, *, other_root: str, roots_status: int = 200
) -> Callable[[httpx.Request], httpx.Response]:
    """A hierarchy mid-damage: ROOT_GUID renamed to 'whatever', one root intact.

    The bare ``/wit/queries`` GET is the roots listing the repair guard reads to
    decide whether the name being restored is still occupied.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = _raw_path(request)
        if request.method == "GET" and path.endswith("/wit/queries"):
            if roots_status != 200:
                return _json_response(roots_status, {"message": "boom"}, request)
            return _json_response(
                200,
                {"count": 2, "value": [_root_folder_item("whatever"), _folder_item(other_root)]},
                request,
            )
        if request.method == "GET":
            return _json_response(200, _root_folder_item("whatever"), request)
        return _json_response(200, _root_folder_item(repair_name), request)

    return handler


@pytest.mark.parametrize(
    "repair_name, other_root",
    [
        ("Shared Queries", "My Queries"),
        ("My Queries", "Shared Queries"),
        ("shared queries", "My Queries"),
    ],
)
@pytest.mark.asyncio
async def test_repairing_a_damaged_root_to_a_vacant_root_name_is_allowed(repair_name, other_root):
    """The escape hatch: the guard must not make the damage unrepairable.

    A caller left holding only the GUID of a renamed root has to be able to set
    the name back without editing this module. Two reads pay for it: what the
    GUID points at, then the roots listing that proves the target name is vacant.
    """
    transport, mcp_ctx = _make_ctx(_damaged_root_handler(repair_name, other_root=other_root))

    result = await _call(
        devops_update_query, UpdateQueryInput(query=ROOT_GUID, name=repair_name), mcp_ctx
    )

    assert "error" not in result, result
    assert [r.method for r in transport.requests] == ["GET", "GET", "PATCH"]
    assert json.loads(transport.requests[2].content) == {"name": repair_name}
    assert result["renamed"] is True


@pytest.mark.parametrize(
    "root, other_root",
    [("Shared Queries", "My Queries"), ("My Queries", "Shared Queries")],
)
@pytest.mark.asyncio
async def test_renaming_a_healthy_root_onto_the_other_root_by_path_is_refused(root, other_root):
    """THE swap bypass: 'is the new name a root name?' is not a guard.

    Answering only that question permits renaming an INTACT root onto the other
    root's name — two roots on one path, and unlike the plain rename there is no
    single clean owner of that path to rename back. The guard has to compare the
    new name against the item's OWN identity.
    """
    transport, mcp_ctx = _make_ctx(_universal_handler)

    result = await _call(
        devops_update_query, UpdateQueryInput(query=root, name=other_root), mcp_ctx
    )

    assert result["error"] is True
    assert "ROOT folder" in result["message"]
    assert transport.requests == [], "the swap must never reach the wire"


@pytest.mark.parametrize(
    "root, other_root",
    [("Shared Queries", "My Queries"), ("My Queries", "Shared Queries")],
)
@pytest.mark.asyncio
async def test_renaming_a_healthy_root_onto_the_other_root_by_guid_is_refused(root, other_root):
    """Same swap, addressed by GUID — the branch that has to read before it leaps."""
    transport, mcp_ctx = _make_ctx(
        lambda request: _json_response(200, _root_folder_item(root), request)
    )

    result = await _call(
        devops_update_query, UpdateQueryInput(query=ROOT_GUID, name=other_root), mcp_ctx
    )

    assert result["error"] is True
    assert f"ROOT folder '{root}'" in result["message"]
    assert [r.method for r in transport.requests] == ["GET"], "no PATCH, no roots probe"


@pytest.mark.parametrize("root", ["Shared Queries", "My Queries"])
@pytest.mark.asyncio
async def test_renaming_a_healthy_root_to_its_own_name_is_refused_by_path(root):
    """A no-op rename of an intact root is still refused: it is not a repair.

    Allowing it would mean the guard accepts a root rename whenever the target
    happens to be a root name, which is exactly the hole the swap goes through.
    """
    transport, mcp_ctx = _make_ctx(_universal_handler)

    result = await _call(devops_update_query, UpdateQueryInput(query=root, name=root), mcp_ctx)

    assert result["error"] is True
    assert "needs no repair" in result["message"]
    assert transport.requests == []


@pytest.mark.asyncio
async def test_renaming_a_healthy_root_to_its_own_name_by_guid_is_refused():
    transport, mcp_ctx = _make_ctx(
        lambda request: _json_response(200, _root_folder_item("Shared Queries"), request)
    )

    result = await _call(
        devops_update_query, UpdateQueryInput(query=ROOT_GUID, name="Shared Queries"), mcp_ctx
    )

    assert result["error"] is True
    assert "needs no repair" in result["message"]
    assert [r.method for r in transport.requests] == ["GET"]


@pytest.mark.asyncio
async def test_repairing_a_damaged_root_onto_the_live_roots_name_is_refused():
    """The collision case: the target root name is still held by the other root.

    Restoring the damaged root to a name that is in use would put two roots on
    one path — worse than the damage, and with no clean way back.
    """
    transport, mcp_ctx = _make_ctx(
        _damaged_root_handler("Shared Queries", other_root="Shared Queries")
    )

    result = await _call(
        devops_update_query, UpdateQueryInput(query=ROOT_GUID, name="Shared Queries"), mcp_ctx
    )

    assert result["error"] is True
    assert "already in use by the other root" in result["message"]
    assert [r.method for r in transport.requests] == ["GET", "GET"], "no PATCH"


@pytest.mark.asyncio
async def test_a_root_repair_fails_closed_when_the_roots_cannot_be_listed():
    """Unverifiable is refused, not assumed free — 500 is not retried on this route."""
    transport, mcp_ctx = _make_ctx(
        _damaged_root_handler("Shared Queries", other_root="My Queries", roots_status=500)
    )

    result = await _call(
        devops_update_query, UpdateQueryInput(query=ROOT_GUID, name="Shared Queries"), mcp_ctx
    )

    assert result["error"] is True
    assert "could not be listed" in result["message"]
    assert not [r for r in transport.requests if r.method == "PATCH"]


@pytest.mark.asyncio
async def test_a_non_object_200_body_fails_the_rename_instead_of_waving_it_through():
    """A required pre-read must fail CLOSED on a body that is not a query item.

    A 200 carrying [] or null would otherwise reach the guard as {}, which is
    'not a root', and the rename would go out unguarded.
    """
    transport, mcp_ctx = _make_ctx(lambda request: _json_response(200, [], request))

    result = await _call(
        devops_update_query, UpdateQueryInput(query=ROOT_GUID, name="whatever"), mcp_ctx
    )

    assert result["error"] is True
    assert "instead of a query item" in result["message"]
    assert [r.method for r in transport.requests] == ["GET"], "no blind PATCH"


@pytest.mark.asyncio
async def test_moving_a_root_is_refused_even_to_a_valid_folder():
    """A root has no parent; re-parenting one strands everything beneath it."""
    transport, mcp_ctx = _make_ctx(
        lambda request: _json_response(200, _root_folder_item(), request)
    )

    result = await _call(
        devops_update_query,
        UpdateQueryInput(query=ROOT_GUID, move_to="Shared Queries/Archive"),
        mcp_ctx,
    )

    assert result["error"] is True
    assert "cannot be moved" in result["message"]
    assert not [r for r in transport.requests if r.method == "POST"]


@pytest.mark.asyncio
async def test_moving_a_root_by_path_is_refused_before_any_http_call():
    transport, mcp_ctx = _make_ctx(_universal_handler)

    result = await _call(
        devops_update_query,
        UpdateQueryInput(query="My Queries", move_to="Shared Queries/Archive"),
        mcp_ctx,
    )

    assert result["error"] is True
    assert "cannot be moved" in result["message"]
    assert transport.requests == []


@pytest.mark.asyncio
async def test_a_non_root_folder_rename_is_untouched_by_the_guard():
    """The guard must not over-reach: an ordinary folder renames normally."""
    folder = {
        "id": DEST_GUID,
        "name": "Team A",
        "path": "Shared Queries/Team A",
        "isFolder": True,
    }
    transport, mcp_ctx = _make_ctx(lambda request: _json_response(200, folder, request))

    result = await _call(
        devops_update_query, UpdateQueryInput(query=DEST_GUID, name="Team B"), mcp_ctx
    )

    assert "error" not in result, result
    assert [r.method for r in transport.requests] == ["GET", "PATCH"]
    assert json.loads(transport.requests[1].content) == {"name": "Team B"}


@pytest.mark.asyncio
async def test_an_unreadable_item_fails_the_rename_instead_of_renaming_blind():
    """The root pre-read is NOT best-effort — unlike the undelete one.

    If the item cannot be read, whether it is a root is unknown, and the same
    error would have met the PATCH anyway. Guessing here is what bricks a root.
    """
    transport, mcp_ctx = _make_ctx(
        _error_handler(404, {"message": "TF401243: QueryItemNotFoundException"})
    )

    result = await _call(
        devops_update_query, UpdateQueryInput(query=QUERY_GUID, name="R"), mcp_ctx
    )

    assert result["error"] is True
    assert "was not found" in result["message"]
    assert [r.method for r in transport.requests] == ["GET"], "no blind PATCH"


# ---------------------------------------------------------------------------
# Running a query — result-shape normalisation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_flat_query_by_guid_makes_no_resolve_call():
    transport, mcp_ctx = _make_ctx(_universal_handler)

    result = await _call(devops_run_query, RunQueryInput(query=QUERY_GUID), mcp_ctx)

    assert "error" not in result, result
    paths = [_raw_path(r) for r in transport.requests]
    assert len(paths) == 2, paths  # run + one hydration batch
    assert paths[0].endswith(f"/_apis/wit/wiql/{QUERY_GUID}")
    assert result["query_type"] == "flat"
    assert result["result_type"] == "workItem"
    assert result["count"] == 2
    assert [w["id"] for w in result["work_items"]] == [1, 2]
    assert result["possibly_truncated"] is False
    assert "relations" not in result


@pytest.mark.asyncio
async def test_run_query_by_path_resolves_the_id_first():
    transport, mcp_ctx = _make_ctx(_universal_handler)

    result = await _call(devops_run_query, RunQueryInput(query=QUERY_PATH), mcp_ctx)

    assert "error" not in result, result
    paths = [_raw_path(r) for r in transport.requests]
    assert paths[0].endswith("/_apis/wit/queries/Shared%20Queries/Team%20A/My%20Query")
    assert paths[1].endswith(f"/_apis/wit/wiql/{QUERY_GUID}")
    assert result["query_id"] == QUERY_GUID
    assert result["query_path"] == QUERY_PATH


@pytest.mark.asyncio
async def test_run_tree_query_reads_relations_not_work_items():
    """tree/oneHop omit 'workItems' entirely — .get('workItems', []) reports zero.

    This is the silent-wrong-answer regression test: the fixture has NO
    'workItems' key and repeats ids across relations.
    """

    def handler(request):
        path = _raw_path(request)
        if "/wit/wiql/" in path:
            return _json_response(200, _tree_run_result(), request)
        if "/wit/workitemsbatch" in path:
            ids = _batch_body(request)["ids"]
            return _json_response(
                200, {"count": len(ids), "value": [{"id": i, "fields": {}} for i in ids]}, request
            )
        raise AssertionError(f"Unexpected request: {request.method} {path}")

    transport, mcp_ctx = _make_ctx(handler)

    result = await _call(devops_run_query, RunQueryInput(query=QUERY_GUID), mcp_ctx)

    assert "error" not in result, result
    assert result["result_type"] == "workItemLink"
    assert result["count"] == 3, "ids repeat once per link and must be de-duplicated"
    assert [w["id"] for w in result["work_items"]] == [1, 2, 3]
    assert result["work_items"], "a tree query must not report an empty work item set"

    # Ids are de-duplicated BEFORE hydration so the 200-per-call budget is not
    # burned on repeats.
    batch_requests = [r for r in transport.requests if "/wit/workitemsbatch" in _raw_path(r)]
    assert len(batch_requests) == 1
    assert _batch_body(batch_requests[0])["ids"] == [1, 2, 3]

    relations = result["relations"]
    assert len(relations) == 4
    assert relations[0] == {"target_id": 1}, "a root row has neither rel nor source"
    assert relations[1] == {
        "rel": "System.LinkTypes.Hierarchy-Forward",
        "source_id": 1,
        "target_id": 2,
    }


@pytest.mark.asyncio
async def test_run_onehop_query_projects_real_link_rows():
    """'tree' is not the only relation-shaped type — MODE (MustContain) is 'oneHop'.

    Live coverage for this branch is absent (the sandbox project had no work item
    links, so only root rows with rel/source null were ever exercised), which is
    exactly why the link row is asserted here field by field.
    """

    def handler(request):
        path = _raw_path(request)
        if "/wit/wiql/" in path:
            return _json_response(
                200,
                {
                    "queryType": "oneHop",
                    "queryResultType": "workItemLink",
                    "asOf": "2026-08-13T10:00:00Z",
                    "columns": [{"referenceName": "System.Id"}],
                    "workItemRelations": [
                        {"rel": None, "source": None,
                         "target": {"id": 10, "url": "https://example.invalid/wi/10"}},
                        {"rel": "System.LinkTypes.Hierarchy-Forward",
                         "source": {"id": 10, "url": "https://example.invalid/wi/10"},
                         "target": {"id": 11, "url": "https://example.invalid/wi/11"}},
                    ],
                },
                request,
            )
        if "/wit/workitemsbatch" in path:
            ids = _batch_body(request)["ids"]
            return _json_response(
                200, {"count": len(ids), "value": [{"id": i, "fields": {}} for i in ids]}, request
            )
        raise AssertionError(f"Unexpected request: {request.method} {path}")

    _transport, mcp_ctx = _make_ctx(handler)

    result = await _call(devops_run_query, RunQueryInput(query=QUERY_GUID), mcp_ctx)

    assert result["query_type"] == "oneHop"
    assert result["result_type"] == "workItemLink"
    assert result["relations"] == [
        {"target_id": 10},
        {"rel": "System.LinkTypes.Hierarchy-Forward", "source_id": 10, "target_id": 11},
    ]
    assert [w["id"] for w in result["work_items"]] == [10, 11]


@pytest.mark.asyncio
async def test_run_query_team_segment():
    """The team goes between project and _apis; without it no '//' sneaks in."""
    transport, mcp_ctx = _make_ctx(_universal_handler)

    await _call(
        devops_run_query, RunQueryInput(query=QUERY_GUID, team="Web Team", fetch_details=False), mcp_ctx
    )
    with_team = _raw_path(transport.requests[0])

    transport2, mcp_ctx2 = _make_ctx(_universal_handler)
    await _call(devops_run_query, RunQueryInput(query=QUERY_GUID, fetch_details=False), mcp_ctx2)
    without_team = _raw_path(transport2.requests[0])

    assert with_team == f"/testorg/Test%20Project/Web%20Team/_apis/wit/wiql/{QUERY_GUID}"
    assert without_team == f"/testorg/Test%20Project/_apis/wit/wiql/{QUERY_GUID}"
    assert "//_apis" not in without_team


@pytest.mark.asyncio
async def test_run_query_hydration_chunks_at_200():
    """450 unique ids => 3 workitemsbatch POSTs at the 200-id service cap."""
    ids = list(range(1, 451))

    def handler(request):
        path = _raw_path(request)
        if "/wit/wiql/" in path:
            return _json_response(200, _flat_run_result(ids), request)
        if "/wit/workitemsbatch" in path:
            batch = _batch_body(request)["ids"]
            return _json_response(
                200, {"count": len(batch), "value": [{"id": i, "fields": {}} for i in batch]}, request
            )
        raise AssertionError(f"Unexpected request: {request.method} {path}")

    transport, mcp_ctx = _make_ctx(handler)

    result = await _call(devops_run_query, RunQueryInput(query=QUERY_GUID), mcp_ctx)

    batches = [_batch_body(r)["ids"] for r in transport.requests if "/wit/workitemsbatch" in _raw_path(r)]
    assert [len(b) for b in batches] == [200, 200, 50]
    assert batches[0][0] == 1 and batches[0][-1] == 200
    assert batches[1][0] == 201 and batches[2][-1] == 450
    assert result["count"] == 450
    assert len(result["work_items"]) == 450


@pytest.mark.asyncio
async def test_run_query_uses_the_queries_own_columns_by_default():
    """The query author already declared which fields matter."""
    transport, mcp_ctx = _make_ctx(_universal_handler)

    await _call(devops_run_query, RunQueryInput(query=QUERY_GUID), mcp_ctx)

    batch = _batch_body(transport.requests[1])
    assert batch["fields"] == ["System.Id", "System.Title"]
    assert batch["errorPolicy"] == "omit"


@pytest.mark.asyncio
async def test_run_query_explicit_fields_override_columns():
    transport, mcp_ctx = _make_ctx(_universal_handler)

    await _call(
        devops_run_query, RunQueryInput(query=QUERY_GUID, fields=["System.State"]), mcp_ctx
    )

    assert _batch_body(transport.requests[1])["fields"] == ["System.State"]


@pytest.mark.asyncio
async def test_run_query_without_details_returns_ids_only():
    transport, mcp_ctx = _make_ctx(_universal_handler)

    result = await _call(
        devops_run_query, RunQueryInput(query=QUERY_GUID, fetch_details=False), mcp_ctx
    )

    assert result["work_item_ids"] == [1, 2]
    assert "work_items" not in result
    assert not [r for r in transport.requests if "/wit/workitemsbatch" in _raw_path(r)]


@pytest.mark.asyncio
async def test_run_query_at_the_20000_cap_is_flagged_as_possibly_truncated():
    """The limits page says results are truncated silently — flag it, don't hide it."""
    ids = list(range(1, 20001))

    def handler(request):
        return _json_response(200, _flat_run_result(ids), request)

    _transport, mcp_ctx = _make_ctx(handler)

    result = await _call(
        devops_run_query, RunQueryInput(query=QUERY_GUID, fetch_details=False), mcp_ctx
    )

    assert result["count"] == 20000
    assert result["possibly_truncated"] is True


@pytest.mark.asyncio
async def test_run_query_with_no_results():
    def handler(request):
        return _json_response(200, _flat_run_result([]), request)

    transport, mcp_ctx = _make_ctx(handler)

    result = await _call(devops_run_query, RunQueryInput(query=QUERY_GUID), mcp_ctx)

    assert result["count"] == 0
    assert result["work_items"] == []
    assert len(transport.requests) == 1, "no hydration call for an empty result"


# ---------------------------------------------------------------------------
# Delete — soft, and 204-with-no-body
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_accepts_bare_204():
    """The samples answer 204 with no body; a missing body is not a failure."""
    transport, mcp_ctx = _make_ctx(lambda request: httpx.Response(204, request=request))

    result = await _call(devops_delete_query, DeleteQueryInput(query=QUERY_PATH), mcp_ctx)

    assert result["deleted"] is True
    assert result["recoverable"] is True
    assert "undelete=True" in result["note"]
    request = transport.requests[0]
    assert request.method == "DELETE"
    assert _raw_path(request).endswith("/_apis/wit/queries/Shared%20Queries/Team%20A/My%20Query")
    assert _params(request) == {"api-version": QUERIES_API_VERSION}, (
        "delete has no query parameters at all — there is no destroy flag"
    )


@pytest.mark.asyncio
async def test_delete_accepts_200_with_body():
    """The Responses table documents a 200 with a body — also a success."""
    _transport, mcp_ctx = _make_ctx(
        lambda request: _json_response(200, _query_item(isDeleted=True), request)
    )

    result = await _call(devops_delete_query, DeleteQueryInput(query=QUERY_GUID), mcp_ctx)

    assert result["deleted"] is True
    assert result["query_id"] == QUERY_GUID
    assert result["path"] == QUERY_PATH


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hierarchy_projection_nests_children_and_drops_boilerplate():
    def handler(request):
        return _json_response(
            200,
            {
                "count": 2,
                "value": [
                    _folder_item("My Queries"),
                    _folder_item("Shared Queries", children=[_query_item()]),
                ],
            },
            request,
        )

    _transport, mcp_ctx = _make_ctx(handler)

    result = await _call(devops_list_queries, ListQueriesInput(), mcp_ctx)

    assert result["count"] == 2
    roots = {q["name"] for q in result["queries"]}
    assert roots == {"My Queries", "Shared Queries"}
    shared = next(q for q in result["queries"] if q["name"] == "Shared Queries")
    child = shared["children"][0]
    assert child["id"] == QUERY_GUID
    assert child["path"] == QUERY_PATH
    assert child["query_type"] == "flat"
    assert child["created_by"] == "Ada Lovelace"
    assert child["columns"] == ["System.Id", "System.Title"]
    assert child["sort_columns"] == [{"field": "System.Id", "descending": False}]
    assert child["html_url"].startswith("https://dev.azure.com/")
    # Identity blobs and self/parent links are noise and must not be echoed.
    assert "url" not in child and "_links" not in child and "createdBy" not in child


@pytest.mark.asyncio
async def test_get_query_flags_invalid_syntax():
    """A saved-but-broken query reads fine and fails only on run — warn on read."""
    _transport, mcp_ctx = _make_ctx(
        lambda request: _json_response(200, _query_item(isInvalidSyntax=True), request)
    )

    result = await _call(devops_get_query, GetQueryInput(query=QUERY_GUID), mcp_ctx)

    assert result["is_invalid_syntax"] is True
    assert "invalid" in result["warning"]


@pytest.mark.asyncio
async def test_expand_clauses_passes_the_clause_tree_through():
    _transport, mcp_ctx = _make_ctx(
        lambda request: _json_response(
            200, _query_item(clauses={"field": {"referenceName": "System.State"}}), request
        )
    )

    result = await _call(
        devops_get_query, GetQueryInput(query=QUERY_GUID, expand="clauses"), mcp_ctx
    )

    assert result["clauses"] == {"field": {"referenceName": "System.State"}}


@pytest.mark.asyncio
async def test_clauses_are_dropped_unless_requested():
    _transport, mcp_ctx = _make_ctx(
        lambda request: _json_response(200, _query_item(clauses={"field": {}}), request)
    )

    result = await _call(devops_get_query, GetQueryInput(query=QUERY_GUID), mcp_ctx)

    assert "clauses" not in result


# The $expand key sets, transcribed from raw live GETs of one query (round 3 —
# an earlier round had these wrong in BOTH directions):
#   none    -> _links, createdBy, createdDate, id, isPublic, lastModifiedBy,
#              lastModifiedDate, name, path, queryType, url
#   minimal -> id, isPublic, name, path, queryType, wiql
#   wiql    -> none's set + columns + wiql
#   all     -> wiql's set + clauses
# So: columns is NOT returned at 'none' (only at 'wiql'/'all'), and 'minimal'
# does NOT drop queryType (present at every level). The two fixtures below are
# the live key sets exactly — do not add keys the service does not send.
_EXPAND_NONE_ITEM = {
    "id": QUERY_GUID,
    "name": "My Query",
    "path": QUERY_PATH,
    "isFolder": False,
    "isPublic": True,
    "queryType": "flat",
    "createdBy": {"displayName": "Ada Lovelace"},
    "createdDate": "2026-08-01T09:00:00Z",
    "lastModifiedBy": {"displayName": "Ada Lovelace"},
    "lastModifiedDate": "2026-08-13T10:00:00Z",
    "_links": {"html": {"href": "https://dev.azure.com/testorg/_queries/query/8a8c8212"}},
    "url": "https://example.invalid/url",
}

_EXPAND_MINIMAL_ITEM = {
    "id": QUERY_GUID,
    "name": "My Query",
    "path": QUERY_PATH,
    "isFolder": False,
    "isPublic": True,
    "queryType": "flat",
    "wiql": "SELECT [System.Id] FROM WorkItems",
}

_EXPAND_WIQL_ITEM = {
    **_EXPAND_NONE_ITEM,
    "wiql": "SELECT [System.Id] FROM WorkItems",
    "columns": [{"referenceName": "System.Id"}, {"referenceName": "System.Title"}],
}


@pytest.mark.asyncio
async def test_expand_none_returns_metadata_but_neither_wiql_nor_columns():
    """'none' carries the dates and the author — and no columns, despite the name."""
    _transport, mcp_ctx = _make_ctx(lambda r: _json_response(200, _EXPAND_NONE_ITEM, r))

    result = await _call(
        devops_get_query, GetQueryInput(query=QUERY_GUID, expand="none"), mcp_ctx
    )

    assert result["query_type"] == "flat"
    assert result["created_by"] == "Ada Lovelace"
    assert result["created_date"] and result["last_modified_date"]
    assert "wiql" not in result, "absent keys stay absent — never a row of nulls"
    assert "columns" not in result, (
        "columns arrive only at expand='wiql'/'all' — promising them at 'none' sends "
        "a caller looking for a key that never comes"
    )


@pytest.mark.asyncio
async def test_expand_minimal_returns_wiql_and_query_type_but_drops_the_metadata():
    """'minimal' is not a subset of 'none' — it is a different projection.

    It keeps queryType (which is present at every level) and loses the author,
    the dates and the links.
    """
    _transport, mcp_ctx = _make_ctx(lambda r: _json_response(200, _EXPAND_MINIMAL_ITEM, r))

    result = await _call(
        devops_get_query, GetQueryInput(query=QUERY_GUID, expand="minimal"), mcp_ctx
    )

    assert result["wiql"] == "SELECT [System.Id] FROM WorkItems"
    assert result["query_type"] == "flat"
    for absent in ("columns", "created_by", "created_date", "last_modified_date", "html_url"):
        assert absent not in result, f"{absent} must not be invented when the service omits it"


@pytest.mark.asyncio
async def test_expand_wiql_is_the_level_that_returns_both_text_and_columns():
    """'wiql' is a strict superset of 'none' — it is the level to ask for."""
    _transport, mcp_ctx = _make_ctx(lambda r: _json_response(200, _EXPAND_WIQL_ITEM, r))

    result = await _call(
        devops_get_query, GetQueryInput(query=QUERY_GUID, expand="wiql"), mcp_ctx
    )

    assert result["wiql"] == "SELECT [System.Id] FROM WorkItems"
    assert result["columns"] == ["System.Id", "System.Title"]
    assert result["query_type"] == "flat"
    assert result["created_by"] == "Ada Lovelace"


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


def _error_handler(status: int, body: dict):
    return lambda request: _json_response(status, body, request)


@pytest.mark.asyncio
async def test_404_names_the_path_the_project_and_the_soft_delete_escape_hatch():
    """The recovery advice must be the one that works: by GUID, via the parent.

    Verified live: a soft-deleted item addressed BY PATH answers 404
    (TF401243 / QueryItemNotFoundException) even with $includeDeleted=true on the
    wire, while the same item by GUID answers 200 with isDeleted: true. Telling
    the caller to "retry with include_deleted=true" on the path they just used is
    dead-end advice.
    """
    _transport, mcp_ctx = _make_ctx(
        _error_handler(
            404,
            {"message": "TF401243: QueryItemNotFoundException: The specified query does not exist."},
        )
    )

    result = await _call(devops_get_query, GetQueryInput(query=QUERY_PATH), mcp_ctx)

    assert result["error"] is True
    assert QUERY_PATH in result["message"]
    assert FAKE_PROJECT in result["message"]
    assert "include_deleted=true" in result["message"]
    assert "only addressable by its GUID" in result["message"]
    assert "PARENT folder" in result["message"], (
        "listing the parent is the only way to learn a deleted item's GUID"
    )


@pytest.mark.asyncio
async def test_a_bad_team_is_reported_as_input_error_not_a_server_fault():
    """An unknown team answers HTTP 500 TeamNotFoundException — verified live."""

    def handler(request):
        if "/wit/wiql/" in _raw_path(request):
            return _json_response(
                500,
                {"message": "TF400913: TeamNotFoundException: The team 'Nope' does not exist."},
                request,
            )
        raise AssertionError("unexpected request")

    _transport, mcp_ctx = _make_ctx(handler)

    result = await _call(
        devops_run_query, RunQueryInput(query=QUERY_GUID, team="Nope"), mcp_ctx
    )

    assert result["error"] is True
    assert "Azure DevOps returned HTTP 500" not in result["message"], (
        "a caller input error must not be surfaced as a raw server fault"
    )
    assert "'Nope'" in result["message"]
    assert "devops_list_teams" in result["message"]


@pytest.mark.parametrize("status", [400, 409])
@pytest.mark.asyncio
async def test_duplicate_name_is_matched_on_the_code_not_the_status(status):
    """TF237018 arrives as HTTP 400, not 409 — so the mapping keys off the code."""
    _transport, mcp_ctx = _make_ctx(
        _error_handler(
            status,
            {"message": "TF237018: A query or a query folder on the server has the same name."},
        )
    )

    result = await _call(
        devops_create_query,
        CreateQueryInput(name="Q", wiql="SELECT [System.Id] FROM WorkItems"),
        mcp_ctx,
    )

    assert result["error"] is True
    assert "already exists" in result["message"]


@pytest.mark.asyncio
async def test_a_409_without_the_code_gets_no_name_collision_story():
    """Nothing here may treat 409 as the duplicate-name status."""
    _transport, mcp_ctx = _make_ctx(_error_handler(409, {"message": "conflict"}))

    result = await _call(
        devops_create_query,
        CreateQueryInput(name="Q", wiql="SELECT [System.Id] FROM WorkItems"),
        mcp_ctx,
    )

    assert "already exists" not in result["message"]
    assert "Azure DevOps returned HTTP 409" in result["message"]


@pytest.mark.asyncio
async def test_create_name_collision_is_explained():
    _transport, mcp_ctx = _make_ctx(
        _error_handler(
            400,
            {
                "message": "TF237018: A query or a query folder on the server has the "
                           "same name as an item you tried to save."
            },
        )
    )

    result = await _call(
        devops_create_query,
        CreateQueryInput(parent="Shared Queries/Team A", name="All Bugs",
                         wiql="SELECT [System.Id] FROM WorkItems"),
        mcp_ctx,
    )

    assert result["error"] is True
    assert "already exists" in result["message"]
    assert "All Bugs" in result["message"] and "Shared Queries/Team A" in result["message"]


@pytest.mark.asyncio
async def test_root_folder_delete_refusal_is_matched_on_the_code():
    """TF237023 — verified live: the service refuses a root delete at HTTP 400.

    No client-side guard is needed for delete (unlike rename, which the service
    permits), but the refusal has to be explained rather than passed through.
    """
    _transport, mcp_ctx = _make_ctx(
        _error_handler(
            400,
            {
                "message": "TF237023: You cannot delete root folders.",
                "typeName": "Microsoft.TeamFoundation.WorkItemTracking.Server."
                            "LegacyQueryItemException, Microsoft.TeamFoundation."
                            "WorkItemTracking.Server",
                "typeKey": "LegacyQueryItemException",
                "errorCode": 600298,
                "eventId": 3200,
            },
        )
    )

    result = await _call(
        devops_delete_query, DeleteQueryInput(query="Shared Queries"), mcp_ctx
    )

    assert result["error"] is True
    assert "ROOT" in result["message"]
    assert "Nothing was deleted" in result["message"]
    assert "devops_list_queries" in result["message"], (
        "the caller needs the way forward: delete the children individually"
    )
    assert "Azure DevOps returned HTTP 400" not in result["message"], (
        "a known refusal must not fall through to the generic passthrough"
    )


@pytest.mark.asyncio
async def test_another_legacy_query_exception_gets_no_root_delete_story():
    """TF237023 shares its typeKey (and 400) with other legacy-query errors.

    Matching on the status or the exception type would tell a caller their
    perfectly ordinary failure was a root-folder refusal.
    """
    _transport, mcp_ctx = _make_ctx(
        _error_handler(
            400,
            {
                "message": "TF237018: A query or a query folder on the server has the "
                           "same name as an item you tried to save.",
                "typeKey": "LegacyQueryItemException",
            },
        )
    )

    result = await _call(devops_delete_query, DeleteQueryInput(query=QUERY_PATH), mcp_ctx)

    assert result["error"] is True
    assert "ROOT" not in result["message"]
    assert "root folder" not in result["message"].lower()
    assert "already exists" in result["message"], "still its own TF237018 story"


@pytest.mark.asyncio
async def test_403_explains_the_required_permissions():
    _transport, mcp_ctx = _make_ctx(
        _error_handler(403, {"message": "TF401019: Access denied."})
    )

    result = await _call(
        devops_create_query,
        CreateQueryInput(name="Q", wiql="SELECT [System.Id] FROM WorkItems"),
        mcp_ctx,
    )

    assert result["error"] is True
    assert "Contribute" in result["message"]


@pytest.mark.asyncio
async def test_result_limit_error_says_narrow_the_query():
    def handler(request):
        if "/wit/wiql/" in _raw_path(request):
            return _json_response(
                400,
                {"message": "VS402337: The number of work items returned exceeds the "
                            "size limit of 20000."},
                request,
            )
        raise AssertionError("unexpected request")

    _transport, mcp_ctx = _make_ctx(handler)

    result = await _call(devops_run_query, RunQueryInput(query=QUERY_GUID), mcp_ctx)

    assert result["error"] is True
    assert "Narrow the query" in result["message"]
    assert "20,000" in result["message"]


@pytest.mark.asyncio
async def test_unmapped_status_falls_back_to_the_standard_message():
    _transport, mcp_ctx = _make_ctx(_error_handler(500, {"message": "boom"}))

    result = await _call(devops_get_query, GetQueryInput(query=QUERY_GUID), mcp_ctx)

    assert result["error"] is True
    assert "Azure DevOps returned HTTP 500" in result["message"]


@pytest.mark.asyncio
async def test_missing_organization_is_reported_as_a_value_error():
    transport, mcp_ctx = _make_ctx(_universal_handler)

    with patch(
        "devops_mcp.tools.queries.resolve_org",
        side_effect=ValueError("No Azure DevOps organization provided."),
    ):
        result = json.loads(await devops_list_queries(ListQueriesInput(), mcp_ctx))

    assert result["error"] is True
    assert "organization" in result["message"]
    assert transport.requests == []


@pytest.mark.asyncio
async def test_run_query_on_a_folder_path_is_rejected_client_side():
    """A folder HAS an id, so 'did it return an id?' never catches one.

    Without an isFolder guard the run falls through to the wiql route and the
    caller gets a generic '400 QueryException: Querying folders is not
    supported' after a wasted round trip.
    """
    transport, mcp_ctx = _make_ctx(
        lambda request: _json_response(
            200,
            {"id": DEST_GUID, "name": "Team A", "path": "Shared Queries/Team A", "isFolder": True},
            request,
        )
    )

    result = await _call(devops_run_query, RunQueryInput(query="Shared Queries/Team A"), mcp_ctx)

    assert result["error"] is True
    assert "FOLDER" in result["message"]
    assert [r.method for r in transport.requests] == ["GET"], (
        "the folder guard must fire on the resolve hop, before any wiql call"
    )
    assert not [r for r in transport.requests if "/wit/wiql/" in _raw_path(r)]


@pytest.mark.asyncio
async def test_running_a_folder_by_guid_is_explained_from_the_400():
    """A GUID run costs no resolve hop, so the folder can only be named by the error."""
    _transport, mcp_ctx = _make_ctx(
        _error_handler(400, {"message": "VS402337x QueryException: Querying folders is not supported."})
    )

    result = await _call(devops_run_query, RunQueryInput(query=QUERY_GUID), mcp_ctx)

    assert result["error"] is True
    assert "FOLDER" in result["message"]
    assert "Azure DevOps returned HTTP 400" not in result["message"]


@pytest.mark.asyncio
async def test_run_query_on_a_path_without_an_id_is_actionable():
    """A resolve hop that yields no id must not fall through into a bad URL."""
    _transport, mcp_ctx = _make_ctx(
        lambda request: _json_response(200, {"name": "Orphan", "isFolder": False}, request)
    )

    result = await _call(devops_run_query, RunQueryInput(query="Shared Queries/Orphan"), mcp_ctx)

    assert result["error"] is True
    assert "no usable id" in result["message"]


@pytest.mark.asyncio
async def test_a_non_guid_id_from_the_server_never_reaches_the_url():
    """query_id is interpolated into the hand-built team URL — GUID-check it first."""
    transport, mcp_ctx = _make_ctx(
        lambda request: _json_response(
            200, {"id": "../../evil", "name": "My Query", "isFolder": False}, request
        )
    )

    result = await _call(
        devops_run_query, RunQueryInput(query=QUERY_PATH, team="Web Team"), mcp_ctx
    )

    assert result["error"] is True
    assert "no usable id" in result["message"]
    assert [r.method for r in transport.requests] == ["GET"]
    assert not [r for r in transport.requests if "/wit/wiql/" in _raw_path(r)]


# ---------------------------------------------------------------------------
# Model validation
# ---------------------------------------------------------------------------


def test_update_query_requires_at_least_one_change():
    with pytest.raises(ValueError, match="Nothing to update"):
        UpdateQueryInput(query=QUERY_GUID)


def test_undelete_descendants_requires_undelete():
    with pytest.raises(ValueError, match="undelete_descendants"):
        UpdateQueryInput(query=QUERY_GUID, name="X", undelete_descendants=True)


@pytest.mark.parametrize("bad_name", ["Shared Queries/Nested", "back\\slash", "   "])
def test_query_name_must_be_a_single_node(bad_name):
    with pytest.raises(ValueError):
        CreateQueryInput(name=bad_name, wiql="SELECT [System.Id] FROM WorkItems")


@pytest.mark.asyncio
async def test_run_query_annotations_are_deliberate():
    """readOnlyHint tools in this repo are idempotentHint too — this one included.

    Running a saved query does advance the query's server-side lastExecutedDate,
    which is why the flag deserved a decision rather than a default; the MCP spec
    treats idempotentHint as meaningful only when readOnlyHint is false.
    """
    from devops_mcp._app import mcp

    tool = next(t for t in await mcp.list_tools() if t.name == "devops_run_query")

    assert tool.annotations.read_only_hint is True
    assert tool.annotations.idempotent_hint is True
    assert tool.annotations.destructive_hint is False


@pytest.mark.parametrize(
    "model, field, fragment",
    [
        # Search matches query names only — a filter naming a folder exactly
        # returns zero results (verified live).
        (ListQueriesInput, "name_filter", "folder names are"),
        # $expand is not monotonic: 'none' and 'minimal' return disjoint extras.
        (ListQueriesInput, "expand", "NOT a simple more-is-more"),
        (GetQueryInput, "expand", "DROPS"),
        # A soft-deleted item is addressable by GUID only.
        (GetQueryInput, "include_deleted", "GUID"),
        (ListQueriesInput, "include_deleted", "GUID"),
        (UpdateQueryInput, "undelete", "GUID"),
        # The descendants cascade only runs on the deleted -> live transition.
        (UpdateQueryInput, "undelete_descendants", "transitions"),
        # An unknown team is an HTTP 500, not a 404.
        (RunQueryInput, "team", "500"),
    ],
)
def test_field_descriptions_carry_the_live_verified_traps(model, field, fragment):
    """These descriptions are the model's only contract — the traps belong in them."""
    description = model.model_fields[field].description or ""

    assert fragment in description, f"{model.__name__}.{field}: {description!r}"


def test_search_top_is_capped_at_200():
    with pytest.raises(ValueError):
        ListQueriesInput(top=201)


def test_empty_name_filter_is_rejected_rather_than_sent_as_an_empty_filter():
    """'' would switch the route to search mode with a meaningless $filter."""
    with pytest.raises(ValueError):
        ListQueriesInput(name_filter="   ")


# ---------------------------------------------------------------------------
# Registration gates (AZDO_ALLOW_WRITE / AZDO_ALLOW_DELETE)
# ---------------------------------------------------------------------------

# The gates are read once, at import time of devops_mcp._app, so they cannot be
# flipped inside a running interpreter without reloading modules and corrupting
# the shared registry other tests rely on. Probe them in a child process.
_PROBE = """
import asyncio, json
import devops_mcp.tools.queries  # noqa: F401 — registration side effect
from devops_mcp._app import mcp

tools = asyncio.run(mcp.list_tools())
print(json.dumps(sorted(t.name for t in tools)))
"""

_WRITE_TOOLS = {"devops_create_query", "devops_create_query_folder", "devops_update_query"}
_DELETE_TOOLS = {"devops_delete_query"}


def _probe_registry(**gates: str | None) -> list[str]:
    env = dict(os.environ)
    for name in ("AZDO_ALLOW_WRITE", "AZDO_ALLOW_DELETE"):
        env.pop(name, None)
    for name, value in gates.items():
        if value is not None:
            env[name] = value
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert proc.returncode == 0, f"Probe failed: {proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_write_and_delete_tools_are_unregistered_when_gates_are_unset():
    names = set(_probe_registry())

    assert _WRITE_TOOLS & names == set()
    assert _DELETE_TOOLS & names == set()
    # Sanity: the module really did import and register its read tools.
    assert {"devops_list_queries", "devops_get_query", "devops_run_query"} <= names


def test_write_tools_are_registered_when_the_write_gate_is_true():
    names = set(_probe_registry(AZDO_ALLOW_WRITE="true"))

    assert _WRITE_TOOLS <= names
    assert _DELETE_TOOLS & names == set()


def test_delete_tool_is_registered_when_the_delete_gate_is_true():
    names = set(_probe_registry(AZDO_ALLOW_DELETE="true"))

    assert _DELETE_TOOLS <= names
    assert _WRITE_TOOLS & names == set()

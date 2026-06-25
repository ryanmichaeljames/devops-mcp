"""Unit tests for client.paginate_results.

Covers:
- 2-page mocked sequence: page 1 returns records + x-ms-continuationtoken,
  page 2 returns records + NO header → all records collected, terminates
  correctly, has_more=False.
- Token is URL-encoded into page 2's continuationToken query param.
- has_more=True when top is reached while a continuation token is still present.
"""

from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from devops_mcp.client import paginate_results

FAKE_URL = "https://dev.azure.com/fake-org/fake-project/_apis/fake"
BASE_PARAMS = {"api-version": "7.1", "$top": "5"}


def _make_response(
    records: list,
    continuation_token: str | None = None,
    record_key: str = "value",
) -> httpx.Response:
    """Build a mock httpx.Response carrying records in an envelope dict."""
    import json as _json

    body = {record_key: records}
    if record_key != "value":
        # also include under "value" so the helper can fall back
        body["value"] = records
    headers = {}
    if continuation_token is not None:
        headers["x-ms-continuationtoken"] = continuation_token

    resp = httpx.Response(
        status_code=200,
        headers=headers,
        content=_json.dumps(body).encode(),
        request=httpx.Request("GET", FAKE_URL),
    )
    return resp


# ---------------------------------------------------------------------------
# Core: 2-page sequence terminates on absent header
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_two_page_sequence_collects_all_records():
    page1_records = [{"id": 1}, {"id": 2}]
    page2_records = [{"id": 3}, {"id": 4}]
    token = "abc+def="  # contains chars that need URL-encoding

    page1 = _make_response(page1_records, continuation_token=token)
    page2 = _make_response(page2_records, continuation_token=None)

    client = AsyncMock(spec=httpx.AsyncClient)
    # request_with_retry internally calls http_client.request; we patch
    # request_with_retry directly so we control what each page returns.
    with patch(
        "devops_mcp.client.request_with_retry",
        new=AsyncMock(side_effect=[page1, page2]),
    ) as mock_rwr:
        records, has_more = await paginate_results(
            client,
            FAKE_URL,
            headers={"Authorization": "Bearer fake"},
            base_params=dict(BASE_PARAMS),
            record_key="value",
            top=100,
        )

    assert records == page1_records + page2_records
    assert has_more is False
    assert mock_rwr.call_count == 2


# ---------------------------------------------------------------------------
# Token URL-encoding: the continuation token must be URL-encoded in page 2
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_continuation_token_is_url_encoded_in_page2():
    raw_token = "abc+def=/xyz"  # + / = must be percent-encoded
    page1_records = [{"id": 1}]
    page2_records = [{"id": 2}]

    page1 = _make_response(page1_records, continuation_token=raw_token)
    page2 = _make_response(page2_records, continuation_token=None)

    captured_calls: list[dict] = []

    async def fake_rwr(client, method, url, *, headers=None, params=None, **kw):
        captured_calls.append({"params": params})
        return [page1, page2][len(captured_calls) - 1]

    with patch("devops_mcp.client.request_with_retry", new=fake_rwr):
        await paginate_results(
            AsyncMock(spec=httpx.AsyncClient),
            FAKE_URL,
            headers={},
            base_params=dict(BASE_PARAMS),
            record_key="value",
            top=100,
        )

    assert len(captured_calls) == 2

    # Page 2 params must carry the URL-encoded token
    page2_params = captured_calls[1]["params"]
    from urllib.parse import quote
    expected_encoded = quote(raw_token, safe="")
    assert page2_params.get("continuationToken") == expected_encoded


# ---------------------------------------------------------------------------
# has_more=True when top reached while token is present
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_has_more_true_when_top_reached():
    page1_records = [{"id": i} for i in range(5)]
    token = "nextpage"

    page1 = _make_response(page1_records, continuation_token=token)

    with patch(
        "devops_mcp.client.request_with_retry",
        new=AsyncMock(return_value=page1),
    ):
        records, has_more = await paginate_results(
            AsyncMock(spec=httpx.AsyncClient),
            FAKE_URL,
            headers={},
            base_params=dict(BASE_PARAMS),
            record_key="value",
            top=5,  # exactly fills on page 1; token still present → has_more
        )

    assert len(records) == 5
    assert has_more is True


# ---------------------------------------------------------------------------
# Single page: no continuation token → has_more=False, terminates
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_single_page_no_token():
    page_records = [{"id": 1}, {"id": 2}]
    page = _make_response(page_records, continuation_token=None)

    with patch(
        "devops_mcp.client.request_with_retry",
        new=AsyncMock(return_value=page),
    ) as mock_rwr:
        records, has_more = await paginate_results(
            AsyncMock(spec=httpx.AsyncClient),
            FAKE_URL,
            headers={},
            base_params=dict(BASE_PARAMS),
            record_key="value",
            top=100,
        )

    assert records == page_records
    assert has_more is False
    assert mock_rwr.call_count == 1


# ---------------------------------------------------------------------------
# initial_continuation_token is passed on the first request
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_initial_continuation_token_used_on_first_request():
    records = [{"id": 99}]
    page = _make_response(records, continuation_token=None)

    captured: list[dict] = []

    async def fake_rwr(client, method, url, *, headers=None, params=None, **kw):
        captured.append({"params": params})
        return page

    with patch("devops_mcp.client.request_with_retry", new=fake_rwr):
        await paginate_results(
            AsyncMock(spec=httpx.AsyncClient),
            FAKE_URL,
            headers={},
            base_params=dict(BASE_PARAMS),
            record_key="value",
            top=100,
            initial_continuation_token="starthere",
        )

    assert len(captured) == 1
    from urllib.parse import quote
    assert captured[0]["params"].get("continuationToken") == quote("starthere", safe="")

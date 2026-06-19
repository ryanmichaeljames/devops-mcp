"""Unit tests for client.request_with_retry.

Covers:
- (a) 429 with Retry-After on a GET → retries after honouring the header.
- (b) 503 on a GET → retries (idempotent method + retryable status).
- (c) 503 on a POST → returns immediately, NO retry (idempotency gate).
- (d) 429 on a POST → retried (throttle is safe for all methods).
- (e) 2xx → returned unchanged, no retry.

asyncio.sleep is patched to avoid real delays; call counts verify behaviour.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from devops_mcp.client import request_with_retry

FAKE_URL = "https://dev.azure.com/fake-org/fake-project/_apis/fake"


def _make_response(status_code: int, headers: dict | None = None) -> httpx.Response:
    """Build a minimal httpx.Response for a given status code."""
    resp = httpx.Response(
        status_code=status_code,
        headers=headers or {},
        content=b"{}",
        request=httpx.Request("GET", FAKE_URL),
    )
    return resp


def _mock_client(responses: list[httpx.Response]) -> httpx.AsyncClient:
    """Return an AsyncClient whose request() yields successive responses."""
    client = AsyncMock(spec=httpx.AsyncClient)
    client.request = AsyncMock(side_effect=responses)
    return client


# ---------------------------------------------------------------------------
# (a) 429 with Retry-After on a GET → retries and honours the header
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_429_get_retries_with_retry_after():
    throttled = _make_response(429, {"retry-after": "5"})
    throttled.request = httpx.Request("GET", FAKE_URL)
    ok = _make_response(200)
    ok.request = httpx.Request("GET", FAKE_URL)

    client = _mock_client([throttled, ok])

    with patch("devops_mcp.client.asyncio.sleep", new=AsyncMock()) as mock_sleep:
        result = await request_with_retry(client, "GET", FAKE_URL, max_attempts=3)

    assert result.status_code == 200
    assert client.request.call_count == 2
    # Retry-After 5 s, but capped at 30 s → expect sleep(5.0)
    mock_sleep.assert_awaited_once_with(5.0)


# ---------------------------------------------------------------------------
# (b) 503 on a GET → retries (idempotent)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_503_get_retries():
    err503 = _make_response(503)
    err503.request = httpx.Request("GET", FAKE_URL)
    ok = _make_response(200)
    ok.request = httpx.Request("GET", FAKE_URL)

    client = _mock_client([err503, ok])

    with patch("devops_mcp.client.asyncio.sleep", new=AsyncMock()):
        result = await request_with_retry(client, "GET", FAKE_URL, max_attempts=3)

    assert result.status_code == 200
    assert client.request.call_count == 2


# ---------------------------------------------------------------------------
# (c) 503 on a POST → returns immediately, NO retry (idempotency gate)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_503_post_no_retry():
    err503 = _make_response(503)
    err503.request = httpx.Request("POST", FAKE_URL)

    client = _mock_client([err503])

    with patch("devops_mcp.client.asyncio.sleep", new=AsyncMock()) as mock_sleep:
        result = await request_with_retry(client, "POST", FAKE_URL, max_attempts=3)

    # Must return immediately — the write may have committed
    assert result.status_code == 503
    assert client.request.call_count == 1
    mock_sleep.assert_not_awaited()


# ---------------------------------------------------------------------------
# (d) 429 on a POST → retried (throttle is safe for all methods)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_429_post_retries():
    throttled = _make_response(429)
    throttled.request = httpx.Request("POST", FAKE_URL)
    ok = _make_response(200)
    ok.request = httpx.Request("POST", FAKE_URL)

    client = _mock_client([throttled, ok])

    with patch("devops_mcp.client.asyncio.sleep", new=AsyncMock()):
        result = await request_with_retry(client, "POST", FAKE_URL, max_attempts=3)

    assert result.status_code == 200
    assert client.request.call_count == 2


# ---------------------------------------------------------------------------
# (e) 2xx → returned unchanged, no retry
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_2xx_no_retry():
    ok = _make_response(200)
    ok.request = httpx.Request("GET", FAKE_URL)

    client = _mock_client([ok])

    with patch("devops_mcp.client.asyncio.sleep", new=AsyncMock()) as mock_sleep:
        result = await request_with_retry(client, "GET", FAKE_URL, max_attempts=3)

    assert result.status_code == 200
    assert client.request.call_count == 1
    mock_sleep.assert_not_awaited()


# ---------------------------------------------------------------------------
# Extra: Retry-After cap at 30 s
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_429_retry_after_capped_at_30s():
    throttled = _make_response(429, {"retry-after": "999"})
    throttled.request = httpx.Request("GET", FAKE_URL)
    ok = _make_response(200)
    ok.request = httpx.Request("GET", FAKE_URL)

    client = _mock_client([throttled, ok])

    with patch("devops_mcp.client.asyncio.sleep", new=AsyncMock()) as mock_sleep:
        await request_with_retry(client, "GET", FAKE_URL, max_attempts=3)

    mock_sleep.assert_awaited_once_with(30.0)


# ---------------------------------------------------------------------------
# Extra: max_attempts exhausted → last response returned
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_exhausts_max_attempts_returns_last():
    responses = [_make_response(503) for _ in range(3)]
    for r in responses:
        r.request = httpx.Request("GET", FAKE_URL)

    client = _mock_client(responses)

    with patch("devops_mcp.client.asyncio.sleep", new=AsyncMock()):
        result = await request_with_retry(client, "GET", FAKE_URL, max_attempts=3)

    assert result.status_code == 503
    assert client.request.call_count == 3


# ---------------------------------------------------------------------------
# Extra: 502/504 on GET → retried
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("status", [502, 504])
async def test_5xx_get_retries(status: int):
    err = _make_response(status)
    err.request = httpx.Request("GET", FAKE_URL)
    ok = _make_response(200)
    ok.request = httpx.Request("GET", FAKE_URL)

    client = _mock_client([err, ok])

    with patch("devops_mcp.client.asyncio.sleep", new=AsyncMock()):
        result = await request_with_retry(client, "GET", FAKE_URL, max_attempts=3)

    assert result.status_code == 200
    assert client.request.call_count == 2


# ---------------------------------------------------------------------------
# Extra: 502/504 on PATCH → returns immediately (non-idempotent gate)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("status", [502, 504])
async def test_5xx_patch_no_retry(status: int):
    err = _make_response(status)
    err.request = httpx.Request("PATCH", FAKE_URL)

    client = _mock_client([err])

    with patch("devops_mcp.client.asyncio.sleep", new=AsyncMock()) as mock_sleep:
        result = await request_with_retry(client, "PATCH", FAKE_URL, max_attempts=3)

    assert result.status_code == status
    assert client.request.call_count == 1
    mock_sleep.assert_not_awaited()

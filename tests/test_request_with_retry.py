"""Unit tests for client.request_with_retry.

Covers:
- (a) 429 with Retry-After on a GET → retries after honouring the header.
- (b) 503 on a GET → retries (idempotent method + retryable status).
- (c) 503 on a POST → returns immediately, NO retry (idempotency gate).
- (d) 429 on a POST → retried (throttle is safe for all methods).
- (e) 2xx → returned unchanged, no retry.
- (f) 200 + Retry-After → proactive sleep, then 200 returned.
- (g) 200 + non-numeric Retry-After → NO sleep, 200 returned.
- (h) 200 + Retry-After > cap → sleep capped at 30 s.
- (i) 200 + X-RateLimit-Remaining=0 (no Retry-After) → WARNING logged, no sleep.
- (j) Idempotency gate unchanged: POST/PATCH 502/503/504 not retried; GET 503 retried.
- (k) 429 POST retried and honours Retry-After.
- (_parse_retry_after) unit tests on the shared parse helper.

asyncio.sleep is patched to avoid real delays; call counts verify behaviour.
"""

import asyncio
import logging
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from devops_mcp.client import _parse_retry_after, request_with_retry

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


# ===========================================================================
# _parse_retry_after unit tests
# ===========================================================================

class TestParseRetryAfter:
    """Unit tests for the _parse_retry_after helper."""

    def test_none_input_returns_none(self):
        assert _parse_retry_after(None) is None

    def test_valid_seconds_returns_float(self):
        assert _parse_retry_after("5") == 5.0

    def test_float_string_returns_float(self):
        assert _parse_retry_after("2.5") == 2.5

    def test_non_numeric_returns_none(self):
        assert _parse_retry_after("soon") is None

    def test_empty_string_returns_none(self):
        assert _parse_retry_after("") is None

    def test_value_above_cap_is_clamped(self):
        assert _parse_retry_after("120") == 30.0

    def test_value_at_cap_is_unchanged(self):
        assert _parse_retry_after("30") == 30.0

    def test_value_below_cap_is_unchanged(self):
        assert _parse_retry_after("10") == 10.0

    def test_zero_returns_none(self):
        assert _parse_retry_after("0") is None

    def test_negative_returns_none(self):
        assert _parse_retry_after("-5") is None


# ===========================================================================
# Proactive throttle: 2xx + Retry-After (findings #1)
# ===========================================================================

# ---------------------------------------------------------------------------
# (f) 200 + Retry-After → bounded sleep, then 200 returned
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_200_with_retry_after_sleeps_then_returns():
    """A 200 carrying Retry-After causes a bounded sleep and the 200 is returned."""
    ok = _make_response(200, {"retry-after": "5"})
    ok.request = httpx.Request("GET", FAKE_URL)

    client = _mock_client([ok])

    with patch("devops_mcp.client.asyncio.sleep", new=AsyncMock()) as mock_sleep:
        result = await request_with_retry(client, "GET", FAKE_URL, max_attempts=3)

    assert result.status_code == 200
    assert client.request.call_count == 1
    mock_sleep.assert_awaited_once_with(5.0)


# ---------------------------------------------------------------------------
# (g) 200 + non-numeric Retry-After → NO sleep, 200 returned
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_200_with_non_numeric_retry_after_no_sleep():
    """A non-numeric Retry-After value must be ignored; no sleep, 200 returned."""
    ok = _make_response(200, {"retry-after": "soon"})
    ok.request = httpx.Request("GET", FAKE_URL)

    client = _mock_client([ok])

    with patch("devops_mcp.client.asyncio.sleep", new=AsyncMock()) as mock_sleep:
        result = await request_with_retry(client, "GET", FAKE_URL, max_attempts=3)

    assert result.status_code == 200
    assert client.request.call_count == 1
    mock_sleep.assert_not_awaited()


# ---------------------------------------------------------------------------
# (h) 200 + Retry-After > 30 → sleep capped at 30s
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_200_retry_after_capped_at_30s():
    """A Retry-After value of 120 must be capped at 30 s (the _RETRY_MAX_WAIT_SECONDS bound)."""
    ok = _make_response(200, {"retry-after": "120"})
    ok.request = httpx.Request("GET", FAKE_URL)

    client = _mock_client([ok])

    with patch("devops_mcp.client.asyncio.sleep", new=AsyncMock()) as mock_sleep:
        result = await request_with_retry(client, "GET", FAKE_URL, max_attempts=3)

    assert result.status_code == 200
    assert client.request.call_count == 1
    mock_sleep.assert_awaited_once_with(30.0)


# ---------------------------------------------------------------------------
# (i) 200 + X-RateLimit-Remaining=0, no Retry-After → WARNING, no sleep
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_200_rate_limit_remaining_zero_warns_no_sleep(caplog):
    """X-RateLimit-Remaining=0 without Retry-After must log a WARNING but not sleep."""
    ok = _make_response(200, {"x-ratelimit-remaining": "0"})
    ok.request = httpx.Request("GET", FAKE_URL)

    client = _mock_client([ok])

    with patch("devops_mcp.client.asyncio.sleep", new=AsyncMock()) as mock_sleep:
        with caplog.at_level(logging.WARNING, logger="devops_mcp.client"):
            result = await request_with_retry(client, "GET", FAKE_URL, max_attempts=3)

    assert result.status_code == 200
    assert client.request.call_count == 1
    mock_sleep.assert_not_awaited()
    assert any("X-RateLimit-Remaining=0" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# (i-b) 200 + X-RateLimit-Remaining=0 AND Retry-After → single WARNING + sleep once
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_200_rate_limit_remaining_zero_with_retry_after_sleeps_once(caplog):
    """When both X-RateLimit-Remaining=0 and Retry-After are present, sleep exactly once."""
    ok = _make_response(200, {"x-ratelimit-remaining": "0", "retry-after": "7"})
    ok.request = httpx.Request("GET", FAKE_URL)

    client = _mock_client([ok])

    with patch("devops_mcp.client.asyncio.sleep", new=AsyncMock()) as mock_sleep:
        with caplog.at_level(logging.WARNING, logger="devops_mcp.client"):
            result = await request_with_retry(client, "GET", FAKE_URL, max_attempts=3)

    assert result.status_code == 200
    assert client.request.call_count == 1
    mock_sleep.assert_awaited_once_with(7.0)


# ===========================================================================
# Idempotency gate unchanged (finding #1 — verify gate not broken by changes)
# ===========================================================================

# ---------------------------------------------------------------------------
# (j-1) POST 502 → NOT retried (idempotency gate)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_502_post_not_retried():
    """POST 502 must return immediately — exactly one request, 502 returned."""
    err = _make_response(502)
    err.request = httpx.Request("POST", FAKE_URL)

    client = _mock_client([err])

    with patch("devops_mcp.client.asyncio.sleep", new=AsyncMock()) as mock_sleep:
        result = await request_with_retry(client, "POST", FAKE_URL, max_attempts=3)

    assert result.status_code == 502
    assert client.request.call_count == 1
    mock_sleep.assert_not_awaited()


# ---------------------------------------------------------------------------
# (j-2) PATCH 503 → NOT retried (idempotency gate)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_503_patch_not_retried():
    """PATCH 503 must return immediately — exactly one request, 503 returned."""
    err = _make_response(503)
    err.request = httpx.Request("PATCH", FAKE_URL)

    client = _mock_client([err])

    with patch("devops_mcp.client.asyncio.sleep", new=AsyncMock()) as mock_sleep:
        result = await request_with_retry(client, "PATCH", FAKE_URL, max_attempts=3)

    assert result.status_code == 503
    assert client.request.call_count == 1
    mock_sleep.assert_not_awaited()


# ---------------------------------------------------------------------------
# (j-3) PATCH 504 → NOT retried (idempotency gate)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_504_patch_not_retried():
    """PATCH 504 must return immediately — exactly one request, 504 returned."""
    err = _make_response(504)
    err.request = httpx.Request("PATCH", FAKE_URL)

    client = _mock_client([err])

    with patch("devops_mcp.client.asyncio.sleep", new=AsyncMock()) as mock_sleep:
        result = await request_with_retry(client, "PATCH", FAKE_URL, max_attempts=3)

    assert result.status_code == 504
    assert client.request.call_count == 1
    mock_sleep.assert_not_awaited()


# ---------------------------------------------------------------------------
# (j-4) GET 503 → IS retried (idempotent method)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_503_get_is_retried():
    """GET 503 is a retryable transient error; the request must be retried."""
    err = _make_response(503)
    err.request = httpx.Request("GET", FAKE_URL)
    ok = _make_response(200)
    ok.request = httpx.Request("GET", FAKE_URL)

    client = _mock_client([err, ok])

    with patch("devops_mcp.client.asyncio.sleep", new=AsyncMock()):
        result = await request_with_retry(client, "GET", FAKE_URL, max_attempts=3)

    assert result.status_code == 200
    assert client.request.call_count == 2


# ---------------------------------------------------------------------------
# (k) 429 on POST → retried AND honours Retry-After
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_429_post_retried_honours_retry_after():
    """POST 429 must be retried (throttled = not yet executed) and honour Retry-After."""
    throttled = _make_response(429, {"retry-after": "3"})
    throttled.request = httpx.Request("POST", FAKE_URL)
    ok = _make_response(200)
    ok.request = httpx.Request("POST", FAKE_URL)

    client = _mock_client([throttled, ok])

    with patch("devops_mcp.client.asyncio.sleep", new=AsyncMock()) as mock_sleep:
        result = await request_with_retry(client, "POST", FAKE_URL, max_attempts=3)

    assert result.status_code == 200
    assert client.request.call_count == 2
    mock_sleep.assert_awaited_once_with(3.0)

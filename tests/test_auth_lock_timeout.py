"""Unit tests for per-scope token lock, re-check, and auth timeout in client.py.

Covers:
- (a) Concurrency: N parallel cold-cache build_headers calls → get_bearer_token
  (and thus credential.get_token) is invoked exactly once.
- (b) Timeout: a fake credential whose get_token blocks longer than the timeout →
  build_headers raises ClientAuthenticationError.
- (c) _get_auth_timeout_seconds defensive parsing: bad / negative / unset values.
"""

import asyncio
import threading
from unittest.mock import MagicMock

import pytest
from azure.core.exceptions import ClientAuthenticationError

from devops_mcp.client import (
    _AZDO_SCOPE,
    _DEFAULT_AUTH_TIMEOUT_SECONDS,
    AppContext,
    _get_auth_timeout_seconds,
    build_headers,
)

# ---------------------------------------------------------------------------
# _get_auth_timeout_seconds
# ---------------------------------------------------------------------------


def test_auth_timeout_default_when_unset(monkeypatch):
    monkeypatch.delenv("AZDO_AUTH_TIMEOUT_SECONDS", raising=False)
    assert _get_auth_timeout_seconds() == _DEFAULT_AUTH_TIMEOUT_SECONDS


def test_auth_timeout_valid_value(monkeypatch):
    monkeypatch.setenv("AZDO_AUTH_TIMEOUT_SECONDS", "60")
    assert _get_auth_timeout_seconds() == 60.0


def test_auth_timeout_invalid_string_falls_back(monkeypatch):
    monkeypatch.setenv("AZDO_AUTH_TIMEOUT_SECONDS", "notanumber")
    assert _get_auth_timeout_seconds() == _DEFAULT_AUTH_TIMEOUT_SECONDS


def test_auth_timeout_zero_falls_back(monkeypatch):
    monkeypatch.setenv("AZDO_AUTH_TIMEOUT_SECONDS", "0")
    assert _get_auth_timeout_seconds() == _DEFAULT_AUTH_TIMEOUT_SECONDS


def test_auth_timeout_negative_falls_back(monkeypatch):
    monkeypatch.setenv("AZDO_AUTH_TIMEOUT_SECONDS", "-5")
    assert _get_auth_timeout_seconds() == _DEFAULT_AUTH_TIMEOUT_SECONDS


def test_auth_timeout_empty_string_falls_back(monkeypatch):
    monkeypatch.setenv("AZDO_AUTH_TIMEOUT_SECONDS", "  ")
    assert _get_auth_timeout_seconds() == _DEFAULT_AUTH_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fake_app_ctx(credential) -> AppContext:
    """Build a minimal AppContext for testing without a real HTTP client."""
    ctx = AppContext(
        organization="fake-org",
        project="fake-project",
        credential=credential,
        http_client=None,  # type: ignore[arg-type]
    )
    return ctx


def _make_fake_access_token(token_str: str = "fake-token") -> MagicMock:
    tok = MagicMock()
    tok.token = token_str
    # Far-future expiry so the in-process cache considers it valid.
    tok.expires_on = 9_999_999_999.0
    return tok


# ---------------------------------------------------------------------------
# Concurrency: exactly one token acquisition on N parallel cold-cache callers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_cold_cache_single_acquisition(monkeypatch):
    """N concurrent build_headers calls on a cold cache should call
    credential.get_token exactly once (lock + re-check pattern)."""

    monkeypatch.delenv("AZDO_AUTH_TIMEOUT_SECONDS", raising=False)

    acquire_count = 0
    fake_access_token = _make_fake_access_token()

    class _SlowCredential:
        async def get_token_async(self, *args, **kwargs):
            nonlocal acquire_count
            acquire_count += 1
            # Yield so other coroutines can attempt to acquire the lock —
            # proving the re-check prevents duplicate acquisitions.
            await asyncio.sleep(0)
            return fake_access_token

        def get_token(self, *args, **kwargs):
            nonlocal acquire_count
            acquire_count += 1
            return fake_access_token

    app_ctx = _make_fake_app_ctx(_SlowCredential())

    # Run 8 concurrent build_headers calls with a cold cache.
    results = await asyncio.gather(*[build_headers(app_ctx) for _ in range(8)])

    # All callers should get a valid Authorization header.
    for headers in results:
        assert headers["Authorization"] == "Bearer fake-token"

    # get_token must have been called exactly once.
    assert acquire_count == 1, (
        f"credential.get_token was called {acquire_count} times; expected 1"
    )


# ---------------------------------------------------------------------------
# Timeout: hung credential → ClientAuthenticationError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_headers_timeout_raises_client_authentication_error(monkeypatch):
    """A credential whose get_token blocks beyond the timeout should cause
    build_headers to raise ClientAuthenticationError.

    NOTE: asyncio.to_thread cannot be cancelled — the underlying worker thread
    continues running after the TimeoutError is raised.  We use a threading.Event
    to unblock the thread after the assertion so the process can exit cleanly.
    """

    # Set a very short timeout so the test completes quickly.
    monkeypatch.setenv("AZDO_AUTH_TIMEOUT_SECONDS", "0.05")

    unblock = threading.Event()

    class _HungCredential:
        def get_token(self, *args, **kwargs):
            # Block until unblocked by the test teardown.  Short poll interval
            # ensures the worker thread exits promptly after the Event is set,
            # preventing the pytest process from hanging at teardown.
            unblock.wait(timeout=30)

    app_ctx = _make_fake_app_ctx(_HungCredential())

    try:
        with pytest.raises(ClientAuthenticationError) as exc_info:
            await build_headers(app_ctx)

        assert "timed out" in str(exc_info.value).lower() or "timeout" in str(exc_info.value).lower()
    finally:
        # Release the worker thread so the process can exit.
        unblock.set()


# ---------------------------------------------------------------------------
# Re-check: second caller doesn't call get_token again if first filled cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_caller_uses_cached_token(monkeypatch):
    """After the first caller populates the cache, subsequent calls return the
    cached token without calling credential.get_token again."""

    monkeypatch.delenv("AZDO_AUTH_TIMEOUT_SECONDS", raising=False)
    acquire_count = 0
    fake_access_token = _make_fake_access_token("second-call-token")

    class _CountingCredential:
        def get_token(self, *args, **kwargs):
            nonlocal acquire_count
            acquire_count += 1
            return fake_access_token

    app_ctx = _make_fake_app_ctx(_CountingCredential())

    # First call — cold cache.
    headers1 = await build_headers(app_ctx)
    assert headers1["Authorization"] == "Bearer second-call-token"
    assert acquire_count == 1

    # Second call — cache is warm.
    headers2 = await build_headers(app_ctx)
    assert headers2["Authorization"] == "Bearer second-call-token"
    # get_token still called only once (cache hit on second call).
    assert acquire_count == 1

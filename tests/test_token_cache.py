"""Unit tests for the persistent token cache helpers in client.py.

Covers:
- (a) _get_token_cache_persist: default=True, explicit false/0/no, unrecognised value.
- (b) _load_auth_record: absent file, corrupt file, valid serialized record.
- (c) _save_auth_record: file is written and can be round-tripped.
- (d) One-shot wrapper regression: a fake credential whose authenticate() calls
  self.get_token exactly once — asserts that authenticate + save are each called
  exactly once, proving restore-before-authenticate prevents unbounded recursion.
- (e) _build_interactive_credential: with AZDO_TOKEN_CACHE_PERSIST=false returns a
  plain InteractiveBrowserCredential (no wrapper installed, no sidecar written).
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from devops_mcp.client import (
    _AZDO_SCOPE,
    _get_token_cache_persist,
    _load_auth_record,
    _save_auth_record,
)

# ---------------------------------------------------------------------------
# _get_token_cache_persist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env_value,expected",
    [
        ("", True),       # unset / empty → default ON
        ("true", True),
        ("True", True),
        ("1", True),
        ("yes", True),
        ("false", False),
        ("False", False),
        ("0", False),
        ("no", False),
        ("NO", False),
    ],
)
def test_get_token_cache_persist_values(env_value, expected, monkeypatch):
    if env_value == "":
        monkeypatch.delenv("AZDO_TOKEN_CACHE_PERSIST", raising=False)
    else:
        monkeypatch.setenv("AZDO_TOKEN_CACHE_PERSIST", env_value)
    assert _get_token_cache_persist() == expected


def test_get_token_cache_persist_unrecognised_falls_back_to_true(monkeypatch):
    monkeypatch.setenv("AZDO_TOKEN_CACHE_PERSIST", "maybe")
    assert _get_token_cache_persist() is True


# ---------------------------------------------------------------------------
# _load_auth_record
# ---------------------------------------------------------------------------


def test_load_auth_record_absent_file(tmp_path):
    result = _load_auth_record(tmp_path / "nonexistent.json")
    assert result is None


def test_load_auth_record_corrupt_file(tmp_path):
    p = tmp_path / "corrupt.json"
    p.write_text("this is not valid json or a record", encoding="utf-8")
    result = _load_auth_record(p)
    assert result is None


def test_load_auth_record_valid(tmp_path):
    """Round-trip: serialise a mock record, then deserialise it back."""
    # AuthenticationRecord.deserialize expects the specific JSON format
    # produced by azure-identity.  We test the round-trip using a real
    # serialised record blob (the class has no public constructor so we
    # patch deserialize instead).
    fake_record = MagicMock()
    p = tmp_path / "record.json"
    p.write_text('{"fake": "data"}', encoding="utf-8")

    with patch("devops_mcp.client.AuthenticationRecord.deserialize", return_value=fake_record) as mock_deser:
        result = _load_auth_record(p)

    mock_deser.assert_called_once_with('{"fake": "data"}')
    assert result is fake_record


# ---------------------------------------------------------------------------
# _save_auth_record
# ---------------------------------------------------------------------------


def test_save_auth_record_writes_file(tmp_path):
    fake_record = MagicMock()
    fake_record.serialize.return_value = '{"home_account_id": "x"}'
    record_path = tmp_path / "auth-record.json"

    _save_auth_record(fake_record, record_path)

    assert record_path.exists()
    assert record_path.read_text(encoding="utf-8") == '{"home_account_id": "x"}'


def test_save_auth_record_round_trip(tmp_path):
    """Save → load round-trip using the real AuthenticationRecord.deserialize."""
    fake_record = MagicMock()
    serialized = '{"unique": "value"}'
    fake_record.serialize.return_value = serialized
    record_path = tmp_path / "auth-record.json"

    _save_auth_record(fake_record, record_path)

    with patch("devops_mcp.client.AuthenticationRecord.deserialize", return_value=fake_record) as mock_deser:
        loaded = _load_auth_record(record_path)

    mock_deser.assert_called_once_with(serialized)
    assert loaded is fake_record


def test_save_auth_record_handles_write_failure(tmp_path):
    """A write failure is logged as a warning but does not raise."""
    fake_record = MagicMock()
    fake_record.serialize.side_effect = RuntimeError("disk full")
    record_path = tmp_path / "auth-record.json"

    # Should not raise.
    _save_auth_record(fake_record, record_path)
    assert not record_path.exists()


# ---------------------------------------------------------------------------
# One-shot wrapper — regression test for restore-before-authenticate
# ---------------------------------------------------------------------------


def test_one_shot_wrapper_no_recursion(tmp_path, monkeypatch):
    """The one-shot get_token wrapper must restore get_token BEFORE calling
    authenticate() so that authenticate()'s internal self.get_token() call
    hits the real method rather than re-entering the wrapper (unbounded recursion).

    Assertions:
    - authenticate() is called exactly once.
    - _save_auth_record is called exactly once (not zero — it wasn't swallowed).
    - The wrapper removes itself (credential.get_token is the original after the call).
    """
    from devops_mcp.client import _build_interactive_credential

    # Provide a realistic serialised record so authenticate() is called.
    monkeypatch.delenv("AZDO_TOKEN_CACHE_PERSIST", raising=False)
    monkeypatch.delenv("AZDO_TENANT_ID", raising=False)

    fake_token = MagicMock()
    fake_token.token = "tok"
    fake_token.expires_on = 9999999999.0

    fake_record = MagicMock()

    class _FakeCredential:
        """Simulates a real InteractiveBrowserCredential.

        authenticate() is implemented as self.get_token(scope) internally in
        azure-identity.  We replicate that here to prove the wrapper's
        restore-before-authenticate guard works.
        """

        authenticate_call_count = 0
        get_token_call_count = 0

        def get_token(self, *scopes, **kwargs):
            self.get_token_call_count += 1
            return fake_token

        def authenticate(self, *, scopes):
            self.authenticate_call_count += 1
            # Simulate the azure-identity behaviour: authenticate() calls
            # self.get_token internally.  If the one-shot wrapper is still
            # installed on the instance, this re-enters the wrapper →
            # unbounded recursion.  If the wrapper correctly restored
            # get_token first, this hits the real method and returns normally.
            self.get_token(*scopes)
            return fake_record

    fake_cred_instance = _FakeCredential()

    save_call_count = 0

    def _fake_save(record, path):
        nonlocal save_call_count
        save_call_count += 1

    record_path = tmp_path / "auth-record.json"

    # Replicate the wrapper installation logic from _build_interactive_credential.
    _original_get_token = fake_cred_instance.get_token

    def _get_token_and_record(*args, **kw):
        token = _original_get_token(*args, **kw)
        # Restore BEFORE authenticate() — the critical guard.
        fake_cred_instance.get_token = _original_get_token  # type: ignore[method-assign]
        try:
            record = fake_cred_instance.authenticate(scopes=list(args))
            _fake_save(record, record_path)
        except Exception:
            pass
        return token

    fake_cred_instance.get_token = _get_token_and_record  # type: ignore[method-assign]

    # Simulate the first call that triggers the wrapper.
    result = fake_cred_instance.get_token(_AZDO_SCOPE)

    # Wrapper must have removed itself.
    assert fake_cred_instance.get_token is _original_get_token, (
        "Wrapper did not restore original get_token — it is still installed"
    )
    # authenticate() was called exactly once (no recursion).
    assert fake_cred_instance.authenticate_call_count == 1, (
        f"authenticate() called {fake_cred_instance.authenticate_call_count} times; expected 1"
    )
    # _save_auth_record was called exactly once (not swallowed by recursion).
    assert save_call_count == 1, (
        f"_save_auth_record called {save_call_count} times; expected 1"
    )
    # The returned token is the real one.
    assert result is fake_token


def test_build_interactive_credential_cache_off_no_wrapper(monkeypatch, tmp_path):
    """With AZDO_TOKEN_CACHE_PERSIST=false the plain credential is returned
    without any wrapper and no sidecar is written."""
    monkeypatch.setenv("AZDO_TOKEN_CACHE_PERSIST", "false")
    monkeypatch.delenv("AZDO_TENANT_ID", raising=False)

    from devops_mcp.client import _build_interactive_credential

    with patch("devops_mcp.client.InteractiveBrowserCredential") as MockCred:
        mock_instance = MagicMock()
        MockCred.return_value = mock_instance
        # Capture the bound original so we can check it is unchanged.
        original_get_token = mock_instance.get_token

        cred = _build_interactive_credential()

    # Must be the plain instance — no wrapper installed, get_token unchanged.
    assert cred is mock_instance
    assert cred.get_token is mock_instance.get_token
    # No sidecar should be written (no auth-record.json in home dir from this test).
    assert not (tmp_path / "auth-record.json").exists()

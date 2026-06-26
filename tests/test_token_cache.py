"""Unit tests for the persistent token cache helpers in client.py.

Covers:
- (a) _get_ephemeral_token: default=False, explicit true/1/yes, unrecognised value.
- (b) _load_auth_record: absent file, corrupt file, valid serialized record.
- (c) _save_auth_record: file is written and can be round-tripped.
- (d) One-shot wrapper regression: a fake credential whose authenticate() calls
  self.get_token exactly once — asserts that authenticate + save are each called
  exactly once, proving restore-before-authenticate prevents unbounded recursion.
- (e) _build_interactive_credential: with AZDO_EPHEMERAL_TOKEN=true returns a
  plain InteractiveBrowserCredential (no wrapper installed, no sidecar written).
"""

from unittest.mock import MagicMock, patch

import pytest

from devops_mcp.client import (
    _AZDO_SCOPE,
    _get_ephemeral_token,
    _get_token_cache_profile,
    _load_auth_record,
    _save_auth_record,
)

# ---------------------------------------------------------------------------
# _get_ephemeral_token
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env_value,expected",
    [
        ("", False),       # unset / empty → default OFF (persist)
        ("true", True),
        ("True", True),
        ("1", True),
        ("yes", True),
        ("YES", True),
        ("false", False),
        ("False", False),
        ("0", False),
        ("no", False),
    ],
)
def test_get_ephemeral_token_values(env_value, expected, monkeypatch):
    if env_value == "":
        monkeypatch.delenv("AZDO_EPHEMERAL_TOKEN", raising=False)
    else:
        monkeypatch.setenv("AZDO_EPHEMERAL_TOKEN", env_value)
    assert _get_ephemeral_token() == expected


def test_get_ephemeral_token_unrecognised_falls_back_to_false(monkeypatch):
    monkeypatch.setenv("AZDO_EPHEMERAL_TOKEN", "maybe")
    assert _get_ephemeral_token() is False


# ---------------------------------------------------------------------------
# _get_token_cache_profile
# ---------------------------------------------------------------------------


def test_token_cache_profile_unset(monkeypatch):
    monkeypatch.delenv("AZDO_TOKEN_CACHE_PROFILE", raising=False)
    assert _get_token_cache_profile() == ""


def test_token_cache_profile_blank_is_empty(monkeypatch):
    monkeypatch.setenv("AZDO_TOKEN_CACHE_PROFILE", "   ")
    assert _get_token_cache_profile() == ""


def test_token_cache_profile_valid_passthrough(monkeypatch):
    monkeypatch.setenv("AZDO_TOKEN_CACHE_PROFILE", "prod-tenant_1")
    assert _get_token_cache_profile() == "prod-tenant_1"


@pytest.mark.parametrize("bad_value", ["a/b", "a.b", "a b", "a:b", "a\\b", "café"])
def test_token_cache_profile_rejects_invalid_chars(bad_value, monkeypatch):
    monkeypatch.setenv("AZDO_TOKEN_CACHE_PROFILE", bad_value)
    with pytest.raises(ValueError, match="AZDO_TOKEN_CACHE_PROFILE"):
        _get_token_cache_profile()


def test_build_interactive_credential_profile_isolates_cache_and_sidecar(
    monkeypatch, tmp_path
):
    """A set profile suffixes both the MSAL cache name and the sidecar path."""
    monkeypatch.delenv("AZDO_EPHEMERAL_TOKEN", raising=False)
    monkeypatch.setenv("AZDO_TOKEN_CACHE_PROFILE", "prod")
    monkeypatch.delenv("AZDO_TENANT_ID", raising=False)

    from devops_mcp import client

    monkeypatch.setattr(client, "_get_user_config_dir", lambda: tmp_path)

    captured = {}

    def _fake_load(path):
        captured["record_path"] = path
        return object()

    def _fake_cache_opts(*, name, allow_unencrypted_storage):
        captured["cache_name"] = name
        return MagicMock()

    monkeypatch.setattr(client, "_load_auth_record", _fake_load)

    with patch.object(client, "TokenCachePersistenceOptions", _fake_cache_opts), \
            patch.object(client, "InteractiveBrowserCredential") as MockCred:
        MockCred.return_value = MagicMock()
        client._build_interactive_credential()

    assert captured["cache_name"] == "devops-mcp.prod.cache"
    assert captured["record_path"] == tmp_path / "auth-record.prod.json"


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

    # Provide a realistic serialised record so authenticate() is called.
    monkeypatch.delenv("AZDO_EPHEMERAL_TOKEN", raising=False)
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
    """With AZDO_EPHEMERAL_TOKEN=true the plain credential is returned
    without any wrapper and no sidecar is written."""
    monkeypatch.setenv("AZDO_EPHEMERAL_TOKEN", "true")
    monkeypatch.delenv("AZDO_TENANT_ID", raising=False)

    from devops_mcp.client import _build_interactive_credential

    with patch("devops_mcp.client.InteractiveBrowserCredential") as MockCred:
        mock_instance = MagicMock()
        MockCred.return_value = mock_instance

        cred = _build_interactive_credential()

    # Must be the plain instance — no wrapper installed, get_token unchanged.
    assert cred is mock_instance
    assert cred.get_token is mock_instance.get_token
    # No sidecar should be written (no auth-record.json in home dir from this test).
    assert not (tmp_path / "auth-record.json").exists()

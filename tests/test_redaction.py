"""Unit tests for devops_mcp.redaction — pure functions, no network required.

Covers the secret-hygiene contract implemented in devops_mcp.redaction:
- Rule 1 (allowlist): unknown authorization.parameters keys are dropped and
  reported by name only, including for a wholly novel/unknown endpoint type.
- Rule 2 (denylist): name-substring matches on data/providerData/variable
  names are dropped and reported.
- Rule 3 (never trust server nulling): a secret variable's value is always
  omitted from our own projection, regardless of what the server returned.
"""

from devops_mcp.redaction import (
    AUTH_PARAM_ALLOWLIST,
    allowlist_mapping,
    is_secret_name,
    project_identity,
    scrub_mapping,
)
from devops_mcp.tools.variable_groups import _project_variable

# ---------------------------------------------------------------------------
# is_secret_name — name-substring denylist
# ---------------------------------------------------------------------------


class TestIsSecretName:
    def test_exact_match_password(self):
        assert is_secret_name("password") is True

    def test_case_insensitive(self):
        assert is_secret_name("DB_PASSWORD") is True
        assert is_secret_name("ApiKey") is True

    def test_substring_match(self):
        assert is_secret_name("my_connectionString_prod") is True

    def test_broad_substring_key(self):
        # 'key' is deliberately broad — over-redaction is the accepted trade.
        assert is_secret_name("region_key") is True

    def test_broad_substring_auth(self):
        assert is_secret_name("authType") is True

    def test_non_secret_name(self):
        assert is_secret_name("environment") is False
        assert is_secret_name("subscriptionId") is False


# ---------------------------------------------------------------------------
# scrub_mapping — Rule 2 denylist applied to a flat mapping
# ---------------------------------------------------------------------------


class TestScrubMapping:
    def test_denylist_hits_are_dropped(self):
        kept, dropped = scrub_mapping({
            "subscriptionId": "abc-123",
            "password": "hunter2",
            "connectionString": "server=...;password=...",
        })
        assert kept == {"subscriptionId": "abc-123"}
        assert set(dropped) == {"password", "connectionString"}

    def test_no_hits_returns_everything(self):
        kept, dropped = scrub_mapping({"environment": "prod", "scopeLevel": "Subscription"})
        assert kept == {"environment": "prod", "scopeLevel": "Subscription"}
        assert dropped == []

    def test_none_input(self):
        kept, dropped = scrub_mapping(None)
        assert kept == {}
        assert dropped == []

    def test_empty_input(self):
        kept, dropped = scrub_mapping({})
        assert kept == {}
        assert dropped == []


# ---------------------------------------------------------------------------
# allowlist_mapping — Rule 1 allowlist applied to authorization.parameters
# ---------------------------------------------------------------------------


class TestAllowlistMapping:
    def test_known_keys_kept_unknown_dropped_and_reported(self):
        """Unknown auth parameter is dropped from the kept dict but its NAME
        (never its value) is reported in dropped."""
        kept, dropped = allowlist_mapping(
            {"tenantId": "tenant-1", "password": "hunter2"},
            AUTH_PARAM_ALLOWLIST,
        )
        assert kept == {"tenantId": "tenant-1"}
        assert dropped == ["password"]
        # The dropped list must never carry the value.
        assert "hunter2" not in dropped

    def test_case_insensitive_key_match(self):
        kept, dropped = allowlist_mapping({"TENANTID": "tenant-1"}, AUTH_PARAM_ALLOWLIST)
        assert kept == {"TENANTID": "tenant-1"}
        assert dropped == []

    def test_unknown_endpoint_type_with_novel_auth_params(self):
        """A third-party/extension endpoint type can define arbitrary new
        parameter names — none of them are in the allowlist, so all must be
        dropped and none of their values leaked, even though the allowlist
        has no specific knowledge of this endpoint type."""
        kept, dropped = allowlist_mapping(
            {
                "customSecretBlob": "should-never-appear",
                "vendorApiSecret": "super-secret-value",
                "obscureNewAuthField": "also-secret",
            },
            AUTH_PARAM_ALLOWLIST,
        )
        assert kept == {}
        assert set(dropped) == {"customSecretBlob", "vendorApiSecret", "obscureNewAuthField"}

    def test_none_input(self):
        kept, dropped = allowlist_mapping(None, AUTH_PARAM_ALLOWLIST)
        assert kept == {}
        assert dropped == []


# ---------------------------------------------------------------------------
# project_identity — IdentityRef projection
# ---------------------------------------------------------------------------


class TestProjectIdentity:
    def test_projects_only_allowed_fields(self):
        ref = {
            "id": "user-1",
            "displayName": "Jane Doe",
            "uniqueName": "jane@example.com",
            "_links": {"self": "https://..."},
            "imageUrl": "https://...",
            "descriptor": "aad.abc123",
        }
        result = project_identity(ref)
        assert result == {"id": "user-1", "display_name": "Jane Doe", "unique_name": "jane@example.com"}
        assert "_links" not in result
        assert "imageUrl" not in result
        assert "descriptor" not in result

    def test_none_input(self):
        assert project_identity(None) is None

    def test_empty_dict_input(self):
        assert project_identity({}) is None


# ---------------------------------------------------------------------------
# _project_variable — Rule 3: secret variable value omission
# ---------------------------------------------------------------------------


class TestProjectVariableSecretOmission:
    def test_secret_variable_value_key_absent(self):
        """isSecret=true must never carry a 'value' key in our output, even
        though the server response includes 'value': null."""
        item = _project_variable("DB_PASSWORD", {"value": None, "isSecret": True}, include_values=True)
        assert "value" not in item
        assert item["is_secret"] is True
        assert item["value_available"] is False

    def test_secret_variable_ignores_include_values_true(self):
        """Rule 3 — never trust the server's nulling, and never leak a value
        even if the caller asked for include_values=True."""
        item = _project_variable(
            "API_TOKEN", {"value": "should-never-leak", "isSecret": True}, include_values=True
        )
        assert "value" not in item

    def test_name_heuristic_redacts_unmarked_secret(self):
        item = _project_variable("DB_PASSWORD", {"value": "plaintext-oops", "isSecret": False}, include_values=True)
        assert item["value"] is None
        assert item["redacted"] == "name_heuristic"

    def test_non_secret_value_included_when_requested(self):
        item = _project_variable("ENVIRONMENT", {"value": "staging", "isSecret": False}, include_values=True)
        assert item["value"] == "staging"
        assert "redacted" not in item

    def test_value_omitted_when_include_values_false(self):
        item = _project_variable("ENVIRONMENT", {"value": "staging", "isSecret": False}, include_values=False)
        assert "value" not in item

    def test_long_value_truncated(self):
        long_value = "x" * 5000
        item = _project_variable("BLOB", {"value": long_value, "isSecret": False}, include_values=True)
        assert len(item["value"]) == 4096
        assert item["truncated"] is True

    def test_short_value_not_truncated(self):
        item = _project_variable("SHORT", {"value": "abc", "isSecret": False}, include_values=True)
        assert item["value"] == "abc"
        assert "truncated" not in item

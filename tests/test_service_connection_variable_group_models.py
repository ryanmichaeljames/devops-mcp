"""Unit tests for models.py input validation — service connections and variable groups.

Covers:
- GetServiceConnectionInput: endpoint_id GUID validation, include_data default.
- ListServiceConnectionsInput: names/auth_schemes max-length caps, top bounds.
- ListVariableGroupsInput: top bounds, include_values default, query_order literal.
- GetVariableGroupInput: group_id bounds, include_values default.
"""

import pytest
from pydantic import ValidationError

from devops_mcp.models import (
    GetServiceConnectionInput,
    GetVariableGroupInput,
    ListServiceConnectionsInput,
    ListVariableGroupsInput,
)

_VALID_GUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_INVALID_GUID = "not-a-guid"


# ---------------------------------------------------------------------------
# GetServiceConnectionInput
# ---------------------------------------------------------------------------


class TestGetServiceConnectionInput:
    def test_valid_guid_passes(self):
        model = GetServiceConnectionInput(endpoint_id=_VALID_GUID)
        assert model.endpoint_id == _VALID_GUID

    def test_malformed_guid_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            GetServiceConnectionInput(endpoint_id=_INVALID_GUID)
        assert "endpoint_id" in str(exc_info.value)
        assert "GUID" in str(exc_info.value)

    def test_include_data_defaults_true(self):
        model = GetServiceConnectionInput(endpoint_id=_VALID_GUID)
        assert model.include_data is True

    def test_include_data_explicit_false(self):
        model = GetServiceConnectionInput(endpoint_id=_VALID_GUID, include_data=False)
        assert model.include_data is False


# ---------------------------------------------------------------------------
# ListServiceConnectionsInput
# ---------------------------------------------------------------------------


class TestListServiceConnectionsInput:
    def test_defaults(self):
        model = ListServiceConnectionsInput()
        assert model.top == 100
        assert model.type is None
        assert model.names is None
        assert model.auth_schemes is None
        assert model.include_failed is None

    def test_top_exceeds_le_raises(self):
        with pytest.raises(ValidationError):
            ListServiceConnectionsInput(top=501)

    def test_top_at_le_passes(self):
        model = ListServiceConnectionsInput(top=500)
        assert model.top == 500

    def test_top_below_ge_raises(self):
        with pytest.raises(ValidationError):
            ListServiceConnectionsInput(top=0)

    def test_names_within_max_length_passes(self):
        model = ListServiceConnectionsInput(names=["a"] * 50)
        assert len(model.names) == 50

    def test_names_exceeds_max_length_raises(self):
        with pytest.raises(ValidationError):
            ListServiceConnectionsInput(names=["a"] * 51)

    def test_auth_schemes_within_max_length_passes(self):
        model = ListServiceConnectionsInput(auth_schemes=["a"] * 10)
        assert len(model.auth_schemes) == 10

    def test_auth_schemes_exceeds_max_length_raises(self):
        with pytest.raises(ValidationError):
            ListServiceConnectionsInput(auth_schemes=["a"] * 11)

    def test_extra_field_forbidden(self):
        with pytest.raises(ValidationError):
            ListServiceConnectionsInput(includeDetails=True)


# ---------------------------------------------------------------------------
# ListVariableGroupsInput
# ---------------------------------------------------------------------------


class TestListVariableGroupsInput:
    def test_defaults(self):
        model = ListVariableGroupsInput()
        assert model.top == 100
        assert model.include_values is False
        assert model.continuation_token is None
        assert model.query_order is None

    def test_include_values_explicit_true(self):
        model = ListVariableGroupsInput(include_values=True)
        assert model.include_values is True

    def test_top_exceeds_le_raises(self):
        with pytest.raises(ValidationError):
            ListVariableGroupsInput(top=501)

    def test_top_at_le_passes(self):
        model = ListVariableGroupsInput(top=500)
        assert model.top == 500

    def test_valid_query_order(self):
        model = ListVariableGroupsInput(query_order="IdAscending")
        assert model.query_order == "IdAscending"

    def test_invalid_query_order_raises(self):
        with pytest.raises(ValidationError):
            ListVariableGroupsInput(query_order="Bogus")

    def test_group_name_wildcard_passes(self):
        model = ListVariableGroupsInput(group_name="Prod*")
        assert model.group_name == "Prod*"


# ---------------------------------------------------------------------------
# GetVariableGroupInput
# ---------------------------------------------------------------------------


class TestGetVariableGroupInput:
    def test_valid_group_id(self):
        model = GetVariableGroupInput(group_id=42)
        assert model.group_id == 42

    def test_group_id_below_ge_raises(self):
        with pytest.raises(ValidationError):
            GetVariableGroupInput(group_id=0)

    def test_include_values_defaults_true(self):
        model = GetVariableGroupInput(group_id=1)
        assert model.include_values is True

    def test_include_values_explicit_false(self):
        model = GetVariableGroupInput(group_id=1, include_values=False)
        assert model.include_values is False

    def test_missing_group_id_raises(self):
        with pytest.raises(ValidationError):
            GetVariableGroupInput()

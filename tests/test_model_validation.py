"""Unit tests for models.py input validation.

Covers:
- GUID validators: malformed GUIDs raise ValidationError; valid GUIDs pass;
  optional GUID fields accept None.
- Regression guard: repository_id accepts a non-GUID name (no over-constraining).
- Bounded top defaults: list models default to 100, exceeding le raises
  ValidationError.
"""

import pytest
from pydantic import ValidationError

from devops_mcp.models import (
    CreatePullRequestInput,
    ListBranchesInput,
    ListPipelineRunsInput,
    ListPipelinesInput,
    ListPullRequestsInput,
    UpdatePullRequestInput,
)

# ---------------------------------------------------------------------------
# Fake constants — no real identifiers
# ---------------------------------------------------------------------------

_VALID_GUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_VALID_GUID_2 = "00000000-0000-0000-0000-000000000000"
_INVALID_GUID = "not-a-guid"
_INVALID_GUID_SHORT = "1234"
_FAKE_REPO_NAME = "my-repo"

# Minimal required fields for models that need them
_BASE_PR_KWARGS = {
    "repository_id": "fake-repo",
    "source_ref_name": "refs/heads/feature/x",
    "target_ref_name": "refs/heads/main",
    "title": "Test PR",
}
_BASE_LIST_PR_KWARGS = {
    "repository_id": "fake-repo",
}

# ---------------------------------------------------------------------------
# CreatePullRequestInput — reviewers GUID list
# ---------------------------------------------------------------------------


class TestCreatePullRequestReviewers:
    def test_valid_guid_passes(self):
        model = CreatePullRequestInput(
            **_BASE_PR_KWARGS,
            reviewers=[_VALID_GUID],
        )
        assert model.reviewers == [_VALID_GUID]

    def test_multiple_valid_guids_pass(self):
        model = CreatePullRequestInput(
            **_BASE_PR_KWARGS,
            reviewers=[_VALID_GUID, _VALID_GUID_2],
        )
        assert len(model.reviewers) == 2

    def test_none_passes(self):
        model = CreatePullRequestInput(**_BASE_PR_KWARGS, reviewers=None)
        assert model.reviewers is None

    def test_omitted_passes(self):
        model = CreatePullRequestInput(**_BASE_PR_KWARGS)
        assert model.reviewers is None

    def test_malformed_guid_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            CreatePullRequestInput(
                **_BASE_PR_KWARGS,
                reviewers=[_INVALID_GUID],
            )
        assert "reviewers" in str(exc_info.value)
        assert "GUID" in str(exc_info.value)

    def test_mixed_valid_and_invalid_raises(self):
        """A single bad element in the list should still fail."""
        with pytest.raises(ValidationError):
            CreatePullRequestInput(
                **_BASE_PR_KWARGS,
                reviewers=[_VALID_GUID, _INVALID_GUID],
            )

    def test_repository_id_accepts_name(self):
        """repository_id must NOT be GUID-validated — names are valid identifiers."""
        model = CreatePullRequestInput(
            **{**_BASE_PR_KWARGS, "repository_id": _FAKE_REPO_NAME},
        )
        assert model.repository_id == _FAKE_REPO_NAME


# ---------------------------------------------------------------------------
# ListPullRequestsInput — creator_id and reviewer_id GUID fields
# ---------------------------------------------------------------------------


class TestListPullRequestsGuids:
    def test_valid_creator_id_passes(self):
        model = ListPullRequestsInput(
            **_BASE_LIST_PR_KWARGS, creator_id=_VALID_GUID
        )
        assert model.creator_id == _VALID_GUID

    def test_none_creator_id_passes(self):
        model = ListPullRequestsInput(**_BASE_LIST_PR_KWARGS, creator_id=None)
        assert model.creator_id is None

    def test_omitted_creator_id_passes(self):
        model = ListPullRequestsInput(**_BASE_LIST_PR_KWARGS)
        assert model.creator_id is None

    def test_malformed_creator_id_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            ListPullRequestsInput(
                **_BASE_LIST_PR_KWARGS, creator_id=_INVALID_GUID
            )
        assert "creator_id" in str(exc_info.value)
        assert "GUID" in str(exc_info.value)

    def test_valid_reviewer_id_passes(self):
        model = ListPullRequestsInput(
            **_BASE_LIST_PR_KWARGS, reviewer_id=_VALID_GUID
        )
        assert model.reviewer_id == _VALID_GUID

    def test_none_reviewer_id_passes(self):
        model = ListPullRequestsInput(**_BASE_LIST_PR_KWARGS, reviewer_id=None)
        assert model.reviewer_id is None

    def test_malformed_reviewer_id_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            ListPullRequestsInput(
                **_BASE_LIST_PR_KWARGS, reviewer_id=_INVALID_GUID
            )
        assert "reviewer_id" in str(exc_info.value)
        assert "GUID" in str(exc_info.value)

    def test_repository_id_accepts_name(self):
        """Regression guard: repository_id must accept plain names."""
        model = ListPullRequestsInput(
            repository_id=_FAKE_REPO_NAME,
        )
        assert model.repository_id == _FAKE_REPO_NAME


# ---------------------------------------------------------------------------
# UpdatePullRequestInput — auto_complete_identity_id GUID field
# ---------------------------------------------------------------------------


class TestUpdatePullRequestAutoCompleteId:
    _BASE = {
        "repository_id": "fake-repo",
        "pull_request_id": 1,
    }

    def test_valid_guid_passes(self):
        model = UpdatePullRequestInput(
            **self._BASE, auto_complete_identity_id=_VALID_GUID
        )
        assert model.auto_complete_identity_id == _VALID_GUID

    def test_none_passes(self):
        model = UpdatePullRequestInput(
            **self._BASE, auto_complete_identity_id=None
        )
        assert model.auto_complete_identity_id is None

    def test_omitted_passes(self):
        model = UpdatePullRequestInput(**self._BASE)
        assert model.auto_complete_identity_id is None

    def test_malformed_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            UpdatePullRequestInput(
                **self._BASE,
                auto_complete_identity_id=_INVALID_GUID,
            )
        assert "auto_complete_identity_id" in str(exc_info.value)
        assert "GUID" in str(exc_info.value)

    def test_short_value_raises(self):
        with pytest.raises(ValidationError):
            UpdatePullRequestInput(
                **self._BASE,
                auto_complete_identity_id=_INVALID_GUID_SHORT,
            )

    def test_repository_id_accepts_name(self):
        """Regression guard: repository_id must accept plain names."""
        model = UpdatePullRequestInput(
            **{**self._BASE, "repository_id": _FAKE_REPO_NAME}
        )
        assert model.repository_id == _FAKE_REPO_NAME


# ---------------------------------------------------------------------------
# GUID case-insensitivity
# ---------------------------------------------------------------------------


class TestGuidCaseInsensitive:
    def test_uppercase_guid_passes(self):
        upper = _VALID_GUID.upper()
        model = ListPullRequestsInput(
            **_BASE_LIST_PR_KWARGS, creator_id=upper
        )
        assert model.creator_id == upper

    def test_mixed_case_guid_passes(self):
        mixed = "AAAAAAAA-bbbb-CCCC-dddd-EEEEEEEEEEEE"
        model = ListPullRequestsInput(
            **_BASE_LIST_PR_KWARGS, creator_id=mixed
        )
        assert model.creator_id == mixed


# ---------------------------------------------------------------------------
# Bounded top defaults
# ---------------------------------------------------------------------------


class TestBoundedTopDefaults:
    def test_list_pipelines_default_top(self):
        model = ListPipelinesInput()
        assert model.top == 100

    def test_list_pipelines_top_exceeds_le_raises(self):
        with pytest.raises(ValidationError):
            ListPipelinesInput(top=1001)

    def test_list_pipelines_top_at_le_passes(self):
        model = ListPipelinesInput(top=1000)
        assert model.top == 1000

    def test_list_pipelines_top_explicit_passes(self):
        model = ListPipelinesInput(top=50)
        assert model.top == 50

    def test_list_pipeline_runs_default_top(self):
        model = ListPipelineRunsInput(pipeline_id=1)
        assert model.top == 100

    def test_list_pipeline_runs_top_exceeds_le_raises(self):
        with pytest.raises(ValidationError):
            ListPipelineRunsInput(pipeline_id=1, top=10001)

    def test_list_pipeline_runs_top_at_le_passes(self):
        model = ListPipelineRunsInput(pipeline_id=1, top=10000)
        assert model.top == 10000

    def test_list_pull_requests_default_top(self):
        model = ListPullRequestsInput(**_BASE_LIST_PR_KWARGS)
        assert model.top == 100

    def test_list_pull_requests_top_exceeds_le_raises(self):
        with pytest.raises(ValidationError):
            ListPullRequestsInput(**_BASE_LIST_PR_KWARGS, top=1001)

    def test_list_pull_requests_top_at_le_passes(self):
        model = ListPullRequestsInput(**_BASE_LIST_PR_KWARGS, top=1000)
        assert model.top == 1000

    def test_list_branches_default_top(self):
        model = ListBranchesInput(repository_id="fake-repo")
        assert model.top == 100

    def test_list_branches_top_exceeds_le_raises(self):
        with pytest.raises(ValidationError):
            ListBranchesInput(repository_id="fake-repo", top=1001)

    def test_list_branches_top_at_le_passes(self):
        model = ListBranchesInput(repository_id="fake-repo", top=1000)
        assert model.top == 1000

    def test_top_ge_1_enforced(self):
        with pytest.raises(ValidationError):
            ListPipelinesInput(top=0)

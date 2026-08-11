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
    GetRepositoryImageInput,
    GetWorkItemAttachmentInput,
    LinkWorkItemAttachmentInput,
    ListBranchesInput,
    ListPipelineRunsInput,
    ListPipelinesInput,
    ListPullRequestsInput,
    ListWorkItemAttachmentsInput,
    UpdatePullRequestInput,
    UploadWorkItemAttachmentInput,
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


# ---------------------------------------------------------------------------
# GetWorkItemAttachmentInput — exactly-one-source + GUID validation
# ---------------------------------------------------------------------------

_FAKE_ATTACHMENT_URL = (
    "https://dev.azure.com/fake-org/_apis/wit/attachments/"
    "a5cedde4-2dd5-4fcf-befe-fd0977dd3433"
)


class TestGetWorkItemAttachmentInput:
    def test_attachment_id_alone_passes(self):
        model = GetWorkItemAttachmentInput(attachment_id=_VALID_GUID)
        assert model.attachment_id == _VALID_GUID
        assert model.url is None

    def test_url_alone_passes(self):
        model = GetWorkItemAttachmentInput(url=_FAKE_ATTACHMENT_URL)
        assert model.url == _FAKE_ATTACHMENT_URL
        assert model.attachment_id is None

    def test_neither_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            GetWorkItemAttachmentInput()
        assert "attachment_id" in str(exc_info.value)
        assert "url" in str(exc_info.value)

    def test_both_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            GetWorkItemAttachmentInput(attachment_id=_VALID_GUID, url=_FAKE_ATTACHMENT_URL)
        assert "exactly one" in str(exc_info.value)

    def test_malformed_attachment_id_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            GetWorkItemAttachmentInput(attachment_id=_INVALID_GUID)
        assert "attachment_id" in str(exc_info.value)
        assert "GUID" in str(exc_info.value)

    def test_file_name_hint_is_optional(self):
        model = GetWorkItemAttachmentInput(attachment_id=_VALID_GUID)
        assert model.file_name is None

    def test_file_name_hint_is_not_extension_restricted(self):
        """The download tool reports non-images too, so any name is accepted here."""
        model = GetWorkItemAttachmentInput(attachment_id=_VALID_GUID, file_name="trace.zip")
        assert model.file_name == "trace.zip"

    @pytest.mark.parametrize("name", ["a\nb.png", "a\rb.png", "a\tb.png", "a\x00b.png"])
    def test_control_characters_in_file_name_are_refused(self, name):
        """Same rule LinkWorkItemAttachmentInput applies, and the same one the
        URL parser applies to a `fileName` hint — the three entry points for a
        file name must not disagree about what is acceptable."""
        with pytest.raises(ValidationError) as exc_info:
            GetWorkItemAttachmentInput(attachment_id=_VALID_GUID, file_name=name)
        assert "control characters" in str(exc_info.value)

    def test_overlong_file_name_is_refused(self):
        with pytest.raises(ValidationError) as exc_info:
            GetWorkItemAttachmentInput(attachment_id=_VALID_GUID, file_name="x" * 260)
        assert "255" in str(exc_info.value)

    def test_whitespace_only_file_name_is_refused(self):
        with pytest.raises(ValidationError) as exc_info:
            GetWorkItemAttachmentInput(attachment_id=_VALID_GUID, file_name="   ")
        assert "file_name" in str(exc_info.value)

    def test_unknown_field_is_forbidden(self):
        with pytest.raises(ValidationError):
            GetWorkItemAttachmentInput(attachment_id=_VALID_GUID, download=True)


# ---------------------------------------------------------------------------
# UploadWorkItemAttachmentInput — source, file name, extension allowlist
# ---------------------------------------------------------------------------

_FAKE_PNG_B64 = "iVBORw0KGgo="


class TestUploadWorkItemAttachmentSource:
    def test_file_path_alone_passes(self):
        model = UploadWorkItemAttachmentInput(file_path="/fake/dir/diagram.png")
        assert model.file_path == "/fake/dir/diagram.png"

    def test_data_base64_with_file_name_passes(self):
        model = UploadWorkItemAttachmentInput(data_base64=_FAKE_PNG_B64, file_name="diagram.png")
        assert model.data_base64 == _FAKE_PNG_B64

    def test_neither_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            UploadWorkItemAttachmentInput()
        assert "exactly one" in str(exc_info.value)

    def test_both_raises(self):
        with pytest.raises(ValidationError):
            UploadWorkItemAttachmentInput(
                file_path="/fake/dir/diagram.png", data_base64=_FAKE_PNG_B64
            )

    def test_data_base64_without_file_name_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            UploadWorkItemAttachmentInput(data_base64=_FAKE_PNG_B64)
        assert "file_name" in str(exc_info.value)

    def test_invalid_base64_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            UploadWorkItemAttachmentInput(data_base64="not valid base64!!!", file_name="a.png")
        assert "base64" in str(exc_info.value)

    def test_data_uri_prefix_is_rejected(self):
        with pytest.raises(ValidationError):
            UploadWorkItemAttachmentInput(
                data_base64="data:image/png;base64,iVBORw0KGgo=", file_name="a.png"
            )


class TestUploadWorkItemAttachmentFileName:
    def test_defaults_to_posix_basename(self):
        model = UploadWorkItemAttachmentInput(file_path="/fake/dir/diagram.png")
        assert model.file_name == "diagram.png"

    def test_defaults_to_windows_basename(self):
        model = UploadWorkItemAttachmentInput(file_path=r"C:\fake\dir\diagram.png")
        assert model.file_name == "diagram.png"

    def test_explicit_file_name_wins(self):
        model = UploadWorkItemAttachmentInput(
            file_path="/fake/dir/diagram.png", file_name="renamed.jpg"
        )
        assert model.file_name == "renamed.jpg"

    @pytest.mark.parametrize("name", ["a.png", "a.PNG", "a.jpg", "a.jpeg", "a.gif", "a.webp"])
    def test_allowed_extensions_pass(self, name):
        model = UploadWorkItemAttachmentInput(data_base64=_FAKE_PNG_B64, file_name=name)
        assert model.file_name == name

    @pytest.mark.parametrize(
        "name",
        [".env", "id_rsa", "key.pem", "appsettings.json", "notes.txt", "trace.zip", "logo.svg", "noext"],
    )
    def test_non_image_extensions_are_refused(self, name):
        """The extension allowlist is a security control, not a convenience."""
        with pytest.raises(ValidationError) as exc_info:
            UploadWorkItemAttachmentInput(data_base64=_FAKE_PNG_B64, file_name=name)
        assert "extension" in str(exc_info.value)

    @pytest.mark.parametrize(
        "name",
        ["../secret.png", "dir/diagram.png", r"dir\diagram.png", r"..\..\x.png"],
    )
    def test_path_like_file_name_is_refused(self, name):
        with pytest.raises(ValidationError) as exc_info:
            UploadWorkItemAttachmentInput(data_base64=_FAKE_PNG_B64, file_name=name)
        assert "file_name" in str(exc_info.value)

    @pytest.mark.parametrize("name", ["con.png", "NUL.png", "com1.png", "LPT9.png"])
    def test_windows_reserved_names_are_refused(self, name):
        with pytest.raises(ValidationError) as exc_info:
            UploadWorkItemAttachmentInput(data_base64=_FAKE_PNG_B64, file_name=name)
        assert "reserved" in str(exc_info.value)

    def test_overlong_file_name_is_refused(self):
        with pytest.raises(ValidationError) as exc_info:
            UploadWorkItemAttachmentInput(
                data_base64=_FAKE_PNG_B64, file_name="x" * 260 + ".png"
            )
        assert "255" in str(exc_info.value)

    def test_area_path_is_optional(self):
        model = UploadWorkItemAttachmentInput(file_path="/fake/dir/diagram.png")
        assert model.area_path is None


# ---------------------------------------------------------------------------
# ListWorkItemAttachmentsInput
# ---------------------------------------------------------------------------


class TestListWorkItemAttachmentsInput:
    def test_minimal(self):
        model = ListWorkItemAttachmentsInput(work_item_id=42)
        assert model.work_item_id == 42
        # Off by default: scanning comments costs an extra round trip.
        assert model.include_comments is False

    def test_include_comments_opt_in(self):
        assert ListWorkItemAttachmentsInput(work_item_id=42, include_comments=True).include_comments

    @pytest.mark.parametrize("work_item_id", [0, -1])
    def test_work_item_id_must_be_positive(self, work_item_id):
        with pytest.raises(ValidationError):
            ListWorkItemAttachmentsInput(work_item_id=work_item_id)

    def test_work_item_id_is_required(self):
        with pytest.raises(ValidationError):
            ListWorkItemAttachmentsInput()

    def test_unknown_field_is_refused(self):
        with pytest.raises(ValidationError):
            ListWorkItemAttachmentsInput(work_item_id=42, include_relations=True)


# ---------------------------------------------------------------------------
# LinkWorkItemAttachmentInput — exactly-one-source + relation file name rules
# ---------------------------------------------------------------------------


class TestLinkWorkItemAttachmentInput:
    def test_attachment_id_alone_passes(self):
        model = LinkWorkItemAttachmentInput(work_item_id=42, attachment_id=_VALID_GUID)
        assert model.attachment_id == _VALID_GUID
        assert model.url is None

    def test_url_alone_passes(self):
        model = LinkWorkItemAttachmentInput(work_item_id=42, url=_FAKE_ATTACHMENT_URL)
        assert model.url == _FAKE_ATTACHMENT_URL

    def test_neither_source_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            LinkWorkItemAttachmentInput(work_item_id=42)
        assert "attachment_id" in str(exc_info.value)

    def test_both_sources_raise(self):
        with pytest.raises(ValidationError) as exc_info:
            LinkWorkItemAttachmentInput(
                work_item_id=42, attachment_id=_VALID_GUID, url=_FAKE_ATTACHMENT_URL
            )
        assert "not both" in str(exc_info.value)

    def test_malformed_attachment_id_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            LinkWorkItemAttachmentInput(work_item_id=42, attachment_id=_INVALID_GUID)
        assert "attachment_id" in str(exc_info.value)

    @pytest.mark.parametrize("work_item_id", [0, -3])
    def test_work_item_id_must_be_positive(self, work_item_id):
        with pytest.raises(ValidationError):
            LinkWorkItemAttachmentInput(work_item_id=work_item_id, attachment_id=_VALID_GUID)

    def test_file_name_defaults_to_none(self):
        model = LinkWorkItemAttachmentInput(work_item_id=42, attachment_id=_VALID_GUID)
        assert model.file_name is None
        assert model.comment is None

    def test_non_image_file_name_is_allowed(self):
        """A relation may point at a PDF or a zip — the upload allowlist is not this."""
        model = LinkWorkItemAttachmentInput(
            work_item_id=42, attachment_id=_VALID_GUID, file_name="trace.zip"
        )
        assert model.file_name == "trace.zip"

    @pytest.mark.parametrize("name", ["a\nb.png", "a\rb.png", "a\tb.png", "a\x00b.png"])
    def test_control_characters_in_file_name_are_refused(self, name):
        with pytest.raises(ValidationError) as exc_info:
            LinkWorkItemAttachmentInput(
                work_item_id=42, attachment_id=_VALID_GUID, file_name=name
            )
        assert "control characters" in str(exc_info.value)

    def test_whitespace_only_file_name_is_refused(self):
        with pytest.raises(ValidationError) as exc_info:
            LinkWorkItemAttachmentInput(
                work_item_id=42, attachment_id=_VALID_GUID, file_name="   "
            )
        assert "file_name" in str(exc_info.value)

    def test_overlong_file_name_is_refused(self):
        with pytest.raises(ValidationError) as exc_info:
            LinkWorkItemAttachmentInput(
                work_item_id=42, attachment_id=_VALID_GUID, file_name="x" * 260
            )
        assert "255" in str(exc_info.value)

    def test_overlong_comment_is_refused(self):
        with pytest.raises(ValidationError):
            LinkWorkItemAttachmentInput(
                work_item_id=42, attachment_id=_VALID_GUID, comment="x" * 1001
            )


# ---------------------------------------------------------------------------
# GetRepositoryImageInput — mirrors GetFileContentInput's addressing
# ---------------------------------------------------------------------------


class TestGetRepositoryImageInput:
    def test_minimal(self):
        model = GetRepositoryImageInput(repository_id=_FAKE_REPO_NAME, path="/docs/a.png")
        assert model.path == "/docs/a.png"
        assert model.branch is None
        assert model.commit_id is None

    def test_repository_name_is_accepted(self):
        """Regression guard: repository_id is a name OR a GUID, never GUID-only."""
        model = GetRepositoryImageInput(repository_id=_FAKE_REPO_NAME, path="a.png")
        assert model.repository_id == _FAKE_REPO_NAME

    def test_repository_guid_is_accepted(self):
        model = GetRepositoryImageInput(repository_id=_VALID_GUID, path="a.png")
        assert model.repository_id == _VALID_GUID

    def test_empty_path_is_refused(self):
        with pytest.raises(ValidationError):
            GetRepositoryImageInput(repository_id=_FAKE_REPO_NAME, path="")

    def test_path_is_required(self):
        with pytest.raises(ValidationError):
            GetRepositoryImageInput(repository_id=_FAKE_REPO_NAME)

    def test_branch_and_commit_may_both_be_supplied(self):
        """Precedence is a tool concern, not a validation error."""
        model = GetRepositoryImageInput(
            repository_id=_FAKE_REPO_NAME, path="a.png", branch="main", commit_id="abc123"
        )
        assert model.branch == "main"
        assert model.commit_id == "abc123"

    def test_unknown_field_is_refused(self):
        with pytest.raises(ValidationError):
            GetRepositoryImageInput(repository_id=_FAKE_REPO_NAME, path="a.png", download=True)

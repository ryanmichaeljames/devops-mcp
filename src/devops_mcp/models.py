"""Pydantic input models for all Azure DevOps MCP tools."""

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# GUID validation helpers
# ---------------------------------------------------------------------------


def _validate_guid(value: str, field: str) -> str:
    """Validate that *value* is a well-formed UUID (8-4-4-4-12 hex, case-insensitive).

    Returns the original value unchanged on success so the field stores whatever
    case the caller provided. Raises ``ValueError`` with an actionable message on
    failure so Pydantic wraps it into a ``ValidationError``.
    """
    try:
        uuid.UUID(value)
    except ValueError:
        raise ValueError(
            f"'{field}' must be a valid GUID (format: 8-4-4-4-12 hex, "
            f"e.g. 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx'); got: {value!r}"
        )
    return value


def _validate_guid_list(values: list[str], field: str) -> list[str]:
    """Validate every element in *values* as a GUID. Returns the list unchanged."""
    for v in values:
        _validate_guid(v, field)
    return values


class AzDoBaseInput(BaseModel):
    """Shared organization and project selection for all Azure DevOps tools."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    organization: str | None = Field(
        default=None,
        description=(
            "Azure DevOps organization name (e.g., 'myorg'). "
            "If omitted, falls back to the AZDO_ORGANIZATION environment variable."
        ),
    )
    project: str | None = Field(
        default=None,
        description=(
            "Azure DevOps project name or ID. "
            "If omitted, falls back to the AZDO_PROJECT environment variable."
        ),
    )


# ---------------------------------------------------------------------------
# Pipelines
# ---------------------------------------------------------------------------


class ListPipelinesInput(AzDoBaseInput):
    """Input for listing pipelines in a project."""

    top: int = Field(
        default=100,
        description="Maximum number of pipelines to return (max 1000).",
        ge=1,
        le=1000,
    )
    continuation_token: str | None = Field(
        default=None,
        description="Pagination token from a previous response.",
    )
    order_by: str | None = Field(
        default=None,
        description="Sort expression (e.g., 'name asc').",
    )


class ListPipelineRunsInput(AzDoBaseInput):
    """Input for listing runs of a specific pipeline."""

    pipeline_id: int = Field(description="The pipeline ID.", ge=1)
    top: int = Field(
        default=100,
        description="Maximum number of runs to return — client-side limit (max 10000, Azure DevOps server-side cap).",
        ge=1,
        le=10000,
    )


class GetPipelineRunInput(AzDoBaseInput):
    """Input for getting a specific pipeline run."""

    pipeline_id: int = Field(description="The pipeline ID.", ge=1)
    run_id: int = Field(description="The run ID.", ge=1)


class ListRunLogsInput(AzDoBaseInput):
    """Input for listing log entries for a pipeline run."""

    build_id: int = Field(
        description=(
            "The build/run ID. This is the 'buildId' value from the Azure DevOps "
            "build URL (e.g., ?buildId=12345). Identical to run_id."
        ),
        ge=1,
    )


class GetBuildInput(AzDoBaseInput):
    """Input for retrieving details of a specific build by build ID."""

    build_id: int = Field(
        description=(
            "The build ID. This is the 'buildId' value from the Azure DevOps "
            "build URL (e.g., ?buildId=12345). Identical to run_id."
        ),
        ge=1,
    )


class GetRunTimelineInput(AzDoBaseInput):
    """Input for retrieving a compact, filtered timeline of an Azure DevOps build/run.

    The timeline usually answers "what failed and why" with little or no log
    text, since Timeline records carry inline issue messages (issues[].message)
    for failed steps. Prefer this over pulling log content directly.
    """

    build_id: int = Field(
        description="The build/run ID (run_id and build_id are the same value).",
        ge=1,
    )
    failed_only: bool = Field(
        default=True,
        description=(
            "When True (default), only return records that failed, were "
            "canceled, succeeded with issues, or have errorCount > 0. "
            "Set False to return all timeline records."
        ),
    )
    include_issues: bool = Field(
        default=True,
        description=(
            "Include each record's inline issues[] (error/warning messages). "
            "These frequently contain the actual failure text, avoiding a "
            "separate log fetch."
        ),
    )
    include_log_line_counts: bool = Field(
        default=True,
        description=(
            "Join each record's log line count from a supplemental call to "
            "devops_list_run_logs, so the model can size a tail window "
            "without an extra round-trip. Disable to skip that extra call."
        ),
    )
    record_types: list[str] | None = Field(
        default=None,
        description=(
            "Optional case-insensitive filter on the record 'type' field "
            "(e.g., ['Task']). Type is an opaque string that varies between "
            "classic and YAML pipelines — omit to keep all types."
        ),
    )


class GetRunLogContentInput(AzDoBaseInput):
    """Input for retrieving the plain-text content of a specific log.

    BEHAVIOR CHANGE: a call with no start_line/end_line/tail now returns at
    most max_lines lines (default 500) instead of the entire log. Use the
    returned has_more/next_start_line fields to iterate, or tail to read the
    end of the log directly.
    """

    build_id: int = Field(
        description="The build/run ID (run_id and build_id are the same value).",
        ge=1,
    )
    log_id: int = Field(
        description="The log ID (obtain from devops_list_run_logs).",
        ge=1,
    )
    start_line: int | None = Field(
        default=None,
        description="Start line for partial log retrieval (1-based, inclusive).",
        ge=1,
    )
    end_line: int | None = Field(
        default=None,
        description="End line for partial log retrieval (inclusive).",
        ge=1,
    )
    max_lines: int = Field(
        default=500,
        description=(
            "Page size cap. With start_line set and no end_line, returns "
            "start_line..start_line+max_lines-1. With neither start_line nor "
            "end_line set, returns lines 1..max_lines (head). Ignored when "
            "both start_line and end_line are given."
        ),
        ge=1,
        le=5000,
    )
    tail: int | None = Field(
        default=None,
        description=(
            "Return the last N lines of the log (most errors are at the "
            "end). Mutually exclusive with start_line/end_line. Requires the "
            "log's total line count to be available (fetched automatically); "
            "fails if the log is still in progress and has no line count yet."
        ),
        ge=1,
        le=5000,
    )

    @model_validator(mode="after")
    def validate_tail_exclusivity(self) -> "GetRunLogContentInput":
        if self.tail is not None and (self.start_line is not None or self.end_line is not None):
            raise ValueError(
                "'tail' is mutually exclusive with 'start_line'/'end_line' — "
                "use tail alone to read the end of the log, or start_line/"
                "end_line to window elsewhere."
            )
        return self


class SearchRunLogInput(AzDoBaseInput):
    """Input for searching (grepping) a specific log from an Azure DevOps run.

    Downloads the full log text inside the MCP server process and returns
    only matching lines plus surrounding context — non-matching lines never
    reach the model, so a large log can cost only a few dozen lines of tokens.
    """

    build_id: int = Field(
        description="The build/run ID (run_id and build_id are the same value).",
        ge=1,
    )
    log_id: int = Field(
        description="The log ID to search (obtain from devops_list_run_logs).",
        ge=1,
    )
    pattern: str = Field(
        description="The search string (literal substring by default; see is_regex).",
        min_length=1,
        max_length=200,
    )
    is_regex: bool = Field(
        default=False,
        description=(
            "When True, treat pattern as a regular expression. Defaults to "
            "False (literal substring match), which is safer and faster."
        ),
    )
    ignore_case: bool = Field(
        default=False,
        description="Case-insensitive matching.",
    )
    context: int = Field(
        default=2,
        description="Number of lines of context to include before/after each match.",
        ge=0,
        le=10,
    )
    max_matches: int = Field(
        default=50,
        description="Maximum number of matches to return (caps payload; sets 'truncated').",
        ge=1,
        le=200,
    )


class ListBuildArtifactsInput(AzDoBaseInput):
    """Input for listing artifacts produced by a pipeline build."""

    build_id: int = Field(
        description="The build/run ID.",
        ge=1,
    )
    artifact_name: str | None = Field(
        default=None,
        description="Filter to a specific artifact by name. Omit to list all artifacts.",
    )


# ---------------------------------------------------------------------------
# Repositories
# ---------------------------------------------------------------------------


class ListRepositoriesInput(AzDoBaseInput):
    """Input for listing Git repositories in a project."""

    include_links: bool = Field(
        default=False,
        description="Include _links in the response.",
    )
    include_all_urls: bool = Field(
        default=False,
        description="Include all remote URLs (HTTPS and SSH).",
    )
    include_hidden: bool = Field(
        default=False,
        description="Include hidden repositories.",
    )


class GetRepositoryInput(AzDoBaseInput):
    """Input for getting a specific Git repository."""

    repository_id: str = Field(
        description="Repository ID (UUID) or repository name.",
    )


class ListBranchesInput(AzDoBaseInput):
    """Input for listing branches in a Git repository."""

    repository_id: str = Field(
        description="Repository ID (UUID) or repository name.",
    )
    filter_contains: str | None = Field(
        default=None,
        description="Filter branches by a substring (e.g., 'feature').",
    )
    top: int = Field(
        default=100,
        description="Maximum number of branches to return (max 1000).",
        ge=1,
        le=1000,
    )


class GetFileContentInput(AzDoBaseInput):
    """Input for retrieving the text content of a file from a Git repository."""

    repository_id: str = Field(
        description="Repository ID (UUID) or repository name.",
    )
    path: str = Field(
        description=(
            "Path to the file within the repository "
            "(e.g., '/src/main.py' or 'README.md')."
        ),
        min_length=1,
    )
    branch: str | None = Field(
        default=None,
        description=(
            "Branch name to read from (e.g., 'main'). "
            "Omit to read from the repository's default branch."
        ),
    )
    commit_id: str | None = Field(
        default=None,
        description=(
            "Commit SHA to read from. "
            "Takes precedence over branch when both are supplied."
        ),
    )


_VALID_RECURSION_LEVELS = {"none", "oneLevel", "full", "oneLevelPlusNestedEmptyFolders"}


class ListRepositoryItemsInput(AzDoBaseInput):
    """Input for listing items (files and folders) in a Git repository."""

    repository_id: str = Field(
        description="Repository ID (UUID) or repository name.",
    )
    scope_path: str | None = Field(
        default=None,
        description=(
            "Folder path to list (e.g., '/' for root, '/src'). "
            "Defaults to repository root."
        ),
    )
    recursion_level: str = Field(
        default="oneLevel",
        description=(
            "Traversal depth: 'none' (item only), 'oneLevel' (immediate children), "
            "'full' (all descendants), 'oneLevelPlusNestedEmptyFolders'."
        ),
    )
    branch: str | None = Field(
        default=None,
        description="Branch name to read from (e.g., 'main'). Defaults to repository default branch.",
    )
    commit_id: str | None = Field(
        default=None,
        description="Commit SHA to read from. Takes precedence over branch when both are supplied.",
    )

    @field_validator("recursion_level", mode="after")
    @classmethod
    def validate_recursion_level(cls, v: str) -> str:
        if v not in _VALID_RECURSION_LEVELS:
            raise ValueError(
                f"'recursion_level' must be one of {sorted(_VALID_RECURSION_LEVELS)}; got: {v!r}"
            )
        return v


class ListCommitsInput(AzDoBaseInput):
    """Input for listing commits in a Git repository."""

    repository_id: str = Field(
        description="Repository ID (UUID) or repository name.",
    )
    branch: str | None = Field(
        default=None,
        description="Branch name to list commits from (e.g., 'main'). Defaults to repository default branch.",
    )
    author: str | None = Field(
        default=None,
        description="Filter by author display name or email address.",
    )
    from_date: str | None = Field(
        default=None,
        description="Return commits at or after this date (ISO 8601, e.g., '2024-01-01T00:00:00Z').",
    )
    to_date: str | None = Field(
        default=None,
        description="Return commits at or before this date (ISO 8601).",
    )
    top: int = Field(
        default=100,
        ge=1,
        le=1000,
        description="Maximum number of commits to return (max 1000).",
    )


class GetCommitInput(AzDoBaseInput):
    """Input for retrieving a single commit from a Git repository."""

    repository_id: str = Field(
        description="Repository ID (UUID) or repository name.",
    )
    commit_id: str = Field(
        description="The full or abbreviated commit SHA.",
        min_length=4,
    )
    change_count: int | None = Field(
        default=None,
        ge=0,
        description="Number of file changes to include in the response (0 = none, omit for default).",
    )


# ---------------------------------------------------------------------------
# Pull Requests
# ---------------------------------------------------------------------------


class GetPullRequestInput(AzDoBaseInput):
    """Input for retrieving a single pull request by ID."""

    repository_id: str = Field(
        description="Repository ID (UUID) or repository name.",
    )
    pull_request_id: int = Field(
        description="The pull request ID.",
        ge=1,
    )
    include_commits: bool = Field(
        default=False,
        description="Include the commits associated with the pull request.",
    )
    include_work_item_refs: bool = Field(
        default=False,
        description="Include work item references associated with the pull request.",
    )


class ListPullRequestsInput(AzDoBaseInput):
    """Input for listing pull requests in a repository."""

    repository_id: str = Field(
        description="Repository ID (UUID) or repository name.",
    )
    status: str | None = Field(
        default="active",
        description=(
            "Filter by PR status: 'active', 'abandoned', 'completed', 'all'. "
            "Defaults to 'active'."
        ),
    )
    source_ref_name: str | None = Field(
        default=None,
        description="Filter by source branch (e.g., 'refs/heads/feature/my-branch').",
    )
    target_ref_name: str | None = Field(
        default=None,
        description="Filter by target branch (e.g., 'refs/heads/main').",
    )
    creator_id: str | None = Field(
        default=None,
        description="Filter by creator identity ID (UUID).",
    )
    reviewer_id: str | None = Field(
        default=None,
        description="Filter by reviewer identity ID (UUID).",
    )
    labels: list[str] | None = Field(
        default=None,
        description="Filter by label names. All specified labels must match (AND).",
    )
    title: str | None = Field(
        default=None,
        description="Filter by title substring.",
    )
    top: int = Field(
        default=100,
        description="Maximum number of pull requests to return (max 1000).",
        ge=1,
        le=1000,
    )
    skip: int | None = Field(
        default=None,
        description="Number of pull requests to skip (for pagination).",
        ge=0,
    )

    @field_validator("creator_id", mode="after")
    @classmethod
    def validate_creator_id(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return _validate_guid(v, "creator_id")

    @field_validator("reviewer_id", mode="after")
    @classmethod
    def validate_reviewer_id(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return _validate_guid(v, "reviewer_id")


class CreatePullRequestInput(AzDoBaseInput):
    """Input for creating a new pull request."""

    repository_id: str = Field(
        description="Repository ID (UUID) or repository name.",
    )
    source_ref_name: str = Field(
        description="Source branch ref name (e.g., 'refs/heads/feature/my-branch').",
    )
    target_ref_name: str = Field(
        description="Target branch ref name (e.g., 'refs/heads/main').",
    )
    title: str = Field(
        description="Title of the pull request.",
    )
    description: str | None = Field(
        default=None,
        description="Description of the pull request (up to 4000 characters).",
    )
    is_draft: bool = Field(
        default=False,
        description="Create as a draft (WIP) pull request.",
    )
    reviewers: list[str] | None = Field(
        default=None,
        description="List of reviewer identity IDs (UUIDs) to add.",
    )
    labels: list[str] | None = Field(
        default=None,
        description="List of label names to attach to the pull request.",
    )
    work_item_ids: list[int] | None = Field(
        default=None,
        description="List of work item IDs to associate with the pull request.",
    )
    delete_source_branch: bool = Field(
        default=False,
        description="Delete the source branch after the pull request is completed.",
    )
    merge_strategy: str | None = Field(
        default=None,
        description=(
            "Merge strategy on completion: 'noFastForward', 'squash', "
            "'rebase', or 'rebaseMerge'."
        ),
    )

    @field_validator("reviewers", mode="after")
    @classmethod
    def validate_reviewers(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        return _validate_guid_list(v, "reviewers")


class UpdatePullRequestInput(AzDoBaseInput):
    """Input for updating an existing pull request.

    Only supply the fields you want to change. Updatable fields: title,
    description, status, isDraft, targetRefName, autoCompleteSetBy, and
    completionOptions (deleteSourceBranch, mergeStrategy, mergeCommitMessage,
    transitionWorkItems).
    """

    repository_id: str = Field(
        description="Repository ID (UUID) or repository name.",
    )
    pull_request_id: int = Field(
        description="The pull request ID to update.",
        ge=1,
    )
    title: str | None = Field(
        default=None,
        description="New title for the pull request.",
    )
    description: str | None = Field(
        default=None,
        description="New description (up to 4000 characters).",
    )
    status: str | None = Field(
        default=None,
        description="New status: 'active', 'abandoned', or 'completed'.",
    )
    is_draft: bool | None = Field(
        default=None,
        description="Set draft status (True = draft/WIP, False = ready for review).",
    )
    target_ref_name: str | None = Field(
        default=None,
        description="Retarget the PR to a different branch (requires retargeting policy).",
    )
    auto_complete_identity_id: str | None = Field(
        default=None,
        description=(
            "Identity ID (UUID) to enable auto-complete. "
            "Set to the current user's ID to enable auto-complete."
        ),
    )
    delete_source_branch: bool | None = Field(
        default=None,
        description="Whether to delete the source branch on completion.",
    )
    merge_strategy: str | None = Field(
        default=None,
        description=(
            "Merge strategy on completion: 'noFastForward', 'squash', "
            "'rebase', or 'rebaseMerge'."
        ),
    )
    merge_commit_message: str | None = Field(
        default=None,
        description="Custom commit message for the merge commit.",
    )
    transition_work_items: bool | None = Field(
        default=None,
        description=(
            "Transition linked work items to the next logical state on completion."
        ),
    )

    @field_validator("auto_complete_identity_id", mode="after")
    @classmethod
    def validate_auto_complete_identity_id(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return _validate_guid(v, "auto_complete_identity_id")


class TagPullRequestInput(AzDoBaseInput):
    """Input for adding labels (tags) to a pull request."""

    repository_id: str = Field(
        description="Repository ID (UUID) or repository name.",
    )
    pull_request_id: int = Field(
        description="The pull request ID.",
        ge=1,
    )
    labels: list[str] = Field(
        description=(
            "One or more label names to add. Labels are created automatically "
            "if they do not already exist."
        ),
        min_length=1,
    )


class LinkWorkItemsToPullRequestInput(AzDoBaseInput):
    """Input for associating work items with a pull request."""

    repository_id: str = Field(
        description="Repository ID (UUID) or repository name.",
    )
    pull_request_id: int = Field(
        description="The pull request ID.",
        ge=1,
    )
    work_item_ids: list[int] = Field(
        description="List of work item IDs to associate with the pull request.",
        min_length=1,
    )


class ListPullRequestThreadsInput(AzDoBaseInput):
    """Input for listing all comment threads on a pull request."""

    repository_id: str = Field(
        description="Repository ID (UUID) or repository name.",
    )
    pull_request_id: int = Field(
        description="The pull request ID.",
        ge=1,
    )


class GetPullRequestThreadInput(AzDoBaseInput):
    """Input for retrieving a single comment thread on a pull request."""

    repository_id: str = Field(
        description="Repository ID (UUID) or repository name.",
    )
    pull_request_id: int = Field(
        description="The pull request ID.",
        ge=1,
    )
    thread_id: int = Field(
        description="The thread ID.",
        ge=1,
    )


_VALID_THREAD_STATUSES = {"active", "fixed", "wontFix", "closed", "byDesign", "pending"}


class CreatePullRequestThreadInput(AzDoBaseInput):
    """Input for creating a new comment thread on a pull request.

    When file_path is provided, the thread is anchored to a specific code line
    (inline comment); right_file_start_line and right_file_end_line are required
    in that case. When file_path is omitted, the thread is a general PR-level
    comment and line fields are ignored.
    """

    repository_id: str = Field(
        description="Repository ID (UUID) or repository name.",
    )
    pull_request_id: int = Field(
        description="The pull request ID.",
        ge=1,
    )
    content: str = Field(
        description="Text of the initial comment on the thread.",
        min_length=1,
    )
    status: str = Field(
        default="active",
        description=(
            "Initial thread status. Valid values: 'active', 'fixed', 'wontFix', "
            "'closed', 'byDesign', 'pending'. Defaults to 'active'."
        ),
    )
    file_path: str | None = Field(
        default=None,
        description=(
            "File path to anchor the thread to a specific code line "
            "(e.g., '/src/main.py'). Omit to create a general PR-level comment."
        ),
    )
    right_file_start_line: int | None = Field(
        default=None,
        description=(
            "Start line in the file (1-based, inclusive). "
            "Required when file_path is provided."
        ),
        ge=1,
    )
    right_file_end_line: int | None = Field(
        default=None,
        description=(
            "End line in the file (1-based, inclusive). "
            "Required when file_path is provided."
        ),
        ge=1,
    )
    right_file_start_offset: int = Field(
        default=1,
        description="Column offset of the start position (1-based). Defaults to 1.",
        ge=1,
    )
    right_file_end_offset: int = Field(
        default=1,
        description="Column offset of the end position (1-based). Defaults to 1.",
        ge=1,
    )

    @field_validator("status", mode="after")
    @classmethod
    def validate_status(cls, v: str) -> str:
        if v not in _VALID_THREAD_STATUSES:
            raise ValueError(
                f"'status' must be one of {sorted(_VALID_THREAD_STATUSES)}; got: {v!r}"
            )
        return v

    @model_validator(mode="after")
    def validate_inline_line_fields(self) -> "CreatePullRequestThreadInput":
        if self.file_path is not None:
            if self.right_file_start_line is None or self.right_file_end_line is None:
                raise ValueError(
                    "right_file_start_line and right_file_end_line are required when "
                    "file_path is set (inline comment on a code line)."
                )
        return self


class SetPullRequestThreadStatusInput(AzDoBaseInput):
    """Input for updating the status of an existing pull request comment thread."""

    repository_id: str = Field(
        description="Repository ID (UUID) or repository name.",
    )
    pull_request_id: int = Field(
        description="The pull request ID.",
        ge=1,
    )
    thread_id: int = Field(
        description="The thread ID to update.",
        ge=1,
    )
    status: str = Field(
        description=(
            "New thread status. Valid values: 'active', 'fixed', 'wontFix', "
            "'closed', 'byDesign', 'pending'."
        ),
    )

    @field_validator("status", mode="after")
    @classmethod
    def validate_status(cls, v: str) -> str:
        if v not in _VALID_THREAD_STATUSES:
            raise ValueError(
                f"'status' must be one of {sorted(_VALID_THREAD_STATUSES)}; got: {v!r}"
            )
        return v


class AddPullRequestCommentInput(AzDoBaseInput):
    """Input for adding a reply comment to an existing pull request thread."""

    repository_id: str = Field(
        description="Repository ID (UUID) or repository name.",
    )
    pull_request_id: int = Field(
        description="The pull request ID.",
        ge=1,
    )
    thread_id: int = Field(
        description="The thread ID to reply to.",
        ge=1,
    )
    content: str = Field(
        description="Text of the comment.",
        min_length=1,
    )
    parent_comment_id: int = Field(
        default=0,
        description=(
            "ID of the parent comment to reply to within the thread. "
            "Use 0 (default) for a top-level reply on the thread."
        ),
        ge=0,
    )


class UpdatePullRequestCommentInput(AzDoBaseInput):
    """Input for updating the content of an existing comment in a pull request thread."""

    repository_id: str = Field(
        description="Repository ID (UUID) or repository name.",
    )
    pull_request_id: int = Field(
        description="The pull request ID.",
        ge=1,
    )
    thread_id: int = Field(
        description="The thread ID that owns the comment.",
        ge=1,
    )
    comment_id: int = Field(
        description="The comment ID to update.",
        ge=1,
    )
    content: str = Field(
        description="The updated text for the comment.",
        min_length=1,
    )


class ListPullRequestIterationsInput(AzDoBaseInput):
    """Input for listing push iterations on a pull request."""

    repository_id: str = Field(
        description="Repository ID (UUID) or repository name.",
    )
    pull_request_id: int = Field(
        description="The pull request ID.",
        ge=1,
    )
    include_commits: bool = Field(
        default=False,
        description=(
            "When True, includes the commits associated with each iteration "
            "in the response."
        ),
    )


class GetPullRequestChangesInput(AzDoBaseInput):
    """Input for retrieving the file-change entries for a pull request iteration."""

    repository_id: str = Field(
        description="Repository ID (UUID) or repository name.",
    )
    pull_request_id: int = Field(
        description="The pull request ID.",
        ge=1,
    )
    iteration_id: int = Field(
        description=(
            "The iteration ID to retrieve changes for. "
            "Obtain from devops_list_pull_request_iterations."
        ),
        ge=1,
    )
    compare_to: int | None = Field(
        default=None,
        description=(
            "Iteration ID to diff against. When supplied, the response contains "
            "only the incremental changes between compare_to and iteration_id. "
            "When omitted, changes are relative to the PR target branch."
        ),
        ge=1,
    )
    top: int | None = Field(
        default=None,
        description="Maximum number of change entries to return (for pagination).",
        ge=1,
    )
    skip: int | None = Field(
        default=None,
        description="Number of change entries to skip (for pagination).",
        ge=0,
    )


# ---------------------------------------------------------------------------
# Work Items
# ---------------------------------------------------------------------------


class GetWorkItemInput(AzDoBaseInput):
    """Input for getting a single work item by ID."""

    work_item_id: int = Field(description="The work item ID.", ge=1)
    fields: list[str] | None = Field(
        default=None,
        description=(
            "Specific field reference names to return "
            "(e.g., ['System.Id', 'System.Title', 'System.State']). "
            "Omit to return all default fields."
        ),
    )
    expand: str | None = Field(
        default=None,
        description="Expand options: 'none', 'relations', 'fields', 'links', 'all'.",
    )


class ListWorkItemsInput(AzDoBaseInput):
    """Input for bulk-fetching work items by their IDs."""

    ids: list[int] = Field(
        description="Work item IDs to fetch (max 200 per call).",
        min_length=1,
        max_length=200,
    )
    fields: list[str] | None = Field(
        default=None,
        description="Specific field reference names to return. Omit for all default fields.",
    )
    expand: str | None = Field(
        default=None,
        description="Expand options: 'none', 'relations', 'fields', 'links', 'all'.",
    )
    error_policy: str | None = Field(
        default="omit",
        description="How to handle invalid IDs: 'fail' raises an error, 'omit' skips them silently.",
    )


class QueryWorkItemsInput(AzDoBaseInput):
    """Input for querying work items using WIQL."""

    wiql: str = Field(
        description=(
            "WIQL query string. Example: "
            "\"SELECT [System.Id], [System.Title] FROM WorkItems "
            "WHERE [System.TeamProject] = @project AND [System.State] <> 'Closed'\""
        ),
    )
    top: int | None = Field(
        default=50,
        description="Maximum number of results to return (max 200).",
        ge=1,
        le=200,
    )
    fetch_details: bool = Field(
        default=True,
        description=(
            "When True (default), automatically fetches full work item field values "
            "for all returned IDs. When False, returns only IDs and URLs."
        ),
    )
    fields: list[str] | None = Field(
        default=None,
        description="Specific field reference names to return when fetch_details=True.",
    )


class CreateWorkItemInput(AzDoBaseInput):
    """Input for creating a new work item.

    Common field values are exposed as named parameters. Use additional_fields
    for any other field reference name not listed here (e.g.,
    'Microsoft.VSTS.Common.Priority', 'System.Tags').
    """

    work_item_type: str = Field(
        description=(
            "The work item type to create (e.g., 'Bug', 'Task', 'User Story', "
            "'Feature', 'Epic')."
        ),
    )
    title: str = Field(
        description="Title of the work item (System.Title).",
    )
    description: str | None = Field(
        default=None,
        description="Description of the work item (System.Description).",
    )
    assigned_to: str | None = Field(
        default=None,
        description=(
            "Assign to a user by display name or email "
            "(e.g., 'Jane Doe' or 'jane@example.com')."
        ),
    )
    state: str | None = Field(
        default=None,
        description=(
            "Initial state (e.g., 'New', 'Active', 'To Do'). "
            "Defaults to the work item type's initial state if omitted."
        ),
    )
    area_path: str | None = Field(
        default=None,
        description="Area path (System.AreaPath), e.g., 'MyProject\\\\Team'.",
    )
    iteration_path: str | None = Field(
        default=None,
        description=(
            "Iteration path (System.IterationPath), "
            "e.g., 'MyProject\\\\Sprint 1'."
        ),
    )
    priority: int | None = Field(
        default=None,
        description="Priority (Microsoft.VSTS.Common.Priority): 1 (high) to 4 (low).",
        ge=1,
        le=4,
    )
    tags: str | None = Field(
        default=None,
        description=(
            "Semicolon-separated tags (System.Tags), "
            "e.g., 'backend; needs-review'."
        ),
    )
    parent_id: int | None = Field(
        default=None,
        description=(
            "Work item ID of the parent to set a child-parent hierarchy link."
        ),
        ge=1,
    )
    additional_fields: dict | None = Field(
        default=None,
        description=(
            "Additional field values as a dict mapping field reference name to value "
            "(e.g., {'Microsoft.VSTS.Scheduling.StoryPoints': 5, "
            "'Custom.MyField': 'value'})."
        ),
    )


class UpdateWorkItemInput(AzDoBaseInput):
    """Input for updating an existing work item.

    Only supply the fields you want to change. Common fields are exposed as
    named parameters. Use additional_fields for any other field reference name.
    """

    work_item_id: int = Field(
        description="The ID of the work item to update.",
        ge=1,
    )
    title: str | None = Field(
        default=None,
        description="New title (System.Title).",
    )
    description: str | None = Field(
        default=None,
        description="New description (System.Description).",
    )
    assigned_to: str | None = Field(
        default=None,
        description=(
            "Assign to a user by display name or email. "
            "Pass an empty string to unassign."
        ),
    )
    state: str | None = Field(
        default=None,
        description="New state (e.g., 'Active', 'Resolved', 'Closed', 'Done').",
    )
    area_path: str | None = Field(
        default=None,
        description="New area path (System.AreaPath).",
    )
    iteration_path: str | None = Field(
        default=None,
        description="New iteration path (System.IterationPath).",
    )
    priority: int | None = Field(
        default=None,
        description="New priority (Microsoft.VSTS.Common.Priority): 1 (high) to 4 (low).",
        ge=1,
        le=4,
    )
    tags: str | None = Field(
        default=None,
        description=(
            "Replace all tags with this semicolon-separated string (System.Tags). "
            "Pass an empty string to clear all tags."
        ),
    )
    comment: str | None = Field(
        default=None,
        description="Add a discussion comment (System.History).",
    )
    additional_fields: dict | None = Field(
        default=None,
        description=(
            "Additional field updates as a dict mapping field reference name to value "
            "(e.g., {'Microsoft.VSTS.Scheduling.StoryPoints': 8})."
        ),
    )


class AddWorkItemCommentInput(AzDoBaseInput):
    """Input for adding a comment to a work item."""

    work_item_id: int = Field(
        description="The ID of the work item to add a comment to.",
        ge=1,
    )
    text: str = Field(
        description="Text of the comment. Supports markdown.",
        min_length=1,
    )


class UpdateWorkItemCommentInput(AzDoBaseInput):
    """Input for updating an existing comment on a work item."""

    work_item_id: int = Field(
        description="The ID of the work item that owns the comment.",
        ge=1,
    )
    comment_id: int = Field(
        description="The ID of the comment to update.",
        ge=1,
    )
    text: str = Field(
        description="The updated text of the comment. Supports markdown.",
        min_length=1,
    )


class ListWorkItemTypesInput(AzDoBaseInput):
    """Input for listing work item types defined in an Azure DevOps project."""


class ListWorkItemFieldsInput(AzDoBaseInput):
    """Input for listing work item field definitions in an Azure DevOps project."""

    work_item_type: str | None = Field(
        default=None,
        description=(
            "Work item type name to list fields for (e.g., 'Bug', 'Task', 'User Story'). "
            "Omit to list all fields defined in the process."
        ),
    )


class CompletePullRequestInput(AzDoBaseInput):
    """Input for completing (merging) an Azure DevOps pull request."""

    repository_id: str = Field(
        description="Repository ID (UUID) or repository name.",
    )
    pull_request_id: int = Field(
        description="The pull request ID to complete.",
        ge=1,
    )
    merge_strategy: Literal["noFastForward", "squash", "rebase", "rebaseMerge"] | None = Field(
        default=None,
        description=(
            "Merge strategy to use on completion: 'noFastForward' (merge commit, preserves full "
            "history), 'squash' (collapses all commits into one — loses individual commit history), "
            "'rebase' (replays commits linearly — rewrites commit SHAs), or 'rebaseMerge' "
            "(rebase then merge commit). If omitted, the repository's default strategy is used. "
            "Confirm with the user before omitting — the default may not be what they expect."
        ),
    )
    delete_source_branch: bool | None = Field(
        default=None,
        description="Delete the source branch after the PR is completed. Confirm with the user before setting.",
    )
    merge_commit_message: str | None = Field(
        default=None,
        description="Custom commit message for the merge commit.",
    )
    transition_work_items: bool | None = Field(
        default=None,
        description="Transition linked work items to the next logical state on completion.",
    )


class AbandonPullRequestInput(AzDoBaseInput):
    """Input for abandoning an Azure DevOps pull request."""

    repository_id: str = Field(
        description="Repository ID (UUID) or repository name.",
    )
    pull_request_id: int = Field(
        description="The pull request ID to abandon.",
        ge=1,
    )


class VotePullRequestInput(AzDoBaseInput):
    """Input for casting a reviewer vote on an Azure DevOps pull request."""

    repository_id: str = Field(
        description="Repository ID (UUID) or repository name.",
    )
    pull_request_id: int = Field(
        description="The pull request ID.",
        ge=1,
    )
    reviewer_id: str = Field(
        description=(
            "Identity ID (UUID) of the reviewer casting the vote. "
            "Must be a valid GUID (format: 8-4-4-4-12 hex)."
        ),
    )
    vote: Literal[-10, -5, 0, 5, 10] = Field(
        description=(
            "Vote value: 10 = Approved, 5 = Approved with suggestions, "
            "0 = No vote (reset), -5 = Waiting for author, -10 = Rejected."
        ),
    )

    @field_validator("reviewer_id", mode="after")
    @classmethod
    def validate_reviewer_id(cls, v: str) -> str:
        return _validate_guid(v, "reviewer_id")


# ---------------------------------------------------------------------------
# Pipelines (write)
# ---------------------------------------------------------------------------


class RunPipelineInput(AzDoBaseInput):
    """Input for triggering a new run of an Azure DevOps pipeline."""

    pipeline_id: int = Field(
        description="The pipeline ID.",
        ge=1,
    )
    branch: str | None = Field(
        default=None,
        description=(
            "Branch to run against (e.g., 'main' or 'refs/heads/main'). "
            "Omit to use the pipeline's configured default branch."
        ),
    )
    template_parameters: dict | None = Field(
        default=None,
        description=(
            "Template parameter overrides as a dict mapping parameter name to value string "
            "(e.g., {'environment': 'staging'})."
        ),
    )
    variables: dict | None = Field(
        default=None,
        description=(
            "Pipeline variable overrides as a dict mapping variable name to value string "
            "(e.g., {'MY_VAR': 'my_value'}). Variables must be marked as settable at queue time."
        ),
    )


# ---------------------------------------------------------------------------
# Discovery (org-level)
# ---------------------------------------------------------------------------


class ListProjectsInput(BaseModel):
    """Input for listing projects in an Azure DevOps organization."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    organization: str | None = Field(
        default=None,
        description=(
            "Azure DevOps organization name (e.g., 'myorg'). "
            "If omitted, falls back to the AZDO_ORGANIZATION environment variable."
        ),
    )
    state_filter: str | None = Field(
        default=None,
        description=(
            "Filter by project state: 'new', 'wellFormed', 'deleting', 'createPending', 'all'. "
            "Omit to return well-formed projects only."
        ),
    )
    top: int = Field(
        default=100,
        ge=1,
        le=1000,
        description="Maximum number of projects to return (max 1000).",
    )
    continuation_token: str | None = Field(
        default=None,
        description="Pagination token from a previous response.",
    )


class ListTeamsInput(AzDoBaseInput):
    """Input for listing teams in an Azure DevOps project."""

    mine: bool = Field(
        default=False,
        description="When true, return only teams the authenticated user belongs to.",
    )
    top: int = Field(
        default=100,
        ge=1,
        le=100,
        description="Maximum number of teams to return (max 100).",
    )
    skip: int | None = Field(
        default=None,
        ge=0,
        description="Number of teams to skip (for pagination).",
    )


# ---------------------------------------------------------------------------
# Advanced Security (GHAzDo)
# ---------------------------------------------------------------------------

AdvSecAlertType = Literal["secret", "dependency", "code"]
AdvSecState = Literal["active", "dismissed", "fixed"]
AdvSecDismissalReason = Literal[
    "fixed", "acceptedRisk", "falsePositive",
    "agreedToGuidance", "toolUpgrade", "notDistributed",
]


class ListAdvancedSecurityAlertsInput(AzDoBaseInput):
    """Input for listing GitHub Advanced Security (GHAzDo) alerts for a repository."""

    repository: str = Field(
        description=(
            "Repository name or ID to list alerts for. Accepts either the repository "
            "name (e.g., 'MyRepo') or its GUID."
        ),
    )
    alert_type: AdvSecAlertType | None = Field(
        default=None,
        description=(
            "Filter by alert type: 'secret' (secret scanning), 'dependency' "
            "(dependency/SCA scanning), or 'code' (code scanning / CodeQL). "
            "Omit to return all alert types."
        ),
    )
    states: list[Literal["active", "dismissed", "fixed", "autoDismissed"]] | None = Field(
        default=None,
        description=(
            "Filter by alert state(s). Valid values: 'active', 'dismissed', 'fixed', "
            "'autoDismissed'. Omit to return alerts in all states."
        ),
    )
    severities: list[Literal["low", "medium", "high", "critical", "note", "warning", "error", "undefined"]] | None = Field(
        default=None,
        description=(
            "Filter by severity level(s). Valid values: 'low', 'medium', 'high', "
            "'critical', 'note', 'warning', 'error', 'undefined'."
        ),
    )
    rule_id: str | None = Field(
        default=None,
        description="Filter by the rule ID that generated the alert (e.g., a CodeQL query ID).",
    )
    tool_name: str | None = Field(
        default=None,
        description="Filter by the scanning tool name that reported the alert (e.g., 'CodeQL').",
    )
    ref: str | None = Field(
        default=None,
        description=(
            "Filter alerts by git ref (e.g., 'refs/heads/main'). "
            "Not applicable to secret alerts."
        ),
    )
    only_default_branch: bool | None = Field(
        default=None,
        description=(
            "When True, return only alerts on the repository's default branch. "
            "Not applicable to secret alerts. Server default is True when unset."
        ),
    )
    order_by: Literal["id", "firstSeen", "lastSeen", "fixedOn", "severity"] | None = Field(
        default=None,
        description=(
            "Sort order for results. Valid values: 'id' (default), 'firstSeen', "
            "'lastSeen', 'fixedOn', 'severity'."
        ),
    )
    top: int = Field(
        default=100,
        ge=1,
        le=1000,
        description="Maximum number of alerts to return (max 1000). Defaults to 100.",
    )
    continuation_token: str | None = Field(
        default=None,
        description="Pagination token from a previous response (x-ms-continuationtoken header).",
    )


class GetAdvancedSecurityAlertInput(AzDoBaseInput):
    """Input for retrieving a single GitHub Advanced Security alert by ID."""

    repository: str = Field(
        description="Repository name or ID. Accepts either the repository name or its GUID.",
    )
    alert_id: int = Field(
        ge=1,
        description="The numeric alert ID to retrieve.",
    )
    ref: str | None = Field(
        default=None,
        description="Git ref to scope the alert to (e.g., 'refs/heads/main').",
    )
    expand: Literal["none", "validationFingerprint"] | None = Field(
        default=None,
        description=(
            "Expand options. 'validationFingerprint' includes the raw secret value in cleartext "
            "— use with extreme caution and only when absolutely necessary. "
            "Defaults to unset (server default 'none')."
        ),
    )


class UpdateAdvancedSecurityAlertInput(AzDoBaseInput):
    """Input for updating the state of a GitHub Advanced Security alert (dismiss or re-activate)."""

    repository: str = Field(
        description="Repository name or ID. Accepts either the repository name or its GUID.",
    )
    alert_id: int = Field(
        ge=1,
        description="The numeric alert ID to update.",
    )
    state: AdvSecState = Field(
        description=(
            "New state for the alert: 'active' (re-activate a dismissed alert), "
            "'dismissed' (dismiss — requires dismissed_reason), or 'fixed' (mark resolved)."
        ),
    )
    dismissed_reason: AdvSecDismissalReason | None = Field(
        default=None,
        description=(
            "Reason for dismissal. Required when state='dismissed'. "
            "Valid values: 'fixed', 'acceptedRisk', 'falsePositive', "
            "'agreedToGuidance', 'toolUpgrade', 'notDistributed'."
        ),
    )
    dismissed_comment: str | None = Field(
        default=None,
        description="Optional comment explaining the dismissal decision.",
    )

    @model_validator(mode="after")
    def _require_reason_when_dismissing(self) -> "UpdateAdvancedSecurityAlertInput":
        if self.state == "dismissed" and self.dismissed_reason is None:
            raise ValueError(
                "dismissed_reason is required when state='dismissed' "
                "(one of: fixed, acceptedRisk, falsePositive, agreedToGuidance, "
                "toolUpgrade, notDistributed)."
            )
        return self

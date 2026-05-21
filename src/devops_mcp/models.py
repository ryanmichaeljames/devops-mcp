"""Pydantic input models for all Azure DevOps MCP tools."""

from pydantic import BaseModel, ConfigDict, Field


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

    top: int | None = Field(
        default=None,
        description="Maximum number of pipelines to return.",
        ge=1,
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
    top: int | None = Field(
        default=None,
        description="Maximum number of runs to return (client-side limit).",
        ge=1,
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


class GetRunLogContentInput(AzDoBaseInput):
    """Input for retrieving the plain-text content of a specific log."""

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
    top: int | None = Field(
        default=None,
        description="Maximum number of branches to return (max 1000).",
        ge=1,
        le=1000,
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
    top: int | None = Field(
        default=None,
        description="Maximum number of pull requests to return.",
        ge=1,
    )
    skip: int | None = Field(
        default=None,
        description="Number of pull requests to skip (for pagination).",
        ge=0,
    )


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

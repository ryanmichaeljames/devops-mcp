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

    pipeline_id: int = Field(description="The pipeline ID.", ge=1)
    run_id: int = Field(description="The run ID.", ge=1)


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

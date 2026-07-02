# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.2.0] - 2026-07-02

### Added

#### Token-efficient pipeline log retrieval

- `devops_get_run_timeline` — compact, failure-filtered build timeline surfacing inline error messages from the timeline `issues[]`; the recommended first stop for "why did this build fail," often answering with zero log fetches
- `devops_search_run_log` — grep a build log in-process and return only matching lines plus surrounding context, so non-matching log text never reaches the model

### Changed

- **BREAKING:** `devops_get_run_log_content` now returns at most `max_lines` (default 500) lines when no range is given, instead of the entire log. Added `tail` (fetch the last N lines) and a paging envelope (`total_line_count`, `start_line`, `end_line`, `returned_line_count`, `has_more`, `next_start_line`) so large logs are paged deliberately rather than flooding the model. Existing `start_line`/`end_line` slicing is unchanged; confirmed empirically against api-version 7.1 that `endLine` is inclusive and an out-of-range `start_line` returns an empty body (not an error).

## [1.1.0] - 2026-06-29

### Added

#### Pull request lifecycle tools

Registered only when `AZDO_ALLOW_WRITE=true`.

- `devops_complete_pull_request` _(write)_ — complete (merge) a pull request via the GET-then-PATCH flow; supports `merge_strategy`, `delete_source_branch`, `merge_commit_message`, and `transition_work_items`. The tool description warns that completion is irreversible and that merge settings must be confirmed first to avoid an unwanted merge type or history loss.
- `devops_abandon_pull_request` _(write)_ — abandon a pull request without merging
- `devops_vote_pull_request` _(write)_ — cast a reviewer vote (-10 reject … 10 approve)

#### Advanced Security alert tools

GitHub Advanced Security for Azure DevOps (GHAzDo) alerts, on the `advsec.dev.azure.com` host (api-version `7.2-preview.1`). Requires Advanced Security enabled on the repository.

- `devops_list_advanced_security_alerts` — list secret, dependency, and code-scanning alerts for a repository, filterable by `alert_type`, state, severity, rule, tool, and branch
- `devops_get_advanced_security_alert` — get a single alert by ID (`expand=validationFingerprint` can expose secrets in cleartext; off by default)
- `devops_update_advanced_security_alert` _(write)_ — dismiss, re-activate, or mark an alert fixed; dismissing requires a dismissal reason

#### Repository browsing

- `devops_get_file_content` — get the text content of a file; supports optional `branch` or `commit_id`; binary files return an error
- `devops_list_repository_items` — browse files and folders; control depth with `recursion_level` (`oneLevel`, `full`, etc.)
- `devops_list_commits` — list commits with optional filters for branch, author, and date range
- `devops_get_commit` — get details of a specific commit; set `change_count` to include changed file paths

#### Pipeline runs

- `devops_run_pipeline` _(write)_ — trigger a new pipeline run; optionally override branch, template parameters, or queue-time variables

#### Discovery tools

- `devops_list_projects` — list projects in an organization; use when the project name is unknown
- `devops_list_teams` — list teams in a project; supports `mine=true` to filter to the authenticated user's teams

#### Work item schema tools

- `devops_list_work_item_types` — list work item types (e.g., Bug, Task, Epic) and their reference names
- `devops_list_work_item_fields` — list field definitions for a work item type or all fields in the process

## [1.0.0] - 2026-06-26

Initial release — an MCP server exposing Azure DevOps to LLMs over stdio (FastMCP).

### Added

#### Tools (31 across 4 domains)

Tools marked _(write)_ are registered only when `AZDO_ALLOW_WRITE=true`.

**Pipelines**

- `devops_list_pipelines` — list pipelines defined in a project
- `devops_list_pipeline_runs` — list runs for a specific pipeline
- `devops_get_pipeline_run` — get details of a specific pipeline run
- `devops_get_build` — get build details by `buildId`
- `devops_list_run_logs` — list log metadata for a build
- `devops_get_run_log_content` — get plain-text log content (with `start_line`/`end_line` slicing)
- `devops_list_build_artifacts` — list artifacts produced by a build

**Repositories**

- `devops_list_repositories` — list Git repositories in a project
- `devops_get_repository` — get details of a specific repository
- `devops_list_branches` — list branches in a repository

**Pull requests**

- `devops_get_pull_request` — get details of a specific pull request
- `devops_list_pull_requests` — list pull requests with optional filters
- `devops_create_pull_request` _(write)_ — create a pull request, optionally linking work items
- `devops_update_pull_request` _(write)_ — update title, description, status, draft state, target branch, or completion options
- `devops_tag_pull_request` _(write)_ — add labels/tags to a pull request
- `devops_link_work_items_to_pull_request` _(write)_ — link work items to a pull request
- `devops_list_pull_request_threads` — list comment threads on a pull request
- `devops_get_pull_request_thread` — get a single comment thread with its comments
- `devops_create_pull_request_thread` _(write)_ — start a comment thread (general, or inline on a code line via `threadContext`)
- `devops_set_pull_request_thread_status` _(write)_ — set a thread's status
- `devops_add_pull_request_comment` _(write)_ — reply to an existing thread
- `devops_update_pull_request_comment` _(write)_ — edit an existing comment
- `devops_list_pull_request_iterations` — list a pull request's iterations (push history)
- `devops_get_pull_request_changes` — list changed files for an iteration (with optional `$compareTo`/`$top`/`$skip`)

**Work items**

- `devops_get_work_item` — get a single work item by ID
- `devops_list_work_items` — bulk-fetch up to 200 work items by ID
- `devops_query_work_items` — query work items with WIQL, auto-fetching full details
- `devops_create_work_item` _(write)_ — create a work item
- `devops_update_work_item` _(write)_ — update fields on a work item
- `devops_add_work_item_comment` _(write)_ — add a comment to a work item
- `devops_update_work_item_comment` _(write)_ — update a work item comment

#### Authentication

- **Microsoft Entra ID** credential types via `AZDO_AUTH_TYPE`: `default` (recommended), `azure_cli`, `interactive`, `client_secret`, `managed_identity`.
- **Persistent interactive token cache** (on by default; opt out with `AZDO_EPHEMERAL_TOKEN=true`) — the MSAL cache is persisted via the OS secret store (Windows DPAPI, macOS Keychain, Linux libsecret) with an `AuthenticationRecord` sidecar, so restarts authenticate silently. Falls back to in-memory cache with an actionable warning on headless hosts.
- **Token cache profiles** (`AZDO_TOKEN_CACHE_PROFILE`) — a filename-safe suffix isolating the cache and sidecar per tenant/account so multiple `interactive` instances on one host don't overwrite each other's pinned account.
- **Per-scope token lock** so concurrent cold-cache callers trigger exactly one credential acquisition, and a **configurable auth timeout** (`AZDO_AUTH_TIMEOUT_SECONDS`, default `30`).

#### Resilience

- **`request_with_retry`** — idempotency-gated retry. `429` (throttling) is retried on all methods; `502`/`503`/`504` are retried only on idempotent methods (`GET`/`PUT`/`DELETE`) so writes are never duplicated. Honours `Retry-After`; otherwise exponential back-off capped at 30 s.
- **`finalize_response`** — ~5 MB response-size cap; oversized payloads return an actionable error instead of flooding the MCP transport.
- **`paginate_results`** — continuation-token paginator that self-paginates up to the requested `top` and returns a `has_more` flag; list tools apply bounded `top` defaults.

#### Correctness & safety

- **URL-encoded request building** — organization, project, and path segments are percent-encoded, so project names with spaces produce valid URLs and raw interpolation can't inject into the path.
- **Pydantic input validation** — GUID validators on identity fields, bounded `top` defaults/limits, and PR thread status / inline line-field validation at construction time.
- **Env-driven configuration** (no hardcoded org/project/credentials/tenant) and **stderr-only logging** (stdout reserved for MCP stdio transport).
- PR-to-work-item links are created via the work-item `ArtifactLink` relation.

#### Release engineering

- **Quality gates** — `ruff` linting and `pytest` (with `pytest-asyncio`); CI runs the matrix across Python 3.10, 3.11, and 3.12 with an import smoke test.
- **PyPI publishing** — a tag-driven (`v*.*.*`) GitHub Actions workflow (gate → build → publish) using OIDC trusted publishing.

[1.2.0]: https://github.com/ryanmichaeljames/devops-mcp/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/ryanmichaeljames/devops-mcp/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/ryanmichaeljames/devops-mcp/releases/tag/v1.0.0

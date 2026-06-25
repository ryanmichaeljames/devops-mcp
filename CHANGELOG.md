# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[1.0.0]: https://github.com/ryanmichaeljames/devops-mcp/releases/tag/v1.0.0

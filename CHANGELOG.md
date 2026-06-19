# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Quality gates** — `ruff` linter and `pytest` + `pytest-asyncio` added as dev dependencies; CI workflow runs the full matrix across Python 3.10, 3.11, and 3.12.
- **Resilience: `request_with_retry`** — automatic retry with idempotency gating. `429` (throttling) is retried on all HTTP methods because a throttled request was never executed by the server. `502`/`503`/`504` (gateway/server errors) are retried only on idempotent methods (`GET`, `PUT`, `DELETE`); non-idempotent writes (`POST`, `PATCH`) are returned immediately to avoid duplicate commits. Retry delays honour the `Retry-After` response header when present; exponential back-off is used otherwise, capped at 30 s.
- **Resilience: `finalize_response`** — response-size cap (~5 MB). Payloads exceeding the cap are replaced with an actionable error message rather than flooding the MCP transport with multi-MB content.
- **Resilience: `paginate_results`** — continuation-token paginator that self-paginates `x-ms-continuationtoken` pages up to the requested `top` limit and returns a `has_more` flag.
- **Persistent interactive token cache** (`AZDO_TOKEN_CACHE_PERSIST`, default `true`) — when enabled, the MSAL token cache is persisted to disk via the OS secret store (Windows DPAPI, macOS Keychain, Linux libsecret) and an `AuthenticationRecord` sidecar is saved to `~/.devops-mcp/auth-record.json` (best-effort `0600` permissions). Subsequent server restarts authenticate silently without a new browser prompt while the refresh token remains valid. On headless platforms without an OS secret store the server logs an actionable warning and falls back to in-memory cache. Only affects `interactive` auth; the other four auth types ignore this setting.
- **Per-scope token lock** — a per-scope `asyncio.Lock` in `build_headers` ensures concurrent cold-cache callers trigger exactly one credential acquisition rather than racing to acquire tokens in parallel.
- **Configurable auth timeout** (`AZDO_AUTH_TIMEOUT_SECONDS`, default `30`) — bounds how long a cold-cache credential acquisition may block before the call is abandoned with an actionable auth error and the per-scope lock is released. Invalid or non-positive values fall back to `30` with a logged warning.
- **Pydantic GUID validators** — reviewer and identity fields on pull request and work item input models validate that supplied values are well-formed GUIDs before the request is issued.
- **Bounded `top` defaults** on list input models — `top` parameters now have explicit defaults and upper bounds so omitting `top` caps results (e.g. 100) rather than requesting an unbounded page from the API.
- **Work item comment tools** — `devops_add_work_item_comment` and `devops_update_work_item_comment` (both require `AZDO_ALLOW_WRITE=true`).

### Changed

- **PR work-item linking** — `devops_create_pull_request` now links work items via the work-item `ArtifactLink` relation (PATCH to the Work Item Tracking API). The previous approach used a `workItemRefs` body field on the PR create request, which is a no-op in the Azure DevOps REST API and never actually associated work items.
- **Bounded `top` defaults on list tools** — list tools (`devops_list_pipelines`, `devops_list_pipeline_runs`, `devops_list_pull_requests`, `devops_list_branches`, etc.) now apply a bounded default when `top` is omitted, capping results instead of passing an unbounded request to the API.
- **Self-paginating list tools** — `devops_list_pipelines` and `devops_list_branches` now self-paginate up to `top` using the `paginate_results` helper. The response envelope no longer echoes `continuation_token`; `has_more: true` indicates that additional results exist beyond the returned page.

### Fixed

- `AZDO_LOG_LEVEL` documented default corrected to `INFO` (matches `server.py`; previously documented as `DEBUG`).
- `devops_create_pull_request` now actually links work items to the pull request (see Changed above).

[Unreleased]: https://github.com/ryanmichaeljames/devops-mcp/compare/HEAD...HEAD

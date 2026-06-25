# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-06-26

Initial release — an MCP server exposing Azure DevOps to LLMs over stdio (FastMCP).

### Added

#### Tools (31 across 4 domains)

Write and delete tools are gated behind `AZDO_ALLOW_WRITE` / `AZDO_ALLOW_DELETE`.

- **Pipelines** — list pipelines, list pipeline runs, get pipeline run, get build, list run logs, get run log content (with `start_line`/`end_line` slicing), list build artifacts.
- **Repositories** — list repositories, get repository, list branches.
- **Pull requests** — get/list/create/update/tag pull requests and link work items; comment threads (`devops_list_pull_request_threads`, `devops_get_pull_request_thread`, `devops_create_pull_request_thread` — general or inline on a code line via `threadContext` — `devops_set_pull_request_thread_status`, `devops_add_pull_request_comment`, `devops_update_pull_request_comment`); diff access (`devops_list_pull_request_iterations`, `devops_get_pull_request_changes` with optional `$compareTo`/`$top`/`$skip`).
- **Work items** — get/list/query (WIQL)/create/update work items and add/update work item comments.

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

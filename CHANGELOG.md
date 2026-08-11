# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

#### Work item image attachments

- `devops_get_work_item_attachment` — download a work item attachment and return it to the model as **viewable image content**. Accepts either the attachment GUID or a full attachment URL, so an agent can paste the `<img src>` it just read out of a description. The endpoint is organization-scoped, so no project is needed even for an attachment belonging to a different one.
- `devops_upload_work_item_attachment` _(write)_ — upload an image from a local path or from base64 bytes and get back the attachment reference plus ready-to-paste `embed.markdown` and `embed.html` snippets. Uploading attaches the file to nothing; linking it into a field or onto the Attachments tab remains a separate `devops_update_work_item` call.

The download tool is the **first departure from the "every tool returns a JSON string" contract**, and it is a deliberate, bounded one. The attachment endpoint requires the Entra bearer token only this server holds, so a URL sitting in a description is a dead end for any other fetch tool — the image was simply unreachable. Base64 inside a JSON string does not fix that: it is not an image to the model, it is tens of thousands of tokens of noise. The tool therefore returns two content blocks — a JSON metadata string first, then a real image block — and is registered with `structured_output=False`, without which the SDK cannot derive an output schema for the return type and registration fails outright. Errors are still a single-element list holding a JSON error string, so the shape never varies. A non-image attachment (PDF, zip, log) is reported as metadata with `is_image: false` and its bytes are deliberately withheld; an image whose base64 expansion would breach the 5 MB response ceiling is reported as an error carrying its size and URL rather than truncated.

The image format is decided by the file's **magic bytes**, never the response `Content-Type`, and only falling back to the file extension. That header is unreliable rather than uniformly generic: measured against the live service, a PNG fetched with `?fileName=` supplied comes back as `image/png`, but the same attachment fetched by GUID alone — the shape this tool uses whenever no file name is known — comes back as the `application/octet-stream` the reference declares. A header that is only correct when the caller already knew the answer is no basis for a decision. On the upload path that same sniffing is a security control rather than a convenience: the extension allowlist is what keeps `.env`, `.pem` and key material out of an LLM's reach, and it means nothing without verifying that the bytes really are a PNG/JPEG/GIF/WEBP. A caller-supplied attachment URL gets the same treatment for the same reason — work item text is user-authored and a prompt-injection surface, so the URL is parsed for a GUID and a file name and then thrown away, with the request rebuilt against the resolved organization and every rejection landing before a token is ever minted. The new optional `AZDO_ATTACHMENT_ROOT` confines uploads to a single directory tree for operators running the server with broader credentials than the person at the keyboard.

Documented by Microsoft and relied on here: the org-scoped download route, the `AttachmentReference { id, url }` upload response, the 60 MB per-attachment service limit, and the `vso.work` / `vso.work_write` scopes. Verified against the live service, and in two places contradicting the published samples: the download is genuinely organization-scoped (an attachment uploaded into one project is read back with the organization alone); `api-version=7.2-preview.4` is accepted on GET as well as POST, and meaningfully so, since a nonsense `9.9` control returns HTTP 400; an upload answers `201`; the `download` query parameter makes no observable difference and is not sent; and the returned `url` is **project-GUID-scoped, not org-scoped**, and emits a **bare, unencoded `&`** inside its `fileName` value — `?fileName=a%20&%20b%20(1).png` for the file `a & b (1).png`. That last one breaks any `parse_qs`-style read of the query, so the file-name parser is a documented heuristic rather than a standards-compliant split, and the HTML embed snippet's entity escaping is load-bearing. Both embed snippets were confirmed to render in the product: a markdown-format large-text field renders `![alt](attachment-url)`, and an html-format field renders the equivalent `<img src>`. That was the one worth checking, since Microsoft's markdown announcement says nothing about images and the markdown dashboard widget explicitly does not support attachments. A markdown field stores the snippet byte-for-byte; an html field is normalised by the sanitiser — parens percent-encoded, quotes around `alt` dropped — but the `&amp;` entity survives, which is what makes the escaping load-bearing. **Not** established either way, and therefore not asserted: whether a pasted inline image also creates an `AttachedFile` relation, and what retention applies to an uploaded-but-unreferenced attachment. The URL parser matches on path *shape* rather than an exact string precisely because what the web UI writes into an `<img src>` is undocumented — and the project-GUID-scoped url is exactly the case that vindicates it.

#### Attachment discovery, linking, and repository images

- `devops_list_work_item_attachments` — list every attachment a work item references, from **both** of its surfaces, de-duplicated by attachment GUID. Optional `include_comments` also scans the discussion for one extra round trip.
- `devops_link_work_item_attachment` _(write)_ — add the `AttachedFile` relation that puts an uploaded attachment on the Attachments tab, the second step `devops_upload_work_item_attachment` deliberately does not perform.
- `devops_get_repository_image` — read an image committed to a Git repository (an architecture diagram, a chart, a design asset) and return it as viewable image content.

A work item holds attachments in two places that have nothing to do with each other, and that is the whole reason the listing tool exists. A file on the Attachments tab is an `AttachedFile` relation; a pasted screenshot is a URL inside a large-text field's text. Enumerating relations — the obvious implementation, and what an agent does by hand with `$expand=relations` — misses every inline image, reporting "no attachments" for a work item that visibly contains one. So both surfaces are scanned: relations, plus the large-text fields (description, acceptance criteria, repro steps, system info, history) and optionally the comments. The same file found both ways is reported once with `sources: ["relation", "inline"]`, the field names or comment ids it was found in, and the size and comment where the relation supplied them. One expression finds the URLs in both field formats, because an HTML `<img src="URL">` and a markdown `![alt](URL)` each contain the same bare URL — a second parser would only be a second thing to get wrong. Its repetitions are explicitly bounded, since it runs over attacker-authored text.

Every URL it finds is then put through the same parse-and-rebuild guard the download tool uses, and a URL that fails is **dropped and counted in `rejected_urls`, never echoed back**. That last part is the point: work item text is user-authored, so repeating a refused URL to the model would relay precisely the payload the guard had just caught. Only canonical URLs rebuilt against the resolved organization are ever surfaced, relations included — a relation is service-stored but still writable through the API, so it earns no more trust than a description does.

Linking needs that guard more than downloading does, and for a different reason: the relation URL is **persisted**. An unvalidated URL written into a relation is re-read by every future reader of the work item, so it is a durable injection vector rather than one bad request. A supplied `url` is therefore parsed for the GUID and `fileName`, discarded, and the stored relation rebuilt from the resolved organization. The write is idempotent — current relations are read first and an already-linked attachment produces `changed: false` with **no PATCH issued at all** — and carries a JSON Patch `test` op on `/rev`, so a concurrent edit fails the call instead of appending to a work item that has moved. The relation reported back is read from the server's own response rather than predicted locally, the same principle `devops_update_work_item_tags` documents; on the rare occasion the response carries no relations, `relation_read_back: false` says so instead of quietly presenting the request as the result.

`devops_get_repository_image` is a **sibling** of `devops_get_file_content`, not a change to it. Making the text reader sometimes return `list[str | Image]` would alter its contract for every text read it has ever served; a new tool costs nothing and breaks nothing. It takes the same addressing arguments, shares the attachment layer's magic-byte sniffing, byte cap and `Image(format=…)` handling, and is likewise registered `structured_output=False` — without which registration fails outright, since the SDK cannot derive an output schema for `Image`. Non-image content is refused with an error naming what was actually found and pointing back at `devops_get_file_content`, whose binary refusal now names this tool in return, so an agent that hits either dead end is told the way forward rather than left guessing.

Both tools send `$format=octetStream`, without which the Git Items route returns the item's *metadata JSON* rather than the blob — see **Fixed** below for the measurements. On the image path the symptom was an image block whose bytes begin `{"object`, with `detected_by: "file_extension"` as the only clue that magic-byte sniffing had found nothing to sniff. That is exactly the signal `detected_by` exists to give, and it is now worth reading on any file that really is an image.

The inline URL scan finds all three `<img src>` shapes the product can emit — absolute, protocol-relative `//host/…` and site-relative `/{org}/…`. The last two used to match nothing and were therefore *silently* missed: not surfaced, and not counted in `rejected_urls` either, so the caller was told the work item was clean. A relative URL is validated exactly as an absolute one is, minus the host check it has no host for; one whose first path segment names a different organization is refused and counted like any other. Finding a candidate and rejecting it is strictly better than not finding it, because only the first outcome is visible. HTML entities are decoded on each candidate too — an html-format field stores `?fileName=a%20&amp;%20b%20%281%29.png`, which was reported verbatim as the file name `a &amp; b (1).png` and re-encoded into the canonical URL as `%26amp%3B`. Only well-formed, semicolon-terminated references are decoded, not `html.unescape`, which would also decode legacy semicolon-less entities and turn the ordinary file name `a&notb.png` into `a¬b.png`.

`devops_list_work_item_attachments` always scans the **most recent comment**, with or without `include_comments`: `System.History` is one of the large-text fields and carries the newest comment's text. `include_comments` is what reaches *older* comments and supplies comment ids, which is what the extra round trip buys.

#### Work item delete

- `devops_delete_work_item` _(delete)_ — delete a work item via `DELETE {org}/{project}/_apis/wit/workitems/{id}?destroy={bool}&api-version=7.2-preview.3`, the same preview revision this module already uses for work item create/update. Registered only under `AZDO_ALLOW_DELETE=true`, and the first and only tool in the server annotated `destructiveHint: true`.

The default is the **recoverable** operation: the work item goes to the project's recycle bin and a human can restore it, fields intact, from Boards > Work Items > Recycle Bin. The unrecoverable one is opt-in per call via `destroy=true`, which erases the work item and all of its revisions from the work tracking store with no recycle bin, no undo and no support path. The two are not distinguished by tone alone — the response always carries `destroyed` (true|false) and `recoverable`, so a caller can tell which operation actually ran rather than inferring it from the request it thinks it sent, and the tests assert the `destroy` query parameter reaches the wire in **both** directions. Azure DevOps backs the distinction with separate project-level permissions — *Delete and restore work items* (Contributors by default) versus *Permanently delete work items* (Project Administrators by default) — so a 403 names the specific permission that was missing instead of a generic access-denied, and a 404 says the item may already be deleted and points at the recycle bin, which is the honest reading when a retried `DELETE` lands there.

One documentation conflict is designed around rather than trusted: the reference declares a `200 OK` carrying a `WorkItemDelete` body (`id`, `name`, `type`, `project`, `deletedBy`, `deletedDate`, plus the full deleted work item under `resource`), while the sample response on the same page is a bodiless `204`. Both are treated as success — the optional fields are surfaced when present, and their absence is never allowed to turn a completed delete into a reported failure. Existing behaviour is unchanged: with `AZDO_ALLOW_DELETE` unset the tool is not registered at all and the server has no way to delete anything.

### Fixed

- **`devops_get_file_content` returned Git item metadata instead of file content.** Every call, since the tool shipped. The `content` value was the JSON description of the item — `{"objectId":"…","gitObjectType":"blob","commitId":"…","path":"…","url":"…"}` — rather than the file, so reading a 6-byte `README.md` produced several hundred bytes of plausible-looking JSON that a model had no reason to distrust.

The cause is a default nobody documents as a trap: **the Azure DevOps Git Items route does not return a blob unless you ask for one.** `GET .../items?path=…&api-version=7.1` answers `200 OK` with `Content-Type: application/json; charset=utf-8` and the item's metadata. Measured live against a 239-byte PNG committed to a repository, only two request shapes return the file: `$format=octetStream` and an `Accept: application/octet-stream` header, both yielding `image/png` and 239 bytes. `download=true` returns the same 972 bytes of metadata; `includeContent=true` returns *more* metadata (1,779 bytes), not the file. The fix sends `$format=octetStream` — the header alternative is unavailable in practice, since this server sends `Accept: application/json` on every request.

The failure was silent by construction, which is the part worth remembering: there is no error, no suspicious status code and no header that looks wrong, just a JSON document where a file should be. A file-size figure that grows when you pass a `branch` is the tell — the *metadata* got longer, not the file.

Two consequences beyond the one-line request change. The tool's binary-file refusal was **dead code**, because the `application/octet-stream` content type it tested for never arrived; it is now live and covered by tests. And it is now byte-driven rather than header-driven, deliberately: with `$format=octetStream` Azure DevOps derives the content type from the file's extension and falls back to `application/octet-stream` for every extension it does not recognise, so refusing on that header alone would have made ordinary text files with less common extensions unreadable. Content that does not decode under the declared encoding is binary; content that does is returned, whatever the header said. `response.text` cannot make that decision, since httpx decodes with `errors="replace"` and silently turns binary into replacement characters instead of raising.

The same defect was caught in `devops_get_repository_image` before release — see above — and both tools' tests now assert `$format=octetStream` reaches the wire, against a transport stub that returns metadata JSON when it is absent, exactly as the service does.

## [1.4.0] - 2026-07-29

### Added

#### Service connection and variable group tools

Read-only, registered by default.

- `devops_list_service_connections` — list service connections (service endpoints) in a project, filtered by `type`, `names`, or `auth_schemes`. Never returns the credential bag at all. Health is reported as three independent signals: `is_ready` (provisioning finished), `is_disabled` (turned off) and `is_outdated` (stored config no longer matches the underlying resource, typically an expired or rotated secret). A connection can be ready and still fail to authenticate, so `is_ready` alone is not a health check.
- `devops_get_service_connection` — get one connection by GUID. `authorization.parameters` is projected onto an allowlist of non-secret identity fields rather than filtered by a denylist, since parameter names are endpoint-type-specific and open-ended: a denylist fails open the first time an unrecognised type appears. Withheld field _names_ are reported in `auth_parameters_dropped` so an agent can tell a credential exists without seeing it.
- `devops_list_variable_groups` — list variable groups, filtered by `group_name` (trailing `*` wildcard). Variable values are omitted by default so broad discovery cannot leak one. Pages via a continuation token.
- `devops_get_variable_group` — get one group by ID, including values. A variable marked `isSecret` has its `value` key omitted entirely rather than returned as `null`, regardless of what the server sent. Non-secret variables whose name matches a credential-like pattern are withheld too and flagged `redacted: "name_heuristic"`, a safety net for values the author forgot to mark secret.

Microsoft documents no redaction contract for either API, and testing against the live service confirmed why that matters: while Azure DevOps does null a secret variable's value and an authorization password, it returns `data.clientSecret` on a service connection in full plaintext despite the key name. Every response from these four tools is therefore projected onto an explicit allowlist rather than passed through. The refreshed-authentication endpoint is deliberately not implemented, as it mints live credentials.

### Fixed

- The server now runs on the MCP Python SDK 2.x. SDK 2.0.0 removed `mcp.server.fastmcp`, renaming the package to `mcp.server.mcpserver` and the `FastMCP` class to `MCPServer`, and the dependency was declared as an unbounded floor — so a fresh install resolved to 2.x and the server died with `ModuleNotFoundError` before serving a single request. Anyone launching via `uvx`, which ignores the lockfile, hit this immediately. The requirement is now `>=2.0.0,<3.0.0`; the upper bound is the actual lesson, since the previous constraint asserted that every future release would work, including ones not yet written. Tool names, annotations, and response wire format are unchanged.

### Removed

- `docs/design/` — the design notes described intended behavior that has since shipped, and the service connection notes proved factually wrong once checked against the live API (fields documented as requiring api-version `7.2-preview.4` are in fact returned at `7.1`). Stale design notes that contradict the service are worse than none. The rationale that is still load-bearing now lives in the module docstrings beside the code it justifies.

## [1.3.0] - 2026-07-24

### Added

- `devops_update_work_item_tags` _(write)_ — add and/or remove tags on an existing work item without replacing the whole tag set. Takes `add` and `remove` lists, matches tags case-insensitively (preserving the casing already stored), gives `remove` precedence when a tag appears in both, and skips the update entirely when the result would be unchanged so no pointless revision is created. The update carries a JSON Patch `test` op on `/rev` for optimistic concurrency, and the response reports the tags Azure DevOps actually persisted rather than a client-side prediction. Note that Azure DevOps re-sorts tags alphabetically on save, so the stored order will not match the order supplied.

### Fixed

- `devops_update_work_item` now genuinely replaces the tag set when `tags` is supplied, and clears all tags when given an empty string — as its documentation has always described. Azure DevOps treats a JSON Patch `add` op on `System.Tags` as an additive union merge with the tags already stored rather than a replacement, so the previous implementation could only ever add tags: removals and clears silently had no effect while the call still reported success. Both this tool and `devops_update_work_item_tags` now send a `replace` op, which was verified against the live API to correctly set, replace, and clear the field. `devops_create_work_item` is unaffected, since a new work item has no existing tags to merge with.

## [1.2.1] - 2026-07-14

### Fixed

- `devops_create_work_item` and `devops_update_work_item` now store large-text fields (`System.Description`, `System.History`, acceptance criteria, repro steps, system info) as **markdown** instead of HTML. Azure DevOps defaults every large-text field to HTML unless the JSON Patch document also carries a `/multilineFieldsFormat/{field}` op; the tools never sent one, so markdown text was rendered as literal characters. A new `format` input (`markdown` | `html`) allows opting into HTML explicitly. Note that Azure DevOps cannot convert a large-text field back to HTML once it has been saved as markdown.
- - `devops_add_work_item_comment` and `devops_update_work_item_comment` now store comments as **markdown** instead of HTML. The comments endpoints only honour the `format` query parameter from api-version `7.1-preview.4` onward; the tools were pinned to `7.0-preview.3` and never sent `format`, so Azure DevOps fell back to HTML and markdown text was rendered as literal characters. Both tools now target `7.1-preview.4` and send `format=markdown` by default. A new `format` input (`markdown` | `html`) allows opting into HTML explicitly.

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

[1.3.0]: https://github.com/ryanmichaeljames/devops-mcp/compare/v1.2.1...v1.3.0
[1.2.1]: https://github.com/ryanmichaeljames/devops-mcp/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/ryanmichaeljames/devops-mcp/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/ryanmichaeljames/devops-mcp/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/ryanmichaeljames/devops-mcp/releases/tag/v1.0.0

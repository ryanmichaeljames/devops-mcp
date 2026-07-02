# Design: Token-efficient pipeline log retrieval

- Status: Draft · Date: 2026-07-02 · Related: `devops_list_run_logs`, `devops_get_run_log_content` (existing), Azure DevOps Build `Timeline`, `Get Build Log`, `Get Build Logs` REST APIs (`api-version=7.1`, GA)
- API facts verified against official Microsoft Learn REST docs, and undocumented log-slice/tail/paging semantics confirmed by a live sandbox test — both 2026-07-02. See [API Verification](#api-verification).

## Summary

Deployment and build logs can run to tens of thousands of lines. Today an LLM that wants to know
"why did this build fail" has two blunt tools — list the logs, then pull a log's text — and the
cheapest path it can reason about is "pull everything," which dumps the whole log into context and
burns tokens. This design adds a **triage-first** log surface: a compact **Timeline** tool that
usually answers "what failed and why" without fetching any log text at all, a **bounded, paged**
enhancement to the existing content tool (window / tail / default page cap + a next-cursor), and a
**Python-side grep** tool that downloads a whole log inside the MCP process but returns only matching
lines. The governing insight: *anything filtered or summarized inside the Python process costs zero
LLM tokens — only the returned payload costs tokens* — so we push targeting and reduction server-side
and hand the model small, high-signal payloads.

## Context & problem

Current state (`src/devops_mcp/tools/pipelines.py`):

- `devops_list_run_logs(build_id)` → `GET /_apis/build/builds/{id}/logs` — returns per-log metadata
  (`id`, `lineCount`, `createdOn`, `url`). No line counts join to steps.
- `devops_get_run_log_content(build_id, log_id, start_line?, end_line?)` → `GET /_apis/build/builds/{id}/logs/{logId}`
  with `Accept: text/plain` and optional 1-based `startLine`/`endLine` server-side slicing. Returns
  `{build_id, log_id, content}` — no total line count, no paging cursor, no upper bound on how much
  comes back.

Two structural problems drive token waste:

1. **The model can't cheaply target.** It knows neither *which* log holds the failure nor *which
   lines* matter, so `startLine`/`endLine` (which already slices server-side and is genuinely cheap)
   goes unused — the model pulls the whole log to be safe.
2. **No bound and no cursor.** `get_run_log_content` with no range returns the entire log in one
   payload. There is no default page size and no "there's more, continue here" signal, so there is no
   safe default behavior and no clean way to iterate a little at a time and stop early.

## Goals / Non-goals

**Goals**
- Let the model discover *what failed and why* with little or no log text (Timeline + inline issues).
- Give log content a **bounded default** and a clean **line-window pagination** contract
  (next-cursor + total + has_more) so the model can iterate or stop early.
- Add **tail** (last N lines — most errors are at the end) and **Python-side grep** (matches only).
- Stay backward-compatible: don't change existing tool semantics; prefer additive optional params and
  new tools. Follow all repo conventions (`{Action}{Resource}Input`, `devops_{verb}_{noun}`, JSON-only
  returns via `finalize_response`, truthful annotations, error ladder, env-driven config).

**Non-goals**
- No new auth, host, or transport plumbing — all endpoints are on `dev.azure.com` `build` area at
  `api-version=7.1`, already reachable via `build_url`/`build_params`.
- No caching/persistence of log text across calls.
- No release-pipeline (`vsrm`) logs — build/pipeline runs only, matching the current surface.

## Confirmed API facts (all `api-version=7.1` GA, `build` area, existing host)

The repo already pins `api-version=7.1` for the whole `build` area (`client.API_VERSION = "7.1"`,
injected by `build_params`). All three endpoints below are **GA at 7.1** — no preview moniker is
needed, and nothing here requires a different version.

- **Timeline** (`Timeline - Get`): `GET /_apis/build/builds/{buildId}/timeline?api-version=7.1` →
  a `Timeline` object `{id, changeId, lastChangedBy, lastChangedOn, records: TimelineRecord[], url}`
  (**not** a `{count, value}` envelope — read `data["records"]`). Each `TimelineRecord` carries,
  among others: `id` (uuid), `parentId` (uuid), `order` (int), `type` (**opaque string** — e.g.
  `Stage`/`Phase`/`Job`/`Task`; not an enum, varies classic vs YAML), `name`,
  `state` (`pending|inProgress|completed`),
  `result` (`succeeded|succeededWithIssues|failed|canceled|skipped|abandoned`, nullable while running),
  `errorCount`, `warningCount`, `startTime`, `finishTime`, `percentComplete`,
  `log` (`BuildLogReference` = `{id, type, url}`, **null until the step has produced a log**), and —
  critically — `issues: Issue[]` where each `Issue` is `{type: error|warning, category, message, data}`.
  (Records also expose `attempt`, `previousAttempts`, `identifier`, `resultCode`, `currentOperation`,
  `lastModified`, `queueId`, `workerName`, `task`, `details`, `_links` — all stripped by our projection.)
  **The `issues[].message` field frequently contains the actual failure text inline** (task errors,
  "##[error] …" summaries), so the failure can often be surfaced with *zero* log fetches.
  Optional params (verified, **not used** by this design): path segment `/{timelineId}` (uuid) selects
  a specific timeline — omitting it returns the build's default/latest timeline, which is what we want;
  query `changeId` (int) and `planId` (uuid) scope to an attempt/plan. **There is no `$expand`
  parameter** on this endpoint.
- **Get Build Log** (`Builds - Get Build Log`):
  `GET /_apis/build/builds/{buildId}/logs/{logId}?startLine={n}&endLine={n}&api-version=7.1`.
  `startLine`/`endLine` are **optional `int64` query params** sliced **server-side**. Response media
  types (per docs): `text/plain` (compact, current choice), `application/json`, and `application/zip`.
  We keep `text/plain` and split on `\n` in Python where we need line arrays.
  ✅ **Verified (live, 7.1) — not MS-documented:** the range is a **closed interval `[startLine, endLine]`**
  (1-based, both inclusive), and an out-of-range `startLine` returns **HTTP 200 with an empty body**
  (not an error). The official docs specify none of this — see [API Verification](#api-verification).
  Implementations should still clamp defensively (treat an empty/short body as "no more lines").
- **List logs** (`Builds - Get Build Logs`): `GET /_apis/build/builds/{buildId}/logs?api-version=7.1`
  → `BuildLog[]`, each `{id, type, url, createdOn, lastChangedOn, lineCount}` where **`lineCount`
  (`int64`) is the number of lines in that log**. This is the **only** place a total line count is
  available — the Timeline record does *not* carry a line count. Media types are `application/json`
  / `application/zip` (no `text/plain`); the existing `devops_list_run_logs` reads `data["value"]`.
  Joining `lineCount` onto timeline records / content responses server-side is a free (token-wise)
  convenience.

## API Verification

Verified 2026-07-02 against the official Azure DevOps REST reference (`view=azure-devops-rest-7.1`).
All three operations are **GA at 7.1**, consistent with the repo's `API_VERSION = "7.1"`.

| Fact | Verified? | Source |
|---|---|---|
| Timeline path `/_apis/build/builds/{buildId}/timeline` (optional `/{timelineId}`; optional `changeId`, `planId`; **no `$expand`**) at 7.1 | ✅ Confirmed | Timeline - Get |
| `Timeline` returns `{…, records: TimelineRecord[]}` (not `{count,value}`) | ✅ Confirmed | Timeline - Get |
| Record fields `id, parentId, order, type, name, state, result, errorCount, warningCount, startTime, finishTime, percentComplete, log, issues` all exist | ✅ Confirmed | Timeline - Get |
| `type` is a free-form `string` (no enum) | ✅ Confirmed | Timeline - Get |
| `result` = `TaskResult` enum `succeeded\|succeededWithIssues\|failed\|canceled\|skipped\|abandoned` | ✅ Confirmed | Timeline - Get |
| `state` = `TimelineRecordState` enum `pending\|inProgress\|completed` | ✅ Confirmed | Timeline - Get |
| `log` = `BuildLogReference` `{id, type, url}` | ✅ Confirmed | Timeline - Get |
| `issues[]` = `Issue` `{category, data, message, type}`; `type` = `IssueType` enum `error\|warning`; **`message` carries the failure text** | ✅ Confirmed | Timeline - Get |
| Get Build Log path `/_apis/build/builds/{buildId}/logs/{logId}`; `startLine`/`endLine` optional `int64` query params | ✅ Confirmed | Get Build Log |
| Get Build Log media types `text/plain`, `application/json`, `application/zip` | ✅ Confirmed | Get Build Log |
| `startLine`/`endLine` are **1-based, closed range `[startLine, endLine]`** (both inclusive) | ✅ **Verified (live, 7.1)** — undocumented by MS but confirmed: `startLine=1&endLine=10` → exactly 10 lines; `startLine=100&endLine=120` → 21 lines (=120−100+1) | Live test (build 6) |
| Out-of-range `startLine` (> `lineCount`) returns **HTTP 200 with an empty body** (not an error) | ✅ **Verified (live, 7.1)** — tool clamps to `returned_line_count=0, has_more=false` | Live test |
| `tail=N` returns the true last N lines (`start_line = total_line_count − N + 1`) | ✅ **Verified (live, 7.1)** — 615-line log, `tail=20` → lines 596–615 | Live test |
| List logs path `/_apis/build/builds/{buildId}/logs` → `BuildLog[]` with `lineCount` (`int64`) per log | ✅ Confirmed | Get Build Logs |

Sources (Microsoft Learn, `view=azure-devops-rest-7.1`):

- Timeline - Get — <https://learn.microsoft.com/en-us/rest/api/azure/devops/build/timeline/get?view=azure-devops-rest-7.1>
- Builds - Get Build Log — <https://learn.microsoft.com/en-us/rest/api/azure/devops/build/builds/get-build-log?view=azure-devops-rest-7.1>
- Builds - Get Build Logs — <https://learn.microsoft.com/en-us/rest/api/azure/devops/build/builds/get-build-logs?view=azure-devops-rest-7.1>

**Correction vs the prior draft, now closed by live test:** the "1-based inclusive, sliced
server-side" claim for `startLine`/`endLine` is **not** in the official docs. It was flagged as an
empirical assumption, then **confirmed by a live test against a real sandbox build (`api-version=7.1`)**
on 2026-07-02: the range is a **closed interval `[startLine, endLine]`** (1-based, both inclusive), an
out-of-range `startLine` returns **HTTP 200 with an empty body** (not an error), and `tail=N` computed
as `start_line = total_line_count − N + 1` returns the true last N lines. The paging envelope was
verified end-to-end (no-range → bounded to `max_lines=500`, `has_more=true`, `next_start_line=501`;
near-end windows clamp with `has_more=false`), as were `devops_search_run_log` (`match_count` counts
all matches, `matches[]` caps at `max_matches` with `truncated=true`) and `devops_get_run_timeline`
(`failed_only=true` → `records:[]` on a green build; `failed_only=false` → full records with
`result`/`log_id`/`log_line_count`). These remain **empirically** confirmed (not MS-documented), so
implementations should still clamp defensively. Everything else in the prior draft verified as written.

## Proposed design

Three moves, ranked by token win. Round-trips also cost tokens (each tool call + its result framing),
so the design favors one high-signal call over many thin ones.

### 1. New tool — `devops_get_run_timeline` (the triage entry point; biggest win)

Returns a **compact, filtered** projection of the timeline. Strips heavy/low-signal fields
(`_links`, `url`, `workerName`, `queueId`, `task` UUIDs, `previousAttempts`, `details`), keeps only
what drives a decision, and surfaces `issues[].message` inline. By default it filters to failing
records so the common payload is a handful of records, not the whole tree.

Input model `GetRunTimelineInput(AzDoBaseInput)`:

| Field | Type | Default | Notes |
|---|---|---|---|
| `build_id` | `int ge=1` | — | run_id == build_id |
| `failed_only` | `bool` | `true` | keep only records whose `result` ∈ {`failed`,`canceled`,`succeededWithIssues`} or `errorCount>0`; `false` returns all records |
| `include_issues` | `bool` | `true` | include the inline `issues[]` (error/warning messages) |
| `include_log_line_counts` | `bool` | `true` | one extra call to `/logs`; joins `lineCount` onto each record's `log` so the model can size a tail window |
| `record_types` | `list[str] \| None` | `None` | optional case-insensitive filter on `type` (e.g. `["Task"]`); omit to keep all |

Response shape:

```json
{
  "build_id": 12345,
  "overall_result": "failed",
  "counts": { "records": 42, "returned": 3, "errors": 2, "warnings": 5 },
  "records": [
    {
      "id": "…uuid…", "parent_id": "…uuid…", "order": 7,
      "type": "Task", "name": "Deploy to prod",
      "state": "completed", "result": "failed",
      "error_count": 1, "warning_count": 0,
      "start_time": "…", "finish_time": "…", "duration_seconds": 12.4,
      "log_id": 23, "log_line_count": 5120,
      "issues": [ { "type": "error", "message": "##[error] az deployment failed: …" } ]
    }
  ],
  "has_more": false
}
```

Notes: `has_more` is `false` unless `failed_only` truncation is ever added (not in phase 1 — the
filtered set is small). `log_id`/`log_line_count` are `null` when the step produced no log yet.
Fetch the timeline via `request_with_retry`; project/filter with plain Python; return via
`finalize_response`. Annotations: read-only/idempotent, `openWorldHint: true`.

### 2. Enhance `devops_get_run_log_content` (bounded paging + tail; backward-compatible)

Add optional params and a paging metadata envelope. Existing calls (`start_line`/`end_line` only, or
neither) keep working — but the **default now bounds output** instead of returning the whole log.

Added fields on `GetRunLogContentInput`:

| Field | Type | Default | Notes |
|---|---|---|---|
| `max_lines` | `int \| None` `ge=1 le=5000` | `500` | page size cap. With `start_line` set and no `end_line`, returns `start_line … start_line+max_lines-1`. With neither set, returns lines `1 … max_lines` (head). Ignored when both `start_line` and `end_line` are given. |
| `tail` | `int \| None` `ge=1 le=5000` | `None` | return the **last** N lines. Mutually exclusive with `start_line`/`end_line` (Pydantic validator → `ValueError`). Implemented by fetching `lineCount` from `/logs`, then requesting `startLine = max(1, lineCount-N+1)` (1-based `startLine` — verified live, see [API Verification](#api-verification); e.g. 615-line log, `tail=20` → lines 596–615). |

Response shape (superset — `content` preserved for compatibility):

```json
{
  "build_id": 12345, "log_id": 23,
  "total_line_count": 5120,
  "start_line": 1, "end_line": 500, "returned_line_count": 500,
  "has_more": true, "next_start_line": 501,
  "content": "…text…"
}
```

`has_more` = more lines exist **after** `end_line`; `next_start_line` = `end_line+1` (else `null`).
`total_line_count` comes from a `/logs` lookup; if that call fails, degrade gracefully
(`total_line_count: null`, `has_more` inferred as `returned_line_count == max_lines`). This gives the
model an explicit "iterate or stop" loop and a cheap head/tail without a separate tool.

### 3. New tool — `devops_search_run_log` (Python-side grep; matches only)

Downloads the full log **inside the MCP process** and returns only matching lines plus a little
context. Non-matching lines never reach the model, so a 5000-line log can cost a few dozen lines of
tokens.

Input model `SearchRunLogInput(AzDoBaseInput)`:

| Field | Type | Default | Notes |
|---|---|---|---|
| `build_id` | `int ge=1` | — | |
| `log_id` | `int ge=1 \| None` | `None` | omit to search **all** logs in the build (phase 3b; bounded by `max_matches`) — phase 3a requires `log_id` |
| `pattern` | `str` (min 1, max ~200) | — | search string |
| `is_regex` | `bool` | `false` | literal substring by default (safer, faster); regex when `true` |
| `ignore_case` | `bool` | `false` | |
| `context` | `int ge=0 le=10` | `2` | lines before/after each match |
| `max_matches` | `int ge=1 le=200` | `50` | cap payload; sets `truncated` |

Response shape:

```json
{
  "build_id": 12345, "log_id": 23,
  "total_line_count": 5120, "match_count": 4, "truncated": false,
  "matches": [
    { "line_number": 4187, "line": "##[error] deploy failed",
      "context_before": ["…", "…"], "context_after": ["…", "…"] }
  ]
}
```

Fetch full text with `text/plain`, split on `\n`, match in Python. Bound `pattern` length and default
to literal substring to limit regex-ReDoS exposure (`re` has no timeout in CPython).

### Explicitly rejected / deferred

- **Standalone "summary" tool (lineCount + head N + tail N + step results).** Rejected — it is exactly
  what `devops_get_run_timeline` (step results + issues + log ids + line counts) plus content-tool
  head/tail already deliver, and a fixed head+tail risks returning the half the model doesn't need.
  The timeline tool *is* the summary.
- **`$format=zip` download.** Rejected for now — zip reduces *network* transfer, not *LLM tokens*
  (we still slice to text), and adds binary/unzip complexity. Only reconsider as a micro-opt for
  whole-build grep (fetch all logs in one zip) in phase 3b.
- **`application/json` line arrays.** Rejected — `{count, value:[…]}` adds per-line JSON quoting
  overhead vs `text/plain`; splitting text on `\n` in Python is cheaper and exact for the returned
  slice.
- **Timeline attachments / plan API.** Out of scope — attachments carry test/artifact data, not
  general error text; no log-token benefit.

## Alternatives considered

- **One mega-tool with a `mode` param (window|tail|grep|timeline).** Rejected — parameter overload
  and muddy tool-selection for the LLM. Distinct mental models (navigate vs read-window vs search)
  map better to distinct tools with focused descriptions.
- **Only enhance the existing content tool, no Timeline tool.** Rejected — leaves the *targeting*
  problem unsolved; windowing a log you can't locate still means pulling everything. Timeline is the
  lever that removes most fetches entirely.
- **Client-side (LLM-driven) pagination via existing `start_line`/`end_line` only.** Insufficient —
  works mechanically but gives the model no total, no cursor, and no default bound, so it keeps
  over-pulling. The paging envelope is the fix.

## Affected areas & work split

All work is Python (`developer`); no CI/IaC, so `devops-engineer` is not engaged. Both touched files
(`models.py`, `tools/pipelines.py`) are **shared**, so parallelizing risks merge conflicts — prefer a
single developer working the phases in order. If parallelized, split strictly by phase and land
Phase 1 first (it is self-contained), then Phase 2/3 on top:

- **Phase 1 — Timeline (biggest win, ship first).** `developer`: add `GetRunTimelineInput` to
  `models.py`; add `devops_get_run_timeline` to `pipelines.py` (fetch timeline, optional `/logs`
  join, Python projection/filter). Disjoint from existing tools.
- **Phase 2 — Content paging + tail.** `developer`: extend `GetRunLogContentInput` (`max_lines`,
  `tail`, mutual-exclusion validator); rework `devops_get_run_log_content` to compute the window,
  join `total_line_count`, and emit the paging envelope. Backward-compatible.
- **Phase 3 — Grep.** `developer`: add `SearchRunLogInput`; add `devops_search_run_log` (3a: single
  `log_id`; 3b optional: whole-build search + optional zip transfer opt).
- **Docs**: update `README` tool count/list and `CLAUDE.md` tool inventory when each phase lands.

## Risks & open questions

- **Extra API call for line counts / totals.** Phase 1 `include_log_line_counts` and Phase 2
  `total_line_count`/`tail` each add a `/logs` call. Make it toggleable (P1) and degrade gracefully
  on failure (P2). Acceptable: `/logs` is small.
- **Undocumented log-slice semantics — resolved (empirically).** `startLine`/`endLine` base index,
  inclusivity, and out-of-range behavior are **not documented** by Microsoft, but were **confirmed by
  live test (2026-07-02, `api-version=7.1`)**: closed range `[startLine, endLine]`, 1-based inclusive;
  out-of-range `startLine` → HTTP 200 empty body (see [API Verification](#api-verification)). Residual
  risk is only that MS could change undocumented behavior — keep the defensive clamp (empty/short body
  = "no more lines," not an error).
- **In-progress builds.** `log` is `null` and `lineCount` absent until a step emits a log; timeline
  records may be `pending`/`inProgress`. Handle null `log_id`/`log_line_count` and don't assume a log
  exists.
- **Classic vs YAML timelines** differ in record `type` values and nesting — treat `type` as an
  opaque string, filter primarily by `result`/`errorCount`, never hardcode a type taxonomy.
- **Regex ReDoS** from LLM-supplied `pattern` — CPython `re` has no timeout. Mitigate with literal
  default (`is_regex=false`), bounded pattern length, and bounded `context`/`max_matches`.
- **5 MB `finalize_response` cap** — grep with large `max_matches`×`context`, or a very wide window,
  could still be large; the bounds above keep payloads well under the cap.
- **Default-bound behavior change (Phase 2).** Making `max_lines` default to 500 means a
  no-arguments call no longer returns the whole log. This is intentional and the token fix, but it is
  a behavioral change to an existing tool — document it clearly and make `has_more`/`next_start_line`
  obvious so callers relying on "get everything" adapt to iterating.
- **Open question:** should whole-build grep (3b) be built at all, or is per-log search + timeline
  targeting enough in practice? Defer until phase-1/2 usage shows whether the model still struggles to
  pick a log.
- **Open question:** default `max_lines` value — 500 balances signal vs tokens; confirm against real
  deployment logs during phase 2.

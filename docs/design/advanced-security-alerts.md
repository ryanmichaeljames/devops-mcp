# Design: Advanced Security Alert tools

- Status: Draft · Date: 2026-06-29 · Related: GitHub Advanced Security for Azure DevOps (GHAzDo) Alerts REST API (`advancedsecurity` area, `7.2-preview.1`)

## Summary

Add a set of MCP tools that let an LLM read GitHub Advanced Security (GHAzDo) alerts for a
repository — secret-scanning, dependency-scanning, and code-scanning — and change an alert's state
(dismiss / re-activate). The alerts live behind a **different host** (`advsec.dev.azure.com`) than
the rest of the server's tools (`dev.azure.com`), which is the central design constraint. We add a
thin sibling URL builder, one read `list` tool (filtered by `alert_type`), one read `get` tool, and
one write `update` tool, all in a new module.

## Context & problem

The server today targets only `https://dev.azure.com/{org}/{project}/_apis/...` — `build_url()` in
`client.py` hardcodes that host. Advanced Security alerts are served from
`https://advsec.dev.azure.com/{org}/{project}/_apis/alert/...`. There is currently no way to reach a
non-`dev.azure.com` host, and the shared `build_params()` helper hardcodes `api-version` to the
module-level `API_VERSION = "7.1"`, whereas every advsec operation is `7.2-preview.1`. We need a
minimal, convention-consistent way to add this surface without disturbing existing tools.

## Goals / Non-goals

**Goals**
- Read alerts for a repo, filterable to secret / dependency / code (and other criteria).
- Get a single alert by ID.
- Change an alert's state (dismiss with a reason + comment; re-activate).
- Stay within existing patterns: `AzDoBaseInput`, `@mcp.tool` / `@write_tool`, error ladder, JSON-only returns, env-driven config.

**Non-goals**
- No enable/disable of Advanced Security on a repo, no analysis-upload (SARIF) tools.
- No `validationFingerprint` expansion by default (it can return secrets in cleartext — see Security).
- No new auth-scope plumbing (the existing Entra `.default` token already covers advsec — see Security).

## Confirmed API facts (all `api-version=7.2-preview.1`)

Host for **all** operations: `https://advsec.dev.azure.com/{organization}/{project}/_apis/alert/...`
Base route segment after `_apis`: `alert/repositories/{repository}`.

| Operation | Method | Path (after host + `/_apis/`) | Scope |
| --- | --- | --- | --- |
| List | `GET` | `alert/repositories/{repository}/alerts` | `vso.advsec` (read) |
| Get | `GET` | `alert/repositories/{repository}/alerts/{alertId}` | `vso.advsec` (read) |
| Update | `PATCH` | `alert/repositories/{repository}/alerts/{alertId}` | `vso.advsec_write` (write) |
| Batch (not exposed) | `POST` | `alert/repositories/{repository}/AlertsBatch` | `vso.advsec` (read) |

Cite: Get / List / Update / Alerts-Batch List reference pages (URLs in Sources).

**Enums — verbatim from docs:**
- `AlertType`: `unknown`, `dependency`, `secret`, `code`.
- `State`: `unknown`, `active`, `dismissed`, `fixed`, `autoDismissed`.
- `DismissalType`: `unknown`, `fixed`, `acceptedRisk`, `falsePositive`, `agreedToGuidance`, `toolUpgrade`, `notDistributed`.
- `Severity`: `low`, `medium`, `high`, `critical`, `note`, `warning`, `error`, `undefined`.
- `Confidence`: `high`, `other`.

**List query params** (all optional, all prefixed `criteria.` except `top`/`orderBy`/`expand`/`continuationToken`):
`criteria.alertType` (single `AlertType`), `criteria.alertIds` (int[]), `criteria.severities` (string[]),
`criteria.states` (string[]), `criteria.confidenceLevels` (string[], secrets only),
`criteria.ruleId`, `criteria.ruleName`, `criteria.toolName`, `criteria.dependencyName` (not secrets),
`criteria.licenseName` (not secrets), `criteria.keywords`, `criteria.ref` (not secrets),
`criteria.onlyDefaultBranch` (bool, not secrets), `criteria.pipelineId`/`pipelineName`/`phaseId`/`phaseName`,
`criteria.fromDate`/`toDate`/`modifiedSince`, `criteria.isTriaged`, `criteria.hasLinkedWorkItems`,
`criteria.validity` (string[], secrets only). Plus `top` (int), `orderBy` (`id`|`firstSeen`|`lastSeen`|`fixedOn`|`severity`, default `id`), `expand` (`none`|`minimal`|`count`), `continuationToken`.

Paging uses the same `x-ms-continuationtoken` header family as the rest of the server (the
`paginate_results` helper already handles it).

**Update request body** (`AlertStateUpdate`): `state` (`State`), `dismissedReason` (`DismissalType`),
`dismissedComment` (string). Returns the updated `Alert`.

**Get** extra params: `ref` (string), `expand` (`none` | `validationFingerprint`).

## Proposed design

### 1. URL-host decision — add a sibling `build_advsec_url(...)`

**Decision:** add a new helper in `client.py` rather than parameterise `build_url`.

Proposed signature, placed next to `build_url` (`client.py`, after the existing `build_url` block
around line 203):

```python
def build_advsec_url(organization: str, project: str, path: str) -> str:
    """Build a percent-encoded Advanced Security REST URL on advsec.dev.azure.com.

    Same encoding contract as build_url (org/project encoded with safe="",
    multi-segment path with safe="/"), but targets the advsec host that serves
    the GHAzDo alerts API instead of dev.azure.com.
    """
    enc_org = quote(organization, safe="")
    enc_project = quote(project, safe="")
    enc_path = quote(path, safe="/")
    return f"https://advsec.dev.azure.com/{enc_org}/{enc_project}/_apis/{enc_path}"
```

**Rationale:** `build_url` is called by ~30 existing tools; adding an optional `host`/`base_url`
param widens a hot signature and invites mis-set defaults. A sibling is a 6-line addition that
mirrors the existing `build_org_url` precedent (already a host-variant sibling), keeps each builder's
host a compile-time constant, and leaves all current call sites untouched. This matches the brain
note [[Azure DevOps REST API]] guidance that ADO is *service-fragmented across hosts* — a per-family
builder is the idiomatic shape here.

**api-version:** do **not** use the shared `build_params()` (it pins `7.1`). Follow the work-items
module precedent: define a module constant `_ADVSEC_API_VERSION = "7.2-preview.1"` in the new tools
file and pass `params={"api-version": _ADVSEC_API_VERSION, ...}` (filtering `None`s with a small
local helper or inline dict comprehension).

### 2. Tools — one filtered `list`, one `get`, one `update`

**Decision:** a single `list` tool with an `alert_type` filter, **not** three type-specific tools.

The API itself models type as one optional `criteria.alertType` value over a single endpoint; three
tools would be three near-identical wrappers differing only by a hardcoded filter, multiplying the
tool surface (the server already exposes 32+ tools) for no capability gain. One tool with an
`Optional[Literal["secret","dependency","code"]]` filter covers "secrets vs deps vs code" and also
supports the unfiltered "all alerts" case the three-tool split cannot express in one call.

| Tool name | Gate | Method + URL (host `advsec.dev.azure.com`) + api-version | Route/query | Body | Annotations |
| --- | --- | --- | --- | --- | --- |
| `devops_list_advanced_security_alerts` | default (`@mcp.tool`) | `GET .../_apis/alert/repositories/{repository}/alerts?api-version=7.2-preview.1` | route: `repository`; query: `criteria.alertType`, `criteria.states`, `criteria.severities`, `criteria.ruleId`, `criteria.toolName`, `criteria.onlyDefaultBranch`, `criteria.ref`, `top`, `orderBy`, `continuationToken` | none | readOnly=True, destructive=False, idempotent=True, openWorld=True, title "List Advanced Security Alerts" |
| `devops_get_advanced_security_alert` | default (`@mcp.tool`) | `GET .../alerts/{alertId}?api-version=7.2-preview.1` | route: `repository`, `alertId`; query: `ref`, `expand` | none | readOnly=True, destructive=False, idempotent=True, openWorld=True, title "Get Advanced Security Alert" |
| `devops_update_advanced_security_alert` | write (`@write_tool`, `AZDO_ALLOW_WRITE=true`) | `PATCH .../alerts/{alertId}?api-version=7.2-preview.1` | route: `repository`, `alertId` | `{ "state": ..., "dismissedReason": ..., "dismissedComment": ... }` (only set keys) | readOnly=False, destructive=False, idempotent=True, openWorld=True, title "Update Advanced Security Alert" |

Notes:
- The update is **idempotent** (re-applying the same state is a no-op) and **not destructive** (it
  changes triage state, not the underlying finding) — same annotation shape as
  `devops_update_work_item`.
- Expose a curated subset of the ~24 `criteria.*` filters on the list tool (the common ones above);
  the rest are reachable later if needed. Keep the model lean.
- `AlertsBatch` (POST) is **not exposed**: it fetches alerts by explicit ID list and *currently
  supports secret alerts only* (per docs), which `list` (via `criteria.alertIds`) and `get` already
  cover for the LLM use case. Documented here so the developer doesn't add it speculatively.

### 3. Module placement

**Decision:** new module `src/devops_mcp/tools/advanced_security.py`. It is a distinct API area
(different host, different api-version, own scope) and folding it into `repositories.py` would mix
hosts within one file. Register it by adding one import line to `server.py` (alongside the existing
tool-module imports, ~line 22):

```python
import devops_mcp.tools.advanced_security  # noqa: E402, F401
```

### 4. Pydantic models (add to `models.py`, new "Advanced Security" section)

All inherit `AzDoBaseInput` (gives `organization`/`project`, `extra="forbid"`). `repository` is a
plain `str` (name **or** ID per docs — do **not** GUID-validate it, mirroring `repository_id` on the
repo tools, which also accepts a name).

```python
# Module-level literals reused across models
AdvSecAlertType = Literal["secret", "dependency", "code"]
AdvSecState     = Literal["active", "dismissed", "fixed"]   # writable subset
AdvSecDismissalReason = Literal[
    "fixed", "acceptedRisk", "falsePositive",
    "agreedToGuidance", "toolUpgrade", "notDistributed",
]

class ListAdvancedSecurityAlertsInput(AzDoBaseInput):
    repository: str                      # required — name or ID
    alert_type: AdvSecAlertType | None = None         # -> criteria.alertType
    states: list[Literal["active","dismissed","fixed","autoDismissed"]] | None = None  # -> criteria.states
    severities: list[Literal["low","medium","high","critical","note","warning","error","undefined"]] | None = None  # -> criteria.severities
    rule_id: str | None = None           # -> criteria.ruleId
    tool_name: str | None = None         # -> criteria.toolName
    ref: str | None = None               # -> criteria.ref (not applicable to secret alerts)
    only_default_branch: bool | None = None  # -> criteria.onlyDefaultBranch
    order_by: Literal["id","firstSeen","lastSeen","fixedOn","severity"] | None = None
    top: int = Field(default=100, ge=1, le=1000)
    continuation_token: str | None = None

class GetAdvancedSecurityAlertInput(AzDoBaseInput):
    repository: str
    alert_id: int = Field(ge=1)          # -> path {alertId}
    ref: str | None = None
    expand: Literal["none","validationFingerprint"] | None = None  # default None == server default "none"

class UpdateAdvancedSecurityAlertInput(AzDoBaseInput):
    repository: str
    alert_id: int = Field(ge=1)
    state: AdvSecState = Field(...)      # required: active | dismissed | fixed
    dismissed_reason: AdvSecDismissalReason | None = None   # -> dismissedReason
    dismissed_comment: str | None = None                    # -> dismissedComment

    @model_validator(mode="after")
    def _require_reason_when_dismissing(self):
        if self.state == "dismissed" and self.dismissed_reason is None:
            raise ValueError(
                "dismissed_reason is required when state='dismissed' "
                "(one of: fixed, acceptedRisk, falsePositive, agreedToGuidance, "
                "toolUpgrade, notDistributed)."
            )
        return self
```

(Every field annotated with `Field(...)` descriptions per repo convention — abbreviated here.) The
`State` enum's `unknown`/`autoDismissed` are service-computed, so the *writable* `state` Literal is
narrowed to `active | dismissed | fixed`; `states` filter on the list tool keeps the full set since
you may want to *read* `autoDismissed` alerts.

The update tool builds the body from only the set keys (omit `None`), exactly like
`devops_update_work_item`/`devops_update_pull_request` build partial payloads.

## Alternatives considered

- **Parameterise `build_url(host=...)`** — rejected: widens a 30-call-site signature for a one-off
  host; sibling builder is lower-risk and matches the existing `build_org_url` precedent.
- **Three tools (`..._secret_alerts` / `..._dependency_alerts` / `..._code_alerts`)** — rejected:
  triples surface area, can't express "all types", and the API is already one filtered endpoint.
- **Expose `AlertsBatch`** — rejected: secret-only today; `list`/`get` cover the need.

## Affected areas & work split

All **developer** (no devops/IaC work). Suggested build order:

1. `client.py` — add `build_advsec_url()` next to `build_url`. (Isolated, no behaviour change.)
2. `models.py` — add the three input models + shared Literals in a new "Advanced Security" section.
3. `tools/advanced_security.py` — new module: import `mcp`, `write_tool`, the client helpers,
   define `_ADVSEC_API_VERSION = "7.2-preview.1"`, implement the three tools using `build_advsec_url`,
   `build_headers`, `request_with_retry`, `finalize_response`, `extract_error_message`, the
   `ValueError → HTTPStatusError → Exception` ladder, and `criteria.*` param mapping (filter `None`).
   List wraps results as `{"alerts": [...], "count": ...}` and surfaces the continuation token if the
   `paginate_results` helper is used.
4. `server.py` — add the one import line to register the module.
5. Docs — bump tool count and add the new tools to `README.md` (developer, follows existing PR
   convention).

## Risks & open questions

- **Unverifiable without a GHAzDo-enabled org.** The reference docs are authoritative for shapes, but
  the exact HTTP error when Advanced Security is **not enabled** on the repo/org (likely 403/404) is
  unconfirmed — the existing `extract_error_message` + error ladder will surface it cleanly
  regardless. The team live-test sandbox should be checked for GHAzDo enablement before integration
  testing.
- **Auth scope — most likely a no-op, must verify.** Docs list `vso.advsec` (read) and
  `vso.advsec_write` (write) as the OAuth scopes. The server authenticates with an **Entra `.default`
  token** for resource `499b84ac-1321-427f-aa17-267ca6975798` (see `client.py` `_AZDO_SCOPE`), which
  grants the union of the app's consented ADO scopes — it is **not** a per-`vso.*` scope token, so no
  scope change is expected. Residual risk: a tenant/app that has not consented to advsec scopes could
  see 401/403 on the write path specifically. Verify on first live test; if it fails, the fix is an
  Entra app-consent / ADO-permission change, **not** a code change to `_AZDO_SCOPE`.
- **`expand=validationFingerprint` can leak secrets in cleartext** (docs warn explicitly). Mitigation:
  default `expand` to unset (server default `none`); document the risk in the `get` tool's field
  description; never log the response body at INFO.
- **`repository` accepts name or ID** — no GUID validation (intentional). A wrong/disabled repo name
  yields an ADO HTTP error handled by the ladder.
- **Preview deprecation:** `7.2-preview.1` is preview-only; per [[Azure DevOps REST API]] a preview
  can be deactivated ~12 weeks after GA. Pin the exact string; revisit when a GA version ships.
- **List default-branch default:** `criteria.onlyDefaultBranch` defaults to true server-side and is
  not applicable to secret alerts — exposing it as an explicit optional avoids surprising omissions.

## Sources

- Alerts - Get: <https://learn.microsoft.com/en-us/rest/api/azure/devops/advancedsecurity/alerts/get?view=azure-devops-rest-7.2>
- Alerts - List: <https://learn.microsoft.com/en-us/rest/api/azure/devops/advancedsecurity/alerts/list?view=azure-devops-rest-7.2>
- Alerts - Update: <https://learn.microsoft.com/en-us/rest/api/azure/devops/advancedsecurity/alerts/update?view=azure-devops-rest-7.2>
- Alerts Batch - List: <https://learn.microsoft.com/en-us/rest/api/azure/devops/advancedsecurity/alerts-batch/list?view=azure-devops-rest-7.2>

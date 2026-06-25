"""Azure DevOps HTTP client with Microsoft Entra ID authentication and lifecycle management."""

import asyncio
import json
import logging
import os
import shutil
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import httpx
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import (
    AuthenticationRecord,
    AzureCliCredential,
    ClientSecretCredential,
    DefaultAzureCredential,
    InteractiveBrowserCredential,
    ManagedIdentityCredential,
    TokenCachePersistenceOptions,
)

logger = logging.getLogger(__name__)

API_VERSION = "7.1"

_AZDO_SCOPE = "499b84ac-1321-427f-aa17-267ca6975798/.default"
_TOKEN_REFRESH_BUFFER_SECONDS = 300

_AZ_CLI_CANDIDATE_PATHS = [
    r"C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin",
    r"C:\Program Files (x86)\Microsoft SDKs\Azure\CLI2\wbin",
]


_DEFAULT_AUTH_TIMEOUT_SECONDS = 30.0


@dataclass
class AppContext:
    """Application context holding shared auth state."""

    organization: str | None
    project: str | None
    credential: (
        AzureCliCredential
        | InteractiveBrowserCredential
        | ClientSecretCredential
        | ManagedIdentityCredential
        | DefaultAzureCredential
    )
    http_client: httpx.AsyncClient = field(default=None)  # type: ignore[assignment]
    _token_cache: dict[str, tuple[str, float]] = field(default_factory=dict)
    _token_locks: dict[str, asyncio.Lock] = field(default_factory=dict)


def _ensure_az_cli_on_path() -> None:
    if shutil.which("az"):
        return
    if os.name != "nt":
        logger.warning("Azure CLI not found on PATH.")
        return
    current_path = os.environ.get("PATH", "")
    existing = {os.path.normcase(os.path.normpath(p)) for p in current_path.split(os.pathsep) if p}
    additions = [p for p in _AZ_CLI_CANDIDATE_PATHS if os.path.isdir(p)
                 and os.path.normcase(os.path.normpath(p)) not in existing]
    if additions:
        os.environ["PATH"] = os.pathsep.join(additions) + os.pathsep + current_path
        logger.info("Added Azure CLI path(s) to PATH: %s", additions)
    else:
        logger.warning("Azure CLI not found. Ensure az is installed and on PATH.")


def _get_cached_bearer_token(app_ctx: AppContext) -> str | None:
    cached = app_ctx._token_cache.get(_AZDO_SCOPE)
    if cached:
        token_str, expires_on = cached
        if time.time() < expires_on - _TOKEN_REFRESH_BUFFER_SECONDS:
            return token_str
    return None


def get_bearer_token(app_ctx: AppContext) -> str:
    # Thread-safety of the cache write: this function runs inside asyncio.to_thread,
    # so the dict assignment executes on a worker thread.  Safety is ensured by two
    # independent guarantees: (1) the per-scope asyncio.Lock in build_headers
    # serializes cold-cache acquisitions — only one caller reaches this function at
    # a time for a given scope; (2) CPython's GIL makes the single dict assignment
    # (`_token_cache[key] = value`) atomic, so there is no torn write or lost update
    # even if a warm-cache reader on the event loop thread races with this write.
    access_token = app_ctx.credential.get_token(_AZDO_SCOPE)
    app_ctx._token_cache[_AZDO_SCOPE] = (access_token.token, float(access_token.expires_on))
    return access_token.token


async def build_headers(
    app_ctx: AppContext,
    *,
    include_content_type: bool = False,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build standard Azure DevOps API headers with a cached Bearer token.

    Token acquisition runs in a thread on cache miss to avoid blocking the event
    loop.  A per-scope lock ensures concurrent cold-cache callers trigger exactly
    one acquisition; the lock is also where the auth timeout is applied.
    """
    token = _get_cached_bearer_token(app_ctx)
    if token is None:
        # Lazy lock creation is race-free on a single-threaded event loop.
        lock = app_ctx._token_locks.setdefault(_AZDO_SCOPE, asyncio.Lock())
        async with lock:
            # Re-check: a concurrent caller may have populated the cache while
            # we were waiting for the lock.
            token = _get_cached_bearer_token(app_ctx)
            if token is None:
                auth_timeout = _get_auth_timeout_seconds()
                try:
                    # NOTE: asyncio.to_thread cannot be cancelled — the worker
                    # thread may outlive this timeout.  The timeout's purpose is
                    # to unblock other callers serialized behind this lock, not
                    # to kill the underlying credential call.  The lock is
                    # released on any exception (including TimeoutError) so
                    # subsequent callers are not permanently serialized.
                    token = await asyncio.wait_for(
                        asyncio.to_thread(get_bearer_token, app_ctx),
                        timeout=auth_timeout,
                    )
                except asyncio.TimeoutError as exc:
                    raise ClientAuthenticationError(
                        message=(
                            f"Credential acquisition timed out after {auth_timeout:.0f}s. "
                            "Check your Azure CLI session (az login), the AZDO_AUTH_TYPE "
                            "setting, or increase AZDO_AUTH_TIMEOUT_SECONDS."
                        )
                    ) from exc
    headers: dict[str, str] = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    if include_content_type:
        headers["Content-Type"] = "application/json"
    if extra:
        headers.update(extra)
    return headers


def extract_error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
        if "message" in body:
            type_key = body.get("typeKey", "")
            msg = body["message"]
            return f"{type_key}: {msg}" if type_key else msg
        return json.dumps(body)
    except Exception:
        return response.text[:500] if response.text else f"HTTP {response.status_code}"


def resolve_org(app_ctx: AppContext, organization: str | None) -> str:
    """Resolve the effective organization, raising if none is available."""
    effective = organization or app_ctx.organization
    if not effective:
        raise ValueError(
            "No Azure DevOps organization provided. Supply 'organization' on the tool "
            "input, or set AZDO_ORGANIZATION as a default."
        )
    return effective.strip()


def resolve_project(app_ctx: AppContext, project: str | None) -> str:
    """Resolve the effective project, raising if none is available."""
    effective = project or app_ctx.project
    if not effective:
        raise ValueError(
            "No Azure DevOps project provided. Supply 'project' on the tool "
            "input, or set AZDO_PROJECT as a default."
        )
    return effective.strip()


def build_url(organization: str, project: str, path: str) -> str:
    """Build a percent-encoded Azure DevOps REST API URL.

    ``organization`` and ``project`` are single URL segments and are encoded
    with ``safe=""`` so spaces and other special characters (common in project
    names) become valid percent-encoded sequences.

    ``path`` is a multi-segment route such as
    ``git/repositories/{id}/pullrequests/{n}`` where ``/`` is an intentional
    path separator, so it is encoded with ``safe="/"`` to preserve those
    separators while still encoding any spaces or special characters that appear
    within individual segments.
    """
    enc_org = quote(organization, safe="")
    enc_project = quote(project, safe="")
    enc_path = quote(path, safe="/")
    return f"https://dev.azure.com/{enc_org}/{enc_project}/_apis/{enc_path}"


def build_params(**kwargs) -> dict:
    """Build a params dict with the API version, filtering out None values."""
    params = {"api-version": API_VERSION}
    params.update({k: v for k, v in kwargs.items() if v is not None})
    return params


# ---------------------------------------------------------------------------
# Resilience helpers
# ---------------------------------------------------------------------------

# Status codes that are safe to retry under the right conditions.
_RETRYABLE_STATUS_CODES = (429, 502, 503, 504)

# HTTP methods where a retried request cannot cause a double-write.
# POST and PATCH are intentionally excluded: a 5xx may mean the write
# already committed on the server side, so we must NOT re-issue those.
# 429 (throttling) is the exception — a throttled POST was never executed,
# so retrying a 429 is safe for all methods (handled separately below).
_IDEMPOTENT_METHODS = {"GET", "PUT", "DELETE"}

_RETRY_MAX_WAIT_SECONDS = 30


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header value into a bounded wait duration.

    Returns the header value as a float capped at ``_RETRY_MAX_WAIT_SECONDS``,
    or ``None`` when the header is absent or not a valid number.  Only positive
    values are returned; zero and negative values are treated as absent.
    """
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    if seconds <= 0:
        return None
    return min(seconds, float(_RETRY_MAX_WAIT_SECONDS))


async def request_with_retry(
    http_client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict | None = None,
    params=None,
    json: dict | None = None,
    content: bytes | None = None,
    max_attempts: int = 3,
    **kwargs,
) -> httpx.Response:
    """Issue an HTTP request with automatic retry for transient failures.

    Retry rules:
    - 429 (throttling): retry on ALL methods — a throttled request was never
      executed by the server, so retrying a POST/PATCH after a 429 is safe.
      Honour the Retry-After header (capped at _RETRY_MAX_WAIT_SECONDS); fall
      back to exponential back-off (2^attempt, capped) when header is absent.
    - 502/503/504 (gateway/server errors): retry ONLY for idempotent methods
      (GET, PUT, DELETE). For POST/PATCH, return immediately — the server may
      have committed the write before the error was surfaced.
    - After max_attempts the last response is returned and the tool's own
      raise_for_status / error handling takes over.
    - Timeouts and connection errors are re-raised after the final attempt
      (or immediately on the last attempt) so the caller sees them.
    """
    method_upper = method.upper()
    last_response: httpx.Response | None = None
    last_exc: BaseException | None = None

    for attempt in range(max_attempts):
        try:
            response = await http_client.request(
                method_upper,
                url,
                headers=headers,
                params=params,
                json=json,
                content=content,
                **kwargs,
            )
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            if attempt == max_attempts - 1:
                raise
            last_exc = exc
            wait = min(2 ** attempt, _RETRY_MAX_WAIT_SECONDS)
            logger.warning(
                "Network error on %s %s (attempt %d/%d): %s — retrying in %.1fs",
                method_upper,
                url,
                attempt + 1,
                max_attempts,
                type(exc).__name__,
                wait,
            )
            await asyncio.sleep(wait)
            continue

        last_exc = None

        if response.status_code not in _RETRYABLE_STATUS_CODES:
            # Proactive throttle signals on non-retryable (including 2xx) responses.
            #
            # Azure DevOps first *delays* requests by returning HTTP 200 with a
            # Retry-After header and X-RateLimit-Remaining=0 before it escalates to
            # HTTP 429 (TF400733).  We must honour these signals so we don't keep
            # hammering the API until it hard-blocks us.
            #
            # Two independent signals to handle:
            # 1. X-RateLimit-Remaining=0  — server is near the limit; log a WARNING
            #    so operators are aware, but do not sleep on its own (it may not carry
            #    a Retry-After on every response).
            # 2. Retry-After present       — server explicitly requests a delay; sleep
            #    for the bounded parsed value.  When both signals are present we log
            #    once and sleep once (no double-logging).
            rate_remaining = response.headers.get("x-ratelimit-remaining")
            proactive_wait = _parse_retry_after(response.headers.get("retry-after"))

            if proactive_wait is not None:
                # Both signals may be present; a single WARNING covers both.
                logger.warning(
                    "Proactive throttle on HTTP %d %s %s — Retry-After: %.1fs "
                    "(X-RateLimit-Remaining: %s); sleeping before returning",
                    response.status_code,
                    method_upper,
                    url,
                    proactive_wait,
                    rate_remaining if rate_remaining is not None else "n/a",
                )
                await asyncio.sleep(proactive_wait)
            elif rate_remaining == "0":
                logger.warning(
                    "X-RateLimit-Remaining=0 on HTTP %d %s %s — "
                    "approaching rate limit; no Retry-After header present",
                    response.status_code,
                    method_upper,
                    url,
                )

            return response

        is_throttle = response.status_code == 429
        is_idempotent = method_upper in _IDEMPOTENT_METHODS

        # For 5xx (502/503/504) on non-idempotent methods, return immediately.
        # The write may have committed — never re-issue a POST or PATCH here.
        if not is_throttle and not is_idempotent:
            logger.warning(
                "HTTP %d on non-idempotent %s %s — not retrying (write may have committed)",
                response.status_code,
                method_upper,
                url,
            )
            return response

        # Determine how long to wait before the next attempt.
        wait = _parse_retry_after(response.headers.get("retry-after"))
        if wait is None:
            wait = min(2 ** attempt, _RETRY_MAX_WAIT_SECONDS)

        if attempt < max_attempts - 1:
            logger.warning(
                "HTTP %d on %s %s (attempt %d/%d) — retrying in %.1fs",
                response.status_code,
                method_upper,
                url,
                attempt + 1,
                max_attempts,
                wait,
            )
            await asyncio.sleep(wait)
            last_response = response
        else:
            # Last attempt exhausted — return and let the tool handle it.
            return response

    # If we exhausted attempts via network errors on the last loop, re-raise.
    if last_exc is not None:
        raise last_exc
    # Should be unreachable, but satisfy the type checker.
    assert last_response is not None
    return last_response


_FINALIZE_WARN_BYTES = 1_000_000
_FINALIZE_CAP_BYTES = 5_000_000


def finalize_response(
    payload: dict,
    *,
    warn_bytes: int = _FINALIZE_WARN_BYTES,
    cap_bytes: int = _FINALIZE_CAP_BYTES,
) -> str:
    """Serialize *payload* to a JSON string, enforcing a size cap.

    - Under warn_bytes: returned as-is.
    - Between warn_bytes and cap_bytes: logged to stderr, returned as-is.
    - Over cap_bytes: returns a JSON error object instead of the payload
      so that the MCP transport is never flooded with multi-MB content.

    Note: devops_get_run_log_content uses start_line/end_line slicing at the
    API level to bound log content — it does not rely on this cap to limit
    output, and the cap here serves only as a last-resort safeguard.
    """
    encoded = json.dumps(payload)
    size = len(encoded.encode("utf-8"))
    if size > cap_bytes:
        logger.warning(
            "Response payload %d bytes exceeds cap (%d bytes); returning error stub",
            size,
            cap_bytes,
        )
        return json.dumps({
            "error": True,
            "message": (
                f"Response exceeded {cap_bytes:,} bytes. "
                "Narrow your query, use paging (top / continuation_token), "
                "or use start_line/end_line to fetch a portion of log content."
            ),
        })
    if size > warn_bytes:
        logger.warning(
            "Large response payload: %d bytes (warn threshold %d bytes)",
            size,
            warn_bytes,
        )
    return encoded


async def paginate_results(
    http_client: httpx.AsyncClient,
    url: str,
    headers: dict,
    base_params: dict,
    record_key: str,
    top: int,
    initial_continuation_token: str | None = None,
) -> tuple[list, bool]:
    """Collect records across x-ms-continuationtoken pages (Azure DevOps style).

    Loops, issuing GETs via request_with_retry. On each response it reads the
    `x-ms-continuationtoken` header and URL-encodes the token into the next
    request's `continuationToken` query parameter. Terminates when the header
    is absent or when `top` records have been collected.

    The `count` in the Azure DevOps response envelope is per-page only — this
    helper ignores it and relies solely on the header presence/absence.

    Args:
        http_client: shared httpx.AsyncClient from AppContext.
        url: endpoint URL (without continuation token).
        headers: authorization / accept headers for each request.
        base_params: base query parameters (e.g. api-version, $top, filter).
        record_key: key in the JSON response that contains the list of records
                    (e.g. "value", "branches", etc.). Falls back to the "value"
                    key if this key is not present.
        top: maximum total records to collect across all pages.
        initial_continuation_token: optional token to start mid-sequence.

    Returns:
        A tuple of (records, has_more) where has_more is True when a
        continuation token was present but top was already reached.
    """
    all_records: list = []
    continuation_token: str | None = initial_continuation_token
    has_more = False

    while True:
        params = dict(base_params)
        if continuation_token is not None:
            params["continuationToken"] = quote(continuation_token, safe="")

        response = await request_with_retry(http_client, "GET", url, headers=headers, params=params)
        response.raise_for_status()

        data = response.json()
        if isinstance(data, dict):
            records = data.get(record_key) or data.get("value") or []
        else:
            records = data or []

        all_records.extend(records)

        continuation_token = response.headers.get("x-ms-continuationtoken")

        if len(all_records) >= top:
            all_records = all_records[:top]
            has_more = continuation_token is not None
            break

        if continuation_token is None:
            break

    return all_records, has_more


def _get_auth_timeout_seconds() -> float:
    """Return the credential-acquisition timeout from AZDO_AUTH_TIMEOUT_SECONDS.

    Falls back to _DEFAULT_AUTH_TIMEOUT_SECONDS and logs a warning when the env
    var is present but non-numeric or non-positive.
    """
    raw = os.environ.get("AZDO_AUTH_TIMEOUT_SECONDS", "").strip()
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
            logger.warning(
                "AZDO_AUTH_TIMEOUT_SECONDS=%r is non-positive; using default %.1fs",
                raw,
                _DEFAULT_AUTH_TIMEOUT_SECONDS,
            )
        except ValueError:
            logger.warning(
                "AZDO_AUTH_TIMEOUT_SECONDS=%r is not a valid number; using default %.1fs",
                raw,
                _DEFAULT_AUTH_TIMEOUT_SECONDS,
            )
    return _DEFAULT_AUTH_TIMEOUT_SECONDS


def _get_ephemeral_token() -> bool:
    """Return whether the interactive token cache should be ephemeral (in-memory).

    Reads AZDO_EPHEMERAL_TOKEN (default: false — unset/empty means persist to
    disk).  Only an explicit "true", "1", or "yes" (case-insensitive) opts into
    an ephemeral, in-memory-only token cache (no disk cache, no sidecar written).
    Any other unrecognised value falls back to the default (false) with a warning.
    """
    raw = os.environ.get("AZDO_EPHEMERAL_TOKEN", "").strip().lower()
    if not raw:
        return False
    if raw in ("true", "1", "yes"):
        return True
    if raw in ("false", "0", "no"):
        return False
    logger.warning(
        "AZDO_EPHEMERAL_TOKEN=%r is not a recognised boolean value; "
        "using default (false)",
        os.environ.get("AZDO_EPHEMERAL_TOKEN", ""),
    )
    return False


def _get_token_cache_profile() -> str:
    """Return a filename-safe profile suffix for the token cache and sidecar.

    Reads AZDO_TOKEN_CACHE_PROFILE (default: empty).  When set, the value is
    appended to both the MSAL cache name and the AuthenticationRecord sidecar
    filename so that two server processes connecting to different
    tenants/accounts on the same host do not share (and overwrite) each other's
    cache and pinned account.  An empty/unset value preserves the original
    single-profile filenames for backwards compatibility.

    Only ``[A-Za-z0-9_-]`` are permitted.  Any other character raises
    ``ValueError`` rather than being silently dropped: sanitizing would let two
    distinct profiles (e.g. ``a/b`` and ``a.b``) collapse to the same value and
    secretly share one cache — the exact cross-tenant collision this option
    exists to prevent.  Failing fast forces the operator to pick an unambiguous,
    filesystem-safe name.
    """
    raw = os.environ.get("AZDO_TOKEN_CACHE_PROFILE", "").strip()
    if not raw:
        return ""
    allowed = (
        "abcdefghijklmnopqrstuvwxyz"
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "0123456789-_"
    )
    if any(c not in allowed for c in raw):
        raise ValueError(
            f"AZDO_TOKEN_CACHE_PROFILE={raw!r} contains characters outside "
            "[A-Za-z0-9_-]. Choose a profile name using only letters, digits, "
            "dashes, or underscores so it is an unambiguous, filesystem-safe "
            "cache identifier."
        )
    return raw


def _get_user_config_dir() -> Path:
    """Return a per-user config directory for devops-mcp, creating it if needed."""
    config_dir = Path.home() / ".devops-mcp"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def _load_auth_record(record_path: Path) -> "AuthenticationRecord | None":
    """Load a persisted AuthenticationRecord from *record_path*.

    Returns None if the file is absent or cannot be parsed, logging a warning
    in the latter case so that corrupt sidecars degrade to a fresh prompt rather
    than crashing the server.
    """
    if not record_path.exists():
        return None
    try:
        text = record_path.read_text(encoding="utf-8")
        return AuthenticationRecord.deserialize(text)
    except Exception as exc:
        logger.warning(
            "Could not load AuthenticationRecord from %s (%s); "
            "a fresh interactive sign-in will be required",
            record_path,
            exc,
        )
        return None


def _save_auth_record(record: "AuthenticationRecord", record_path: Path) -> None:
    """Serialize *record* to *record_path* with best-effort 0600 permissions.

    The AuthenticationRecord contains no secrets (home_account_id, tenant,
    authority, username only), but we restrict permissions defensively.  On
    Windows, chmod is best-effort; NTFS ACLs govern actual access.
    """
    try:
        text = record.serialize()
        fd = os.open(
            str(record_path),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        try:
            os.write(fd, text.encode("utf-8"))
        finally:
            os.close(fd)
        # chmod is best-effort: on Windows it silently does little,
        # on Unix it corrects the umask-applied mode from os.open.
        try:
            os.chmod(str(record_path), 0o600)
        except OSError:
            pass
        logger.info("AuthenticationRecord saved to %s", record_path)
    except Exception as exc:
        logger.warning(
            "Could not save AuthenticationRecord to %s (%s); "
            "restart re-prompts may still occur",
            record_path,
            exc,
        )


def _build_interactive_credential() -> InteractiveBrowserCredential:
    """Build an InteractiveBrowserCredential with a persistent token cache.

    By default the credential is constructed with TokenCachePersistenceOptions
    so MSAL stores its token cache on disk via the OS secret store (DPAPI on
    Windows, Keychain on macOS, libsecret on Linux).  Set AZDO_EPHEMERAL_TOKEN=true
    to opt into an in-memory-only cache (no disk cache, no sidecar).

    An AuthenticationRecord sidecar is loaded from the user config dir on
    startup so that MSAL can silently select the previously authenticated account
    on restart without re-prompting the user.  If no sidecar exists yet, a
    one-shot get_token wrapper saves one after the first interactive sign-in.

    GOTCHA: the wrapper MUST restore credential.get_token to the original method
    BEFORE calling credential.authenticate(), because authenticate() is
    implemented as self.get_token(…) internally — leaving the wrapper in place
    causes unbounded recursion that is silently swallowed.
    """
    tenant_id = os.environ.get("AZDO_TENANT_ID")

    if _get_ephemeral_token():
        logger.info(
            "AZDO_EPHEMERAL_TOKEN=true: "
            "interactive credential uses in-memory token cache only"
        )
        return (
            InteractiveBrowserCredential(tenant_id=tenant_id)
            if tenant_id
            else InteractiveBrowserCredential()
        )

    # Optional profile suffix isolates the cache + sidecar per tenant/account so
    # concurrent sessions on one host do not collide.
    profile = _get_token_cache_profile()
    cache_name = "devops-mcp.cache" if not profile else f"devops-mcp.{profile}.cache"
    record_filename = (
        "auth-record.json" if not profile else f"auth-record.{profile}.json"
    )
    if profile:
        logger.info("Token cache profile active: %r (cache name=%s)", profile, cache_name)

    # Build cache persistence options.  allow_unencrypted_storage=False (the
    # default) means the credential will raise on platforms without an OS
    # secret store — we catch that below and log an actionable message.
    try:
        cache_opts = TokenCachePersistenceOptions(
            name=cache_name,
            allow_unencrypted_storage=False,
        )
    except Exception as exc:
        logger.warning(
            "Could not initialise TokenCachePersistenceOptions (%s); "
            "falling back to in-memory token cache.  "
            "On headless Linux, install libsecret-1 or set "
            "AZDO_EPHEMERAL_TOKEN=true to suppress this warning.",
            exc,
        )
        return (
            InteractiveBrowserCredential(tenant_id=tenant_id)
            if tenant_id
            else InteractiveBrowserCredential()
        )

    config_dir = _get_user_config_dir()
    record_path = config_dir / record_filename
    auth_record = _load_auth_record(record_path)

    kwargs: dict = {"cache_persistence_options": cache_opts}
    if tenant_id:
        kwargs["tenant_id"] = tenant_id
    if auth_record is not None:
        kwargs["authentication_record"] = auth_record

    try:
        credential = InteractiveBrowserCredential(**kwargs)
    except Exception as exc:
        logger.warning(
            "Could not build InteractiveBrowserCredential with persistent cache (%s); "
            "falling back to in-memory credential.  "
            "On headless Linux without a secret store, set "
            "AZDO_EPHEMERAL_TOKEN=true to suppress this warning.",
            exc,
        )
        return (
            InteractiveBrowserCredential(tenant_id=tenant_id)
            if tenant_id
            else InteractiveBrowserCredential()
        )

    logger.info(
        "Interactive token cache persistence enabled (encrypted=True, sidecar=%s)",
        record_path,
    )

    if auth_record is None:
        # No prior sidecar: install a one-shot wrapper to capture the
        # AuthenticationRecord after the first interactive sign-in and persist
        # it so subsequent restarts can authenticate silently.
        _original_get_token = credential.get_token

        def _get_token_and_record(*args, **kw):
            # Call the real get_token first so the user is prompted and a token
            # is obtained.
            token = _original_get_token(*args, **kw)
            # CRITICAL: restore BEFORE calling authenticate() — authenticate()
            # is implemented internally as self.get_token(…), so if the wrapper
            # is still installed on the instance it re-enters this function
            # causing unbounded recursion (silently swallowed by the broad
            # except, meaning _save_auth_record is never reached).
            credential.get_token = _original_get_token  # type: ignore[method-assign]
            try:
                record = credential.authenticate(scopes=list(args))
                _save_auth_record(record, record_path)
            except Exception as exc:
                logger.warning(
                    "Could not obtain AuthenticationRecord after sign-in (%s); "
                    "restart re-prompts will still occur",
                    exc,
                )
            return token

        credential.get_token = _get_token_and_record  # type: ignore[method-assign]

    return credential


def _build_credential(auth_type: str):
    """Instantiate an azure-identity credential based on AZDO_AUTH_TYPE."""
    _ensure_az_cli_on_path()
    if auth_type == "azure_cli":
        return AzureCliCredential()
    if auth_type == "interactive":
        return _build_interactive_credential()
    if auth_type == "client_secret":
        tenant_id = os.environ.get("AZDO_TENANT_ID", "")
        client_id = os.environ.get("AZDO_CLIENT_ID", "")
        client_secret = os.environ.get("AZDO_CLIENT_SECRET", "")
        missing = [
            name
            for name, val in (
                ("AZDO_TENANT_ID", tenant_id),
                ("AZDO_CLIENT_ID", client_id),
                ("AZDO_CLIENT_SECRET", client_secret),
            )
            if not val
        ]
        if missing:
            raise ValueError(
                f"AZDO_AUTH_TYPE=client_secret requires: {', '.join(missing)}"
            )
        return ClientSecretCredential(tenant_id, client_id, client_secret)
    if auth_type == "managed_identity":
        return ManagedIdentityCredential()
    if auth_type == "default":
        return DefaultAzureCredential()
    raise ValueError(
        f"Unknown AZDO_AUTH_TYPE '{auth_type}'. "
        "Valid values: azure_cli, interactive, client_secret, managed_identity, default"
    )


async def _log_request(request: httpx.Request) -> None:
    auth_header = request.headers.get("authorization", "")
    logger.debug(
        "HTTP %s %s | Authorization header: %s (length=%d)",
        request.method,
        request.url,
        "present" if auth_header else "MISSING",
        len(auth_header),
    )


async def _log_response(response: httpx.Response) -> None:
    logger.debug("HTTP response %d for %s %s", response.status_code, response.request.method, response.request.url)
    if response.status_code in (301, 302, 303, 307, 308):
        logger.warning(
            "Redirect %d -> %s",
            response.status_code,
            response.headers.get("location", "<no location>"),
        )


@asynccontextmanager
async def devops_lifespan(server) -> AsyncIterator[AppContext]:
    """FastMCP lifespan that initializes shared Azure DevOps auth state.

    Reads configuration from environment variables:
    - AZDO_AUTH_TYPE: Credential type (default: default)
        default          — DefaultAzureCredential (tries all methods in order) [recommended]
        azure_cli        — Azure CLI credential (az login)
        interactive      — Interactive browser login
        client_secret    — Service principal with client secret
                           (requires AZDO_TENANT_ID, AZDO_CLIENT_ID, AZDO_CLIENT_SECRET)
        managed_identity — Managed identity (Azure-hosted workloads)
    - AZDO_TENANT_ID:     Entra ID tenant ID (required for client_secret)
    - AZDO_CLIENT_ID:     Service principal client ID (required for client_secret)
    - AZDO_CLIENT_SECRET: Service principal client secret (required for client_secret)
    - AZDO_ORGANIZATION:  Default organization name (optional; can be supplied per-tool)
    - AZDO_PROJECT:       Default project name (optional; can be supplied per-tool)

    Yields:
        AppContext containing the credential, HTTP client, and optional defaults.
    """
    auth_type = os.environ.get("AZDO_AUTH_TYPE", "default").lower()
    organization = os.environ.get("AZDO_ORGANIZATION")
    project = os.environ.get("AZDO_PROJECT")

    credential = _build_credential(auth_type)
    logger.info("Azure DevOps auth type: %s", auth_type)

    if organization:
        logger.info("Default Azure DevOps organization: %s", organization)
    else:
        logger.info("No AZDO_ORGANIZATION set; tools must supply 'organization'")

    if project:
        logger.info("Default Azure DevOps project: %s", project)
    else:
        logger.info("No AZDO_PROJECT set; tools must supply 'project'")

    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=30.0, write=60.0, pool=5.0),
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        event_hooks={"request": [_log_request], "response": [_log_response]},
    )

    app_ctx = AppContext(
        organization=organization,
        project=project,
        credential=credential,
        http_client=http_client,
    )
    logger.info("Azure DevOps MCP server initialized")

    try:
        yield app_ctx
    finally:
        await http_client.aclose()
        close_fn = getattr(credential, "close", None)
        if callable(close_fn):
            close_fn()
        logger.info("Azure DevOps MCP server shutting down")

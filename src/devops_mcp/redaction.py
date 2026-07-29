"""Pure secret-hygiene helpers for service connection and variable group tools.

Both `serviceendpoint/endpoints` (`authorization.parameters`) and
`distributedtask/variablegroups` (`variables`, `providerData`) return
credential-shaped payloads with no documented redaction contract from
Microsoft — the reference schema types these fields as bare `object`/`string`
and the samples show inconsistent shapes for the same field.

This is not theoretical. Verified live against api-version 7.1: the server
nulls `authorization.parameters.password` and secret variable values itself,
but echoes `data.clientSecret` back in full plaintext on both create and get,
despite the key name. Redaction here is load-bearing, not defence in depth.

This module has no HTTP or MCP dependencies — every function here is pure and
operates on already-fetched dicts, so it is safe to unit test without a
network call and without importing the rest of the server.

Two complementary rules:

- Rule 1 (allowlist): `authorization.parameters` is type-specific and
  open-ended (any endpoint type/extension can define new keys), so it is
  projected onto an explicit allowlist rather than denylisted — a denylist
  fails open on the first unknown type.
- Rule 2 (denylist): `data`, `providerData`, and unmarked variable names are
  scrubbed with a case-insensitive substring denylist. Over-redaction here is
  a recoverable annoyance; under-redaction of a credential is not.
"""

# Rule 1 — allowlisted `authorization.parameters` keys (lower-cased). Only
# devops_get_service_connection touches this bag; the list tool never reads
# authorization.parameters at all.
AUTH_PARAM_ALLOWLIST: set[str] = {
    "authenticationtype",
    "tenantid",
    "serviceprincipalid",
    "scope",
    "role",
    "username",
    "loginserver",
    "issuer",
    "subject",
    "workloadidentityfederationissuer",
    "workloadidentityfederationsubject",
}

# Rule 2 — case-insensitive substring denylist applied to `data`,
# `providerData`, and unmarked variable names. Broad on purpose: `key` and
# `auth` will over-match some legitimate names (e.g. `region_key`), which is
# an acceptable trade against under-redacting a real credential.
SECRET_NAME_SUBSTRINGS: set[str] = {
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "apikey",
    "api_key",
    "key",
    "cert",
    "certificate",
    "privatekey",
    "credential",
    "connectionstring",
    "sas",
    "signature",
    "accountkey",
    "passphrase",
    "pat",
    "auth",
}


def is_secret_name(name: str) -> bool:
    """Return True if *name* contains a denylisted substring (case-insensitive)."""
    lowered = name.lower()
    return any(substring in lowered for substring in SECRET_NAME_SUBSTRINGS)


def scrub_mapping(m: dict | None) -> tuple[dict, list[str]]:
    """Apply the Rule 2 name-substring denylist to a flat mapping.

    Returns a tuple of (kept, dropped_key_names). Values are never inspected —
    only the key name determines whether an entry is withheld. A falsy/None
    input returns ({}, []).
    """
    if not m:
        return {}, []
    kept: dict = {}
    dropped: list[str] = []
    for key, value in m.items():
        if is_secret_name(str(key)):
            dropped.append(key)
        else:
            kept[key] = value
    return kept, dropped


def allowlist_mapping(m: dict | None, allow: set[str]) -> tuple[dict, list[str]]:
    """Apply the Rule 1 allowlist to a flat mapping.

    Keys are matched case-insensitively against *allow*; the returned `kept`
    dict preserves the original key casing. Returns a tuple of
    (kept, dropped_key_names) — dropped contains key names only, never values,
    so a caller can report which fields were withheld without leaking them.
    A falsy/None input returns ({}, []).
    """
    if not m:
        return {}, []
    kept: dict = {}
    dropped: list[str] = []
    for key, value in m.items():
        if str(key).lower() in allow:
            kept[key] = value
        else:
            dropped.append(key)
    return kept, dropped


def project_identity(ref: dict | None) -> dict | None:
    """Project an Azure DevOps IdentityRef onto {id, display_name, unique_name}.

    Drops _links, imageUrl, descriptor, profileUrl, url, directoryAlias,
    inactive, isAadIdentity, isContainer, isDeletedInOrigin — noise fields
    with no diagnostic value for an LLM. Returns None when ref is falsy.
    """
    if not ref:
        return None
    return {
        "id": ref.get("id"),
        "display_name": ref.get("displayName"),
        "unique_name": ref.get("uniqueName"),
    }

"""Pure media helpers for the work item attachment tools.

Mirrors the `devops_mcp.redaction` boundary: no HTTP, no MCP, no filesystem and
no pydantic imports — every function here is pure and operates on strings or
already-materialised bytes, so it is unit-testable without a network call and
without importing the rest of the server.

Four concerns live here:

1. **Image type detection.** The attachment download endpoint's response
   `Content-Type` is *unreliable*, which is why the bytes are sniffed for a
   magic-byte signature instead, with the filename extension used only as a
   fallback. Measured against the live service: a PNG fetched with `?fileName=`
   supplied comes back as `image/png`, but the same attachment fetched by GUID
   alone — the call shape this server makes whenever no file name is known —
   comes back as `application/octet-stream`, which is also what the reference
   declares (`application/octet-stream` / `application/zip`). A header that is
   right only when the caller already knew the answer is no basis for a decision.
   On the upload path the same sniffing is a security control, not a nicety: it
   is what stops `cp ~/.ssh/id_rsa /tmp/x.png` from defeating the extension
   allowlist.

   BMP, TIFF and SVG are deliberately excluded — the first two are not
   universally supported by MCP clients, and SVG is XML with script-execution
   semantics that has no business being handed to a renderer.

2. **Attachment URL parsing (SSRF guard).** A caller-supplied attachment URL is
   attacker-influenceable: it is typically copied out of a work item
   description, which is user-authored content and therefore a prompt-injection
   surface. Issuing `GET <caller-url>` with an `Authorization: Bearer …` header
   would hand a live Azure DevOps token to any host an injected description
   names — a total org credential compromise from one crafted field.

   The guard is **not** a host allowlist (an open redirect on a Microsoft-owned
   host would defeat that). `parse_attachment_url` validates the URL, extracts
   only the attachment GUID and the `fileName` hint, and throws the URL away;
   the caller rebuilds the request URL from its own resolved organization. It
   raises before the caller ever mints a token.

3. **Embed snippet construction.** Work item large-text fields are HTML unless
   flipped one-way to markdown, and the correct embed syntax differs per format,
   so `build_embed_snippets` emits both rather than making the model guess.

4. **Inline attachment-URL discovery.** An image pasted into a description or a
   comment is *only* a URL inside the field text — it is not necessarily an
   `AttachedFile` relation, so enumerating relations misses it entirely.
   `find_attachment_urls` locates URL-shaped candidates in raw field text. It is
   deliberately a **candidate finder, not a validator**: every candidate it
   returns is untrusted, attacker-influenceable text that must still be put
   through `parse_attachment_url` before anything is done with it.
"""

import html
import re
import uuid
from urllib.parse import unquote, urlsplit

# Formats an MCP client can be expected to render. The value is what must be
# passed to `Image(format=…)`: note "jpeg", never "jpg" — the SDK builds the
# MIME type as f"image/{format}" with no validation, so "jpg" yields the
# invalid "image/jpg" and a client-side hard error.
IMAGE_FORMATS: tuple[str, ...] = ("png", "jpeg", "gif", "webp")

# Extension -> Image(format=…). Also the upload allowlist: this is what removes
# .env, id_rsa, .pem, .kdbx and appsettings.json from an LLM's reach.
EXTENSION_FORMATS: dict[str, str] = {
    ".png": "png",
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".gif": "gif",
    ".webp": "webp",
}

ALLOWED_UPLOAD_EXTENSIONS: frozenset[str] = frozenset(EXTENSION_FORMATS)

# Raw byte cap for an image returned as an inline MCP image block. Base64
# inflates by 4/3, so 3,750,000 raw lands at exactly 5,000,000 characters on the
# wire — the same ceiling `finalize_response` enforces for JSON. That helper
# cannot police the Image branch (those bytes never pass through it), so every
# tool that returns an Image re-checks this explicitly. Do not raise it without
# evidence: MCP clients impose their own image ceilings and those are not part
# of the protocol.
MAX_INLINE_IMAGE_BYTES = 3_750_000

# Hosts that can legitimately serve an Azure DevOps attachment.
_DEV_AZURE_HOST = "dev.azure.com"
_LEGACY_HOST_RE = re.compile(r"^(?P<org>[a-z0-9][a-z0-9-]*)\.visualstudio\.com$", re.IGNORECASE)

# The modern attachment route. Matched on path *shape* rather than an exact
# string because what the web UI writes into an <img src> (absolute vs
# site-relative, extra query parameters, project-scoped vs org-scoped) is not
# documented anywhere and must be tolerated. Confirmed live: the url on an
# upload's AttachmentReference is *project-GUID*-scoped —
# https://dev.azure.com/{org}/{projectGuid}/_apis/wit/attachments/{guid}?fileName=…
# — not org-scoped as the published samples show, which is exactly why the
# prefix group is non-greedy and the organization is read off the first path
# segment rather than assumed to be the only one.
_ATTACHMENT_PATH_RE = re.compile(
    r"^(?P<prefix>/.*?)?/_apis/wit/attachments/"
    r"(?P<id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/?$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Image type detection
# ---------------------------------------------------------------------------


def sniff_image_format(data: bytes) -> str | None:
    """Return the image format implied by *data*'s magic bytes, else None.

    Recognises exactly the four formats in ``IMAGE_FORMATS``. The return value
    is the string to pass to ``Image(format=…)`` — "jpeg", not "jpg".
    """
    if not data:
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"GIF8"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def image_format_from_name(name: str | None) -> str | None:
    """Return the image format implied by *name*'s extension, else None.

    A filename is caller-influenceable, so this is only ever a fallback for
    ``sniff_image_format`` on the download path — and on the upload path it is
    a pre-filter that magic-byte verification must still confirm.
    """
    if not name:
        return None
    _, dot, ext = name.rpartition(".")
    if not dot:
        return None
    return EXTENSION_FORMATS.get(f".{ext.lower()}")


def mime_type_for_format(image_format: str) -> str:
    """Return the MIME type for an ``IMAGE_FORMATS`` value (e.g. 'image/png')."""
    return f"image/{image_format.lower()}"


def detect_image_format(data: bytes, file_name: str | None = None) -> tuple[str | None, str | None]:
    """Return ``(image_format, detected_by)`` for *data*, or ``(None, None)``.

    The single decision procedure every media tool shares: magic bytes decide,
    and the file name (or path) is consulted only when the bytes say nothing at
    all — a name is caller-influenceable, and an upstream ``Content-Type`` is
    worse still (the attachment endpoint answers ``application/octet-stream``
    for a PNG fetched by GUID alone).

    ``detected_by`` is "magic_bytes" or "file_extension", suitable for reporting
    to the model so it can tell a proven image from a guessed one.
    """
    image_format = sniff_image_format(data)
    if image_format is not None:
        return image_format, "magic_bytes"

    image_format = image_format_from_name(file_name)
    if image_format is not None:
        return image_format, "file_extension"

    return None, None


# ---------------------------------------------------------------------------
# File-name hygiene (shared by the models and the URL parser)
# ---------------------------------------------------------------------------

# Long enough for any real attachment; short enough that a hostile name cannot
# be used to pad a stored relation URL. Matches the models' own limit, which is
# the point — one number, one predicate.
MAX_FILE_NAME_LENGTH = 255


def file_name_rejection_reason(name: str | None) -> str | None:
    """Return why *name* is unusable as an attachment file name, else ``None``.

    Deliberately *not* the upload validator: an ``AttachedFile`` relation may
    point at a PDF, a zip or a log, so no extension allowlist applies here. What
    is enforced is what makes a value safe to percent-encode into a URL that may
    then be persisted on a work item — no control characters (CR/LF included)
    and a bounded length.

    A predicate rather than a raising validator because the two callers need
    different policies from the same rule: the models raise (a bad explicit
    ``file_name`` is a caller error worth reporting), while a name parsed out of
    a caller-supplied URL is silently dropped (the attachment GUID is what a
    download depends on, and it is unaffected).
    """
    if not name:
        return "it is empty or whitespace-only"
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        return "it contains control characters (including newlines)"
    if len(name) > MAX_FILE_NAME_LENGTH:
        return f"it is longer than {MAX_FILE_NAME_LENGTH} characters (got {len(name)})"
    return None


# ---------------------------------------------------------------------------
# Attachment URL parsing (SSRF guard)
# ---------------------------------------------------------------------------


def _reject(reason: str) -> "ValueError":
    return ValueError(
        f"Refusing to use the supplied attachment 'url': {reason}. "
        "Supply 'attachment_id' (the attachment GUID) instead, or pass a URL of "
        "the form https://dev.azure.com/{organization}/_apis/wit/attachments/{guid}."
    )


def parse_attachment_url(url: str, expected_org: str) -> tuple[str, str | None]:
    """Validate an Azure DevOps attachment URL and extract (guid, file_name).

    The returned values are the *only* things a caller may keep: the URL itself
    must be discarded and the request re-issued against a URL built from the
    caller's own resolved organization. See the module docstring for why.

    Three URL *shapes* are accepted, because all three occur in the wild:

    - absolute — ``https://dev.azure.com/{org}/…`` or the legacy
      ``https://{org}.visualstudio.com/…``;
    - protocol-relative — ``//dev.azure.com/{org}/…``;
    - site-relative — ``/{org}/_apis/wit/attachments/{guid}``.

    The relative forms weaken nothing. The URL is discarded either way and the
    request is rebuilt from the caller's own organization, so there is no host
    to follow; what a site-relative URL still has to pass is the *organization*
    check, since its first path segment names an org exactly as the absolute
    form's does. A protocol-relative URL does carry a host, so it gets the full
    host/userinfo/port treatment with the scheme taken to be https.

    Raises ``ValueError`` (with an actionable message) for anything else: a
    non-https scheme, userinfo, a non-443 port, a host that is not
    ``dev.azure.com`` or ``{org}.visualstudio.com``, a path that is not the
    ``/_apis/wit/attachments/{guid}`` route, an id that is not a GUID, or an
    organization that does not match *expected_org* case-insensitively.
    """
    if not url or not url.strip():
        raise _reject("it is empty")

    raw = url.strip()
    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise _reject(f"it is not a parseable URL ({exc})")

    # urlsplit already does the right thing with both relative shapes: '//host/p'
    # yields netloc='host', and '/p' yields netloc=''. The leading characters are
    # what distinguishes them, since neither carries a scheme.
    protocol_relative = not parts.scheme and raw.startswith("//")
    site_relative = not parts.scheme and raw.startswith("/") and not protocol_relative

    legacy = None
    if not site_relative:
        if not protocol_relative and parts.scheme.lower() != "https":
            raise _reject(f"the scheme is {parts.scheme!r}, not 'https'")

        # Userinfo is the classic "https://dev.azure.com@evil.example/…" confusion —
        # the real host is everything after the '@'.
        if "@" in parts.netloc:
            raise _reject("the host contains userinfo ('@'), which disguises the real host")

        try:
            port = parts.port
        except ValueError:
            raise _reject("the port is not a valid number")
        if port not in (None, 443):
            raise _reject(f"an explicit non-443 port ({port}) is not allowed")

        # Match on the *parsed* hostname, never a substring of the raw string.
        host = (parts.hostname or "").lower()
        if not host:
            raise _reject("it has no host")

        legacy = _LEGACY_HOST_RE.match(host)
        if host != _DEV_AZURE_HOST and legacy is None:
            raise _reject(
                f"host {host!r} is not an Azure DevOps host "
                "(expected 'dev.azure.com' or '{org}.visualstudio.com')"
            )

    match = _ATTACHMENT_PATH_RE.match(parts.path or "")
    if match is None:
        raise _reject("the path is not the /_apis/wit/attachments/{guid} route")

    attachment_id = match.group("id")
    try:
        uuid.UUID(attachment_id)
    except ValueError:
        raise _reject("the attachment id in the path is not a valid GUID")

    # Organization: first path segment on dev.azure.com, subdomain label on the
    # legacy host. A token minted for one org must never travel to a URL naming
    # another, so a mismatch is refused rather than silently rewritten.
    if legacy is not None:
        url_org = legacy.group("org")
    else:
        prefix = (match.group("prefix") or "").strip("/")
        url_org = unquote(prefix.split("/", 1)[0]) if prefix else ""
    if not url_org:
        raise _reject("no organization could be read from the URL")
    if url_org.casefold() != (expected_org or "").strip().casefold():
        raise ValueError(
            f"The attachment URL names organization '{url_org}', but this call resolved "
            f"to organization '{expected_org}'. Refusing to send credentials for one "
            "organization to a URL naming another — pass 'organization' explicitly if "
            "you intended to read from that organization."
        )

    return attachment_id, _file_name_from_query(parts.query)


# The 'fileName' key, at the start of the query or after a separator.
_FILE_NAME_KEY_RE = re.compile(r"(?:\A|&)filename=", re.IGNORECASE)

# An '&' that is followed by something shaped like a *parameter*: a run of
# characters legal in a query key, then '='. Deliberately excludes '%', so an
# encoded space ('%20b=…') never reads as the start of a parameter.
_NEXT_PARAM_RE = re.compile(r"&(?=[A-Za-z0-9_.~+-]+=)")


def _file_name_from_query(query: str) -> str | None:
    """Return the 'fileName' query value (case-insensitive key), else None.

    **This is a heuristic, and it has to be.** Azure DevOps emits the file name
    into this query string *without* encoding an '&' inside it. A real observed
    upload response url ends:

        ?fileName=a%20&%20b%20(1).png        (file name: "a & b (1).png")

    Space is percent-encoded, '&' is not — so the query is ambiguous at the byte
    level and no parser can be universally correct. ``parse_qs`` resolves the
    ambiguity the standards-compliant way (every '&' separates parameters) and
    therefore returns "a", which is wrong for every attachment whose name
    contains an ampersand.

    The heuristic, matching the shape the service actually emits: take the value
    as everything after ``fileName=`` up to the first '&' that is *followed by a
    parameter-shaped token* (``key=``). Azure DevOps puts ``fileName`` on these
    urls as the only parameter, so in practice the whole remainder is the value;
    an '&' inside the name is swallowed because the text after it is not a
    ``key=`` pair, while a genuine ``&download=false`` still terminates it. The
    residual failure is a file name containing a literal "&word=" — accepted as
    unavoidable, and cheap: the file name is a display/format hint only, the
    GUID (which is what the download depends on) comes from the *path*.

    Nothing here is a security control. The host, path, GUID and organization
    checks in ``parse_attachment_url`` all run first and are unaffected.
    """
    if not query:
        return None

    match = _FILE_NAME_KEY_RE.search(query)
    if match is None:
        return None

    remainder = query[match.end() :]
    end = _NEXT_PARAM_RE.search(remainder)
    raw = remainder[: end.start()] if end else remainder

    # unquote, not unquote_plus: this producer percent-encodes space as %20
    # (observed), i.e. it is RFC 3986 encoding rather than form encoding, so a
    # '+' in the query is a literal '+' in the file name and must survive.
    candidate = unquote(raw).strip()
    if not candidate:
        return None
    # Defense in depth. A file name reaching this function came out of a
    # caller-supplied (and often user-authored) URL, whereas the explicit
    # `file_name` model field is validated by the model. Both paths now share
    # one predicate so they cannot drift; only the *policy* differs, and
    # deliberately so — an explicit field is a caller error worth raising on,
    # while a hint scraped from a URL is dropped so the attachment's GUID (which
    # is what a download actually depends on) stays usable.
    if file_name_rejection_reason(candidate) is not None:
        return None
    return candidate


# ---------------------------------------------------------------------------
# Inline attachment-URL discovery
# ---------------------------------------------------------------------------

# One pass covers BOTH large-text field formats, because an HTML
# `<img src="URL">` and a markdown `![alt](URL)` each contain the same bare URL
# — there is nothing to gain from two parsers, and a second one is a second
# thing to get wrong.
#
# The search is anchored on the one fixed point — the `/_apis/wit/attachments/`
# route plus a GUID — and the URL's *start* is then found by walking left to the
# nearest delimiter. That inversion is deliberate. What the web UI writes into
# an <img src> is undocumented and demonstrably varied (absolute,
# protocol-relative `//host/…`, site-relative `/{org}/…`, org-scoped,
# project-GUID-scoped, extra query parameters); expressing all of those as
# regex alternations with a permissive prefix reintroduces exactly the
# catastrophic-backtracking risk this code has to avoid, since the leading `/`
# alternative can start at every slash in a hostile 200 KB description. Walking
# left is linear and the bound is explicit and obvious.
#
# Delimiters are excluded rather than enumerated: whitespace ends any URL,
# `"` / `'` / `<` / `>` end an HTML attribute or tag, and `(` / `)` / `[` / `]`
# end a markdown link destination or label (Azure DevOps percent-encodes
# parentheses inside a fileName, and this server's own embed builder does too,
# so a legitimate URL never contains a bare one).
#
# SAFETY: this finds *candidates*. It is not a trust boundary and makes no
# security decision. Host, scheme, port, userinfo, path shape, GUID validity and
# organization ownership are all decided afterwards by `parse_attachment_url`,
# which throws the matched string away and keeps only the GUID and file name.
# In particular the relative shapes are candidates like any other: a
# site-relative URL whose first path segment names a different organization is
# refused there and *counted*, which is the whole point of matching it — a
# missed candidate is worse than a rejected one, because a miss is invisible to
# the caller.
_INLINE_ATTACHMENT_ROUTE_RE = re.compile(
    r"/_apis/wit/attachments/"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"(?:\?[^\s\"'<>()\[\]\\]{0,512})?",
    re.IGNORECASE,
)

# Characters that terminate a URL token in either field format. Whitespace is
# handled separately via str.isspace() so every Unicode space counts.
_URL_DELIMITERS = frozenset("\"'<>()[]\\")

# How far left of the route the URL's start may be. A real prefix is a host plus
# at most a couple of path segments; anything longer is not an attachment URL,
# and refusing to scan further keeps the work per match strictly bounded.
_MAX_URL_PREFIX_CHARS = 512

# Only well-formed, semicolon-terminated references for the five characters HTML
# escaping actually produces. Deliberately NOT html.unescape(), which also
# decodes semicolon-less legacy entities — it would turn the perfectly ordinary
# file name "a&notb.png" into "a¬b.png".
_HTML_ENTITY_REPLACEMENTS = {
    "&amp;": "&",
    "&#38;": "&",
    "&#x26;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&apos;": "'",
    "&#39;": "'",
    "&#x27;": "'",
}
_HTML_ENTITY_RE = re.compile(
    "|".join(re.escape(entity) for entity in _HTML_ENTITY_REPLACEMENTS),
    re.IGNORECASE,
)


def _is_url_delimiter(ch: str) -> bool:
    return ch.isspace() or ch in _URL_DELIMITERS


def _unescape_html_entities(candidate: str) -> str:
    """Decode the HTML entities an html-format field escapes a URL with.

    Live-observed: an html-format description stores an attachment link whose
    file name contains '&' as ``?fileName=a%20&amp;%20b%20%281%29.png``. Left
    encoded, the literal text "&amp;" becomes the reported file name and is then
    percent-encoded into the canonical URL as ``%26amp%3B``.

    Trade-off, taken knowingly: this runs on every candidate, not only ones from
    html-format fields, because the work item GET does not report which format
    each large-text field is stored in — there is no reliable signal to branch
    on, and guessing wrong in the other direction reintroduces the bug. The cost
    is that a *markdown*-format field whose file name contains the five literal
    characters "&amp;" is displayed with a bare "&". That is a display-only
    inaccuracy in a value this module already documents as a heuristic hint: the
    GUID, which is what a download depends on, comes from the path and is
    untouched. One pass, so "&amp;amp;" decodes once to "&amp;" rather than
    collapsing all the way to "&".
    """
    return _HTML_ENTITY_RE.sub(
        lambda m: _HTML_ENTITY_REPLACEMENTS[m.group(0).lower()], candidate
    )


def _token_start(text: str, route_start: int) -> int | None:
    """Return the index the URL token containing *route_start* begins at.

    ``None`` when no delimiter is found within ``_MAX_URL_PREFIX_CHARS`` — the
    token is then too long to be an attachment URL, and stopping mid-token would
    invent a leading '/' that is not really the start of anything.
    """
    limit = route_start - _MAX_URL_PREFIX_CHARS
    i = route_start
    while i > 0 and i - 1 >= limit and not _is_url_delimiter(text[i - 1]):
        i -= 1
    if i > 0 and not _is_url_delimiter(text[i - 1]):
        return None
    return i


def find_attachment_urls(text: str | None) -> list[str]:
    """Return attachment-URL-shaped candidates in *text*, in order, de-duplicated.

    Handles HTML and markdown field text with the same pass — both embed the
    bare URL — and all three URL shapes the product is known to emit: absolute
    ``https://…``, protocol-relative ``//host/…`` and site-relative ``/{org}/…``.
    Anything else in front of the route (a plain ``http://`` URL, a bare
    hostname) is not returned, matching what ``parse_attachment_url`` would
    refuse anyway.

    HTML entities are decoded on each candidate; see ``_unescape_html_entities``
    for the trade-off that involves.

    **Every returned string is untrusted**: it came out of user-authored work
    item content. Put it through ``parse_attachment_url`` before using it for
    anything, and never echo a rejected one back to a model (that just relays an
    injected payload).
    """
    if not text:
        return []

    seen: set[str] = set()
    found: list[str] = []
    for match in _INLINE_ATTACHMENT_ROUTE_RE.finditer(text):
        start = _token_start(text, match.start())
        if start is None:
            continue
        candidate = _unescape_html_entities(text[start : match.end()])
        # The three shapes above, and nothing else. A token that does not begin
        # with '/' or 'https://' is not a URL this server will ever accept.
        if not (candidate.startswith("/") or candidate[:8].lower() == "https://"):
            continue
        if candidate not in seen:
            seen.add(candidate)
            found.append(candidate)
    return found


# ---------------------------------------------------------------------------
# Embed snippets
# ---------------------------------------------------------------------------

EMBED_USAGE = (
    "Paste 'markdown' into a large-text field written with format='markdown' "
    "(the default for devops_update_work_item / devops_create_work_item), or "
    "'html' when writing that field with format='html'. Mixing them renders the "
    "raw snippet as literal text."
)


def build_embed_snippets(url: str, file_name: str) -> dict[str, str]:
    """Build ready-to-paste markdown and HTML embed snippets for an attachment.

    *url* must be the URL Azure DevOps returned on the ``AttachmentReference``,
    used verbatim — it carries ``?fileName=`` and is mostly percent-encoded.
    "Mostly": an '&' inside the file name is emitted **bare** (observed live),
    which is precisely why the escaping below is not optional.

    Escaping here is load-bearing, not cosmetic:

    - The markdown alt text escapes ``[`` / ``]`` so a bracketed filename cannot
      terminate the link label early.
    - The markdown link destination percent-encodes space and parentheses; a
      literal ``(`` or ``)`` truncates a CommonMark link destination silently.
    - The HTML snippet runs both the URL and the alt through ``html.escape``
      with ``quote=True``. The URL contains ``?fileName=`` and may contain a
      bare ``&``, which starts an entity reference inside an attribute value.
    """
    alt = (file_name or "").replace("\r", "").replace("\n", "")
    markdown_alt = alt.replace("[", r"\[").replace("]", r"\]")
    markdown_url = url.replace(" ", "%20").replace("(", "%28").replace(")", "%29")
    return {
        "markdown": f"![{markdown_alt}]({markdown_url})",
        "html": f'<img src="{html.escape(url, quote=True)}" alt="{html.escape(alt, quote=True)}" />',
        "usage": EMBED_USAGE,
    }

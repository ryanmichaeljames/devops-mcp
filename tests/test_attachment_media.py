"""Unit tests for devops_mcp.attachment_media — the pure attachment helpers.

No HTTP, no filesystem, no MCP: this module mirrors the redaction.py boundary,
so everything here is a plain function call.

Three things are load-bearing rather than cosmetic and get the most coverage:

- Magic-byte sniffing, because on the upload path it is the guard that makes the
  extension allowlist mean anything (a renamed non-image must be rejected).
- parse_attachment_url, because a caller-supplied URL is attacker-influenceable
  and following it with a bearer token attached would be a credential compromise.
- Embed-snippet escaping, because an unescaped `&` or paren silently corrupts
  the snippet the agent pastes into a work item field.
"""

import time

import pytest

from devops_mcp.attachment_media import (
    ALLOWED_UPLOAD_EXTENSIONS,
    EMBED_USAGE,
    MAX_INLINE_IMAGE_BYTES,
    build_embed_snippets,
    detect_image_format,
    file_name_rejection_reason,
    find_attachment_urls,
    image_format_from_name,
    mime_type_for_format,
    parse_attachment_url,
    sniff_image_format,
)

# ---------------------------------------------------------------------------
# Fake constants — no real organizations, projects, or identifiers
# ---------------------------------------------------------------------------

FAKE_ORG = "fake-org"
FAKE_GUID = "a5cedde4-2dd5-4fcf-befe-fd0977dd3433"
# The upload response url is project-GUID-scoped, not org-scoped (live-verified).
FAKE_PROJECT_GUID = "0f1e2d3c-4b5a-4968-8776-655443332211"

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 32
GIF_BYTES = b"GIF89a" + b"\x00" * 32
WEBP_BYTES = b"RIFF" + b"\x24\x00\x00\x00" + b"WEBP" + b"\x00" * 32
PDF_BYTES = b"%PDF-1.7\n" + b"\x00" * 32


# ---------------------------------------------------------------------------
# sniff_image_format
# ---------------------------------------------------------------------------


class TestSniffImageFormat:
    @pytest.mark.parametrize(
        ("data", "expected"),
        [
            (PNG_BYTES, "png"),
            (JPEG_BYTES, "jpeg"),
            (GIF_BYTES, "gif"),
            (WEBP_BYTES, "webp"),
        ],
    )
    def test_recognised_signatures(self, data, expected):
        assert sniff_image_format(data) == expected

    def test_gif87a_also_recognised(self):
        assert sniff_image_format(b"GIF87a" + b"\x00" * 8) == "gif"

    def test_jpeg_never_reported_as_jpg(self):
        """'jpg' would produce the invalid MIME type image/jpg."""
        assert sniff_image_format(JPEG_BYTES) == "jpeg"

    @pytest.mark.parametrize(
        "data",
        [
            b"",
            PDF_BYTES,
            b"BM" + b"\x00" * 32,  # BMP — deliberately excluded
            b"II*\x00" + b"\x00" * 32,  # TIFF — deliberately excluded
            b"<svg xmlns='http://www.w3.org/2000/svg'/>",  # SVG — deliberately excluded
            b"-----BEGIN OPENSSH PRIVATE KEY-----\n",
            b"RIFF\x24\x00\x00\x00WAVE",  # RIFF container, but not WEBP
        ],
    )
    def test_unrecognised_returns_none(self, data):
        assert sniff_image_format(data) is None

    def test_truncated_png_signature_is_not_an_image(self):
        assert sniff_image_format(b"\x89PNG") is None


# ---------------------------------------------------------------------------
# image_format_from_name
# ---------------------------------------------------------------------------


class TestImageFormatFromName:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("screenshot.png", "png"),
            ("photo.jpg", "jpeg"),
            ("photo.jpeg", "jpeg"),
            ("anim.gif", "gif"),
            ("modern.webp", "webp"),
            ("SHOUTING.PNG", "png"),
            ("a.b.c.png", "png"),
        ],
    )
    def test_allowed_extensions(self, name, expected):
        assert image_format_from_name(name) == expected

    @pytest.mark.parametrize(
        "name",
        [None, "", "noextension", "trace.zip", "report.pdf", "notes.txt", "diagram.svg", "id_rsa", ".env"],
    )
    def test_disallowed_or_missing(self, name):
        assert image_format_from_name(name) is None

    def test_allowlist_matches_extension_map(self):
        assert ALLOWED_UPLOAD_EXTENSIONS == {".png", ".jpg", ".jpeg", ".gif", ".webp"}


class TestMimeTypeForFormat:
    def test_builds_image_mime(self):
        assert mime_type_for_format("png") == "image/png"
        assert mime_type_for_format("jpeg") == "image/jpeg"


# ---------------------------------------------------------------------------
# parse_attachment_url — the SSRF guard
# ---------------------------------------------------------------------------


class TestParseAttachmentUrlAccepts:
    def test_org_scoped_url(self):
        url = f"https://dev.azure.com/{FAKE_ORG}/_apis/wit/attachments/{FAKE_GUID}"
        assert parse_attachment_url(url, FAKE_ORG) == (FAKE_GUID, None)

    def test_file_name_is_extracted_from_query(self):
        url = (
            f"https://dev.azure.com/{FAKE_ORG}/_apis/wit/attachments/{FAKE_GUID}"
            "?fileName=screenshot.png"
        )
        assert parse_attachment_url(url, FAKE_ORG) == (FAKE_GUID, "screenshot.png")

    def test_project_scoped_url(self):
        url = f"https://dev.azure.com/{FAKE_ORG}/fake-project/_apis/wit/attachments/{FAKE_GUID}"
        assert parse_attachment_url(url, FAKE_ORG)[0] == FAKE_GUID

    def test_legacy_visualstudio_host(self):
        url = f"https://{FAKE_ORG}.visualstudio.com/_apis/wit/attachments/{FAKE_GUID}"
        assert parse_attachment_url(url, FAKE_ORG)[0] == FAKE_GUID

    def test_explicit_port_443_allowed(self):
        url = f"https://dev.azure.com:443/{FAKE_ORG}/_apis/wit/attachments/{FAKE_GUID}"
        assert parse_attachment_url(url, FAKE_ORG)[0] == FAKE_GUID

    def test_trailing_slash_allowed(self):
        url = f"https://dev.azure.com/{FAKE_ORG}/_apis/wit/attachments/{FAKE_GUID}/"
        assert parse_attachment_url(url, FAKE_ORG)[0] == FAKE_GUID

    def test_extra_query_parameters_are_ignored(self):
        url = (
            f"https://dev.azure.com/{FAKE_ORG}/_apis/wit/attachments/{FAKE_GUID}"
            "?fileName=a.png&download=false&api-version=7.1"
        )
        assert parse_attachment_url(url, FAKE_ORG) == (FAKE_GUID, "a.png")

    def test_host_and_org_matching_are_case_insensitive(self):
        url = f"https://DEV.AZURE.COM/{FAKE_ORG.upper()}/_apis/WIT/Attachments/{FAKE_GUID.upper()}"
        guid, _ = parse_attachment_url(url, FAKE_ORG)
        assert guid.lower() == FAKE_GUID

    def test_percent_encoded_org_segment_is_compared_decoded(self):
        url = f"https://dev.azure.com/fake%20org/_apis/wit/attachments/{FAKE_GUID}"
        assert parse_attachment_url(url, "fake org")[0] == FAKE_GUID

    def test_blank_file_name_query_returns_none(self):
        url = f"https://dev.azure.com/{FAKE_ORG}/_apis/wit/attachments/{FAKE_GUID}?fileName="
        assert parse_attachment_url(url, FAKE_ORG) == (FAKE_GUID, None)

    def test_project_guid_scoped_upload_response_url(self):
        """The shape the service really returns from an upload: {org}/{projectGuid}/…"""
        url = (
            f"https://dev.azure.com/{FAKE_ORG}/{FAKE_PROJECT_GUID}"
            f"/_apis/wit/attachments/{FAKE_GUID}?fileName=diagram.png"
        )
        assert parse_attachment_url(url, FAKE_ORG) == (FAKE_GUID, "diagram.png")

    def test_site_relative_url(self):
        """No host to validate — but the org still comes off the first segment."""
        url = f"/{FAKE_ORG}/_apis/wit/attachments/{FAKE_GUID}?fileName=shot.png"
        assert parse_attachment_url(url, FAKE_ORG) == (FAKE_GUID, "shot.png")

    def test_site_relative_project_scoped_url(self):
        url = f"/{FAKE_ORG}/{FAKE_PROJECT_GUID}/_apis/wit/attachments/{FAKE_GUID}"
        assert parse_attachment_url(url, FAKE_ORG) == (FAKE_GUID, None)

    def test_protocol_relative_url(self):
        url = f"//dev.azure.com/{FAKE_ORG}/_apis/wit/attachments/{FAKE_GUID}?fileName=shot.png"
        assert parse_attachment_url(url, FAKE_ORG) == (FAKE_GUID, "shot.png")

    def test_protocol_relative_legacy_host(self):
        url = f"//{FAKE_ORG}.visualstudio.com/_apis/wit/attachments/{FAKE_GUID}"
        assert parse_attachment_url(url, FAKE_ORG)[0] == FAKE_GUID


class TestParseAttachmentUrlFileName:
    """The fileName value is parsed by heuristic — see _file_name_from_query.

    Azure DevOps emits a *bare, unencoded* '&' inside the fileName value, so the
    query is ambiguous and parse_qs (which splits on every '&') truncates the
    name. These pin the observed shape and the edges around it.
    """

    def _url(self, query: str = "") -> str:
        base = f"https://dev.azure.com/{FAKE_ORG}/_apis/wit/attachments/{FAKE_GUID}"
        return f"{base}?{query}" if query else base

    def test_observed_bare_ampersand_in_file_name(self):
        """Verbatim from a live upload response: space is %20-encoded, '&' is not."""
        url = (
            f"https://dev.azure.com/{FAKE_ORG}/{FAKE_PROJECT_GUID}"
            f"/_apis/wit/attachments/{FAKE_GUID}?fileName=a%20&%20b%20(1).png"
        )
        assert parse_attachment_url(url, FAKE_ORG) == (FAKE_GUID, "a & b (1).png")

    def test_percent_encoded_ampersand_also_works(self):
        assert parse_attachment_url(self._url("fileName=a%20%26%20b.png"), FAKE_ORG)[1] == "a & b.png"

    def test_no_query_at_all(self):
        assert parse_attachment_url(self._url(), FAKE_ORG)[1] is None

    def test_file_name_absent_from_query(self):
        assert parse_attachment_url(self._url("download=false&api-version=7.1"), FAKE_ORG)[1] is None

    def test_key_lookalike_is_not_file_name(self):
        assert parse_attachment_url(self._url("xfileName=a.png"), FAKE_ORG)[1] is None

    def test_key_is_case_insensitive(self):
        assert parse_attachment_url(self._url("FILENAME=a.png"), FAKE_ORG)[1] == "a.png"

    def test_trailing_parameter_still_terminates_the_value(self):
        """A genuine following parameter must not be swallowed into the name."""
        assert parse_attachment_url(self._url("fileName=a.png&download=false"), FAKE_ORG)[1] == "a.png"

    def test_bare_ampersand_alongside_a_real_parameter(self):
        url = self._url("fileName=a%20&%20b.png&download=false&api-version=7.1")
        assert parse_attachment_url(url, FAKE_ORG)[1] == "a & b.png"

    def test_file_name_not_first_parameter(self):
        url = self._url("download=false&fileName=a%20&%20b.png")
        assert parse_attachment_url(url, FAKE_ORG)[1] == "a & b.png"

    def test_empty_value_before_another_parameter(self):
        assert parse_attachment_url(self._url("fileName=&download=false"), FAKE_ORG)[1] is None

    def test_whitespace_only_value(self):
        assert parse_attachment_url(self._url("fileName=%20%20"), FAKE_ORG)[1] is None

    def test_plus_is_a_literal_plus_not_a_space(self):
        """This producer percent-encodes space as %20, so '+' is not form encoding."""
        assert parse_attachment_url(self._url("fileName=a+b.png"), FAKE_ORG)[1] == "a+b.png"

    def test_ampersand_name_does_not_disturb_the_guid(self):
        """The GUID comes from the path; the query heuristic cannot affect it."""
        url = self._url("fileName=a%20&%20b%20(1).png")
        assert parse_attachment_url(url, FAKE_ORG)[0] == FAKE_GUID


class TestParseAttachmentUrlRejects:
    @pytest.mark.parametrize(
        ("url", "why"),
        [
            (f"http://dev.azure.com/{{org}}/_apis/wit/attachments/{FAKE_GUID}", "plain http"),
            (f"ftp://dev.azure.com/{{org}}/_apis/wit/attachments/{FAKE_GUID}", "non-http scheme"),
            (f"https://evil.example/_apis/wit/attachments/{FAKE_GUID}", "foreign host"),
            (f"https://dev.azure.com@evil.example/{{org}}/_apis/wit/attachments/{FAKE_GUID}", "userinfo"),
            (f"https://evil-dev.azure.com.attacker.test/{{org}}/_apis/wit/attachments/{FAKE_GUID}", "suffix trick"),
            (f"https://notdev.azure.com/{{org}}/_apis/wit/attachments/{FAKE_GUID}", "subdomain trick"),
            (f"https://dev.azure.com:8443/{{org}}/_apis/wit/attachments/{FAKE_GUID}", "non-443 port"),
            (f"https://{{org}}.visualstudio.com.evil.example/_apis/wit/attachments/{FAKE_GUID}", "legacy suffix trick"),
            ("https://dev.azure.com/{org}/_apis/wit/attachments/not-a-guid", "non-guid id"),
            ("https://dev.azure.com/{org}/_apis/wit/attachments/", "no id"),
            (f"https://dev.azure.com/{{org}}/_apis/git/repositories/{FAKE_GUID}", "wrong route"),
            (f"https://dev.azure.com/_apis/wit/attachments/{FAKE_GUID}", "no org segment"),
            ("", "empty"),
            ("   ", "whitespace"),
            ("not a url at all", "garbage"),
        ],
    )
    def test_rejected(self, url, why):
        with pytest.raises(ValueError):
            parse_attachment_url(url.replace("{org}", FAKE_ORG), FAKE_ORG)

    def test_org_mismatch_is_refused(self):
        """A token minted for one org must never travel to a URL naming another."""
        url = f"https://dev.azure.com/other-org/_apis/wit/attachments/{FAKE_GUID}"
        with pytest.raises(ValueError) as exc_info:
            parse_attachment_url(url, FAKE_ORG)
        assert "other-org" in str(exc_info.value)
        assert FAKE_ORG in str(exc_info.value)

    def test_legacy_host_org_mismatch_is_refused(self):
        url = f"https://other-org.visualstudio.com/_apis/wit/attachments/{FAKE_GUID}"
        with pytest.raises(ValueError):
            parse_attachment_url(url, FAKE_ORG)

    def test_error_message_is_actionable(self):
        with pytest.raises(ValueError) as exc_info:
            parse_attachment_url(f"https://evil.example/_apis/wit/attachments/{FAKE_GUID}", FAKE_ORG)
        assert "attachment_id" in str(exc_info.value)

    @pytest.mark.parametrize(
        ("url", "why"),
        [
            (f"/other-org/_apis/wit/attachments/{FAKE_GUID}", "site-relative, different org"),
            (f"/_apis/wit/attachments/{FAKE_GUID}", "site-relative, no org segment"),
            (f"//dev.azure.com/_apis/wit/attachments/{FAKE_GUID}", "protocol-relative, no org"),
            (f"//evil.example/{{org}}/_apis/wit/attachments/{FAKE_GUID}", "protocol-relative, foreign host"),
            (f"//dev.azure.com@evil.example/{{org}}/_apis/wit/attachments/{FAKE_GUID}", "protocol-relative userinfo"),
            (f"//dev.azure.com:8443/{{org}}/_apis/wit/attachments/{FAKE_GUID}", "protocol-relative non-443 port"),
            (f"/{{org}}/_apis/git/repositories/{FAKE_GUID}", "site-relative, wrong route"),
            ("/{org}/_apis/wit/attachments/not-a-guid", "site-relative, non-guid id"),
        ],
    )
    def test_relative_shapes_are_still_validated(self, url, why):
        """Relaxing the *shape* must not relax any of the checks."""
        with pytest.raises(ValueError):
            parse_attachment_url(url.replace("{org}", FAKE_ORG), FAKE_ORG)

    def test_site_relative_org_mismatch_names_both_orgs(self):
        with pytest.raises(ValueError) as exc_info:
            parse_attachment_url(f"/other-org/_apis/wit/attachments/{FAKE_GUID}", FAKE_ORG)
        assert "other-org" in str(exc_info.value)
        assert FAKE_ORG in str(exc_info.value)


class TestParseAttachmentUrlFileNameHygiene:
    """A file name taken off a URL runs the same check as the model field.

    Not currently exploitable — the value is percent-encoded before it reaches
    any URL and json.dumps escapes control characters — but the two ways a file
    name enters these tools must not disagree about what is acceptable. The
    *policies* differ deliberately: the model raises, a URL-derived hint is
    dropped so the GUID (which is what a download depends on) stays usable.
    """

    def _url(self, query: str) -> str:
        return f"https://dev.azure.com/{FAKE_ORG}/_apis/wit/attachments/{FAKE_GUID}?{query}"

    @pytest.mark.parametrize("encoded", ["a%0Ab.png", "a%0Db.png", "a%00b.png", "a%09b.png"])
    def test_control_characters_are_dropped_not_returned(self, encoded):
        guid, file_name = parse_attachment_url(self._url(f"fileName={encoded}"), FAKE_ORG)
        assert guid == FAKE_GUID, "dropping the hint must never cost the attachment"
        assert file_name is None

    def test_overlong_file_name_is_dropped(self):
        guid, file_name = parse_attachment_url(self._url(f"fileName={'x' * 300}.png"), FAKE_ORG)
        assert guid == FAKE_GUID
        assert file_name is None

    def test_a_name_at_the_limit_survives(self):
        name = "x" * 251 + ".png"  # exactly 255
        assert parse_attachment_url(self._url(f"fileName={name}"), FAKE_ORG)[1] == name

    def test_the_predicate_is_the_one_the_models_use(self):
        assert file_name_rejection_reason("ok.png") is None
        assert "control characters" in (file_name_rejection_reason("a\nb.png") or "")
        assert "255" in (file_name_rejection_reason("x" * 256) or "")
        assert file_name_rejection_reason("") is not None
        assert file_name_rejection_reason(None) is not None


# ---------------------------------------------------------------------------
# build_embed_snippets
# ---------------------------------------------------------------------------

_PLAIN_URL = f"https://dev.azure.com/{FAKE_ORG}/_apis/wit/attachments/{FAKE_GUID}?fileName=diagram.png"


class TestBuildEmbedSnippets:
    def test_plain_snippets(self):
        snippets = build_embed_snippets(_PLAIN_URL, "diagram.png")
        assert snippets["markdown"] == f"![diagram.png]({_PLAIN_URL})"
        assert snippets["html"] == (
            f'<img src="{_PLAIN_URL}" alt="diagram.png" />'
        )
        assert snippets["usage"] == EMBED_USAGE

    def test_ampersand_in_url_is_entity_escaped_in_html_only(self):
        """A bare & inside an attribute value starts an entity reference."""
        url = f"{_PLAIN_URL}&download=false"
        snippets = build_embed_snippets(url, "diagram.png")
        assert "&amp;download=false" in snippets["html"]
        # Markdown link destinations do not entity-escape; the raw & must survive.
        assert "&download=false" in snippets["markdown"]
        assert "&amp;" not in snippets["markdown"]

    def test_markdown_percent_encodes_space_and_parentheses(self):
        url = "https://dev.azure.com/o/_apis/wit/attachments/g?fileName=my file (1).png"
        snippets = build_embed_snippets(url, "my file (1).png")
        assert snippets["markdown"] == (
            "![my file (1).png](https://dev.azure.com/o/_apis/wit/attachments/g"
            "?fileName=my%20file%20%281%29.png)"
        )

    def test_html_escapes_quotes_in_url_and_alt(self):
        url = 'https://dev.azure.com/o/_apis/wit/attachments/g?fileName="x".png'
        snippets = build_embed_snippets(url, '"x".png')
        assert '&quot;x&quot;.png' in snippets["html"]
        assert snippets["html"].count('"') == 4  # only the two attribute delimiters

    def test_html_escapes_angle_brackets_in_alt(self):
        snippets = build_embed_snippets(_PLAIN_URL, "<script>.png")
        assert "<script>" not in snippets["html"]
        assert "&lt;script&gt;.png" in snippets["html"]

    def test_markdown_escapes_brackets_in_alt(self):
        snippets = build_embed_snippets(_PLAIN_URL, "shot [1].png")
        assert snippets["markdown"].startswith(r"![shot \[1\].png](")

    def test_html_alt_carries_no_markdown_escapes(self):
        """Backslash bracket escaping is markdown syntax; it is noise in HTML."""
        snippets = build_embed_snippets(_PLAIN_URL, "shot [1].png")
        assert 'alt="shot [1].png"' in snippets["html"]

    def test_newlines_are_stripped_from_alt(self):
        snippets = build_embed_snippets(_PLAIN_URL, "line1\r\nline2.png")
        assert "\n" not in snippets["markdown"]
        assert "\r" not in snippets["markdown"]
        assert snippets["markdown"].startswith("![line1line2.png](")

    def test_all_three_keys_present(self):
        assert set(build_embed_snippets(_PLAIN_URL, "a.png")) == {"markdown", "html", "usage"}


# ---------------------------------------------------------------------------
# detect_image_format
# ---------------------------------------------------------------------------


class TestDetectImageFormat:
    def test_magic_bytes_win(self):
        assert detect_image_format(PNG_BYTES, "anything.jpg") == ("png", "magic_bytes")

    def test_extension_is_the_fallback_only(self):
        assert detect_image_format(b"\x00\x01\x02\x03", "mystery.gif") == ("gif", "file_extension")

    def test_a_path_works_as_well_as_a_bare_name(self):
        assert detect_image_format(b"\x00\x01", "/docs/diagrams/arch.webp") == (
            "webp",
            "file_extension",
        )

    def test_neither_signature_nor_extension(self):
        assert detect_image_format(PDF_BYTES, "report.pdf") == (None, None)

    def test_no_name_supplied(self):
        assert detect_image_format(JPEG_BYTES) == ("jpeg", "magic_bytes")
        assert detect_image_format(b"\x00\x01") == (None, None)

    def test_cap_leaves_headroom_under_the_five_mb_json_ceiling(self):
        """Base64 inflates by 4/3; the cap must land at or under 5,000,000 chars."""
        assert MAX_INLINE_IMAGE_BYTES * 4 / 3 <= 5_000_000


# ---------------------------------------------------------------------------
# find_attachment_urls — one regex, both field formats
# ---------------------------------------------------------------------------

_INLINE_URL = f"https://dev.azure.com/{FAKE_ORG}/_apis/wit/attachments/{FAKE_GUID}"


class TestFindAttachmentUrls:
    def test_html_img_src(self):
        text = f'<div><img src="{_INLINE_URL}?fileName=shot.png" alt="shot" /></div>'
        assert find_attachment_urls(text) == [f"{_INLINE_URL}?fileName=shot.png"]

    def test_html_single_quoted_src(self):
        text = f"<img src='{_INLINE_URL}'>"
        assert find_attachment_urls(text) == [_INLINE_URL]

    def test_markdown_image(self):
        text = f"Repro:\n\n![shot.png]({_INLINE_URL}?fileName=shot.png)\n"
        assert find_attachment_urls(text) == [f"{_INLINE_URL}?fileName=shot.png"]

    def test_markdown_percent_encoded_parens_survive(self):
        url = f"{_INLINE_URL}?fileName=my%20file%20%281%29.png"
        assert find_attachment_urls(f"![x]({url})") == [url]

    def test_project_scoped_url_is_found(self):
        url = (
            f"https://dev.azure.com/{FAKE_ORG}/{FAKE_PROJECT_GUID}"
            f"/_apis/wit/attachments/{FAKE_GUID}?fileName=a.png"
        )
        assert find_attachment_urls(f"<img src=\"{url}\">") == [url]

    def test_trailing_sentence_punctuation_is_not_swallowed(self):
        assert find_attachment_urls(f"see {_INLINE_URL}.") == [_INLINE_URL]

    def test_repeated_identical_url_reported_once(self):
        text = f"{_INLINE_URL} and again {_INLINE_URL}"
        assert find_attachment_urls(text) == [_INLINE_URL]

    def test_multiple_distinct_urls_in_order(self):
        second = f"https://dev.azure.com/{FAKE_ORG}/_apis/wit/attachments/{FAKE_PROJECT_GUID}"
        text = f"<img src='{_INLINE_URL}'><img src='{second}'>"
        assert find_attachment_urls(text) == [_INLINE_URL, second]

    def test_foreign_host_is_still_returned_as_a_candidate(self):
        """This is a finder, not a validator — parse_attachment_url is the guard."""
        hostile = f"https://evil.example/_apis/wit/attachments/{FAKE_GUID}"
        assert find_attachment_urls(f"<img src='{hostile}'>") == [hostile]
        with pytest.raises(ValueError):
            parse_attachment_url(hostile, FAKE_ORG)

    def test_non_attachment_urls_are_ignored(self):
        text = (
            f"https://dev.azure.com/{FAKE_ORG}/_apis/wit/workitems/42 "
            "https://example.com/pic.png "
            f"http://dev.azure.com/{FAKE_ORG}/_apis/wit/attachments/{FAKE_GUID}"
        )
        # The plain-http one is not matched at all: the pattern requires https.
        assert find_attachment_urls(text) == []

    def test_malformed_guid_is_not_matched(self):
        assert find_attachment_urls(f"https://dev.azure.com/{FAKE_ORG}/_apis/wit/attachments/nope") == []

    def test_empty_and_none(self):
        assert find_attachment_urls("") == []
        assert find_attachment_urls(None) == []

    def test_long_hostile_text_terminates_promptly(self):
        """Bounded repetition: a pathological field must not become a CPU sink."""
        text = "https://" + ("a" * 200_000)
        assert find_attachment_urls(text) == []

    @pytest.mark.parametrize(
        "text",
        [
            "/" * 200_000,
            " /" * 100_000,
            "/x" * 100_000,
            "<img src='/a/_apis/wit/attachments/" * 20_000,
        ],
        ids=["all-slashes", "space-slash", "slash-x", "truncated-routes"],
    )
    def test_hostile_slash_heavy_text_terminates_promptly(self, text):
        """The relative shapes mean a bare '/' can start a candidate. A regex
        alternation would then retry a permissive prefix at every slash in a
        200 KB description; the left-walk is linear and stays bounded."""
        start = time.perf_counter()
        find_attachment_urls(text)
        assert time.perf_counter() - start < 2.0

    def test_a_prefix_longer_than_the_bound_is_not_a_candidate(self):
        """Stopping mid-token would invent a leading '/' that starts nothing."""
        text = ("a" * 2_000) + f"/_apis/wit/attachments/{FAKE_GUID}"
        assert find_attachment_urls(text) == []


class TestFindAttachmentUrlsRelativeShapes:
    """Site-relative and protocol-relative `<img src>` values.

    Every row measured live against the finder. The first three parsed already;
    the last two returned **zero** candidates and were therefore silently
    missed — not even counted in `rejected_urls`, so the caller could not tell
    discovery had failed. A miss is the worst outcome of the three, which is why
    these are found and then left for `parse_attachment_url` to judge.
    """

    def test_site_relative_is_found(self):
        url = f"/{FAKE_ORG}/_apis/wit/attachments/{FAKE_GUID}?fileName=a.png"
        assert find_attachment_urls(f'<img src="{url}">') == [url]
        assert parse_attachment_url(url, FAKE_ORG) == (FAKE_GUID, "a.png")

    def test_site_relative_project_scoped_is_found(self):
        url = f"/{FAKE_ORG}/{FAKE_PROJECT_GUID}/_apis/wit/attachments/{FAKE_GUID}"
        assert find_attachment_urls(f"<img src='{url}'>") == [url]
        assert parse_attachment_url(url, FAKE_ORG)[0] == FAKE_GUID

    def test_protocol_relative_is_found(self):
        url = f"//dev.azure.com/{FAKE_ORG}/_apis/wit/attachments/{FAKE_GUID}"
        assert find_attachment_urls(f'<img src="{url}">') == [url]
        assert parse_attachment_url(url, FAKE_ORG)[0] == FAKE_GUID

    def test_site_relative_naming_another_org_is_found_then_refused(self):
        """Found so it can be *counted*; refused so it is never used."""
        url = f"/other-org/_apis/wit/attachments/{FAKE_GUID}"
        assert find_attachment_urls(f'<img src="{url}">') == [url]
        with pytest.raises(ValueError):
            parse_attachment_url(url, FAKE_ORG)

    def test_site_relative_without_an_org_is_found_then_refused(self):
        url = f"/_apis/wit/attachments/{FAKE_GUID}"
        assert find_attachment_urls(f'<img src="{url}">') == [url]
        with pytest.raises(ValueError):
            parse_attachment_url(url, FAKE_ORG)

    def test_plain_http_is_still_not_a_candidate(self):
        """The left-walk must not turn `http://host/org/…` into a site-relative
        `/org/…` by starting after the scheme."""
        assert find_attachment_urls(f"http://dev.azure.com/{FAKE_ORG}/_apis/wit/attachments/{FAKE_GUID}") == []

    def test_a_bare_path_with_no_leading_slash_is_not_a_candidate(self):
        assert find_attachment_urls(f"foo/bar/_apis/wit/attachments/{FAKE_GUID}") == []

    def test_absolute_url_is_matched_whole_not_as_its_own_suffix(self):
        url = f"https://dev.azure.com/{FAKE_ORG}/_apis/wit/attachments/{FAKE_GUID}"
        assert find_attachment_urls(f"<img src='{url}'>") == [url]


class TestFindAttachmentUrlsHtmlEntities:
    """An html-format field escapes the URL it stores; the entities must go.

    Live on a work item whose html description stores
    `?fileName=a%20&amp;%20b%20%281%29.png`, the file name was reported as the
    literal "a &amp; b (1).png" and then percent-encoded into the rebuilt URL as
    `%26amp%3B`. Harmless for the download (the GUID comes from the path) but
    wrong on display.
    """

    def test_amp_entity_is_decoded(self):
        stored = (
            f"https://dev.azure.com/{FAKE_ORG}/_apis/wit/attachments/{FAKE_GUID}"
            "?fileName=a%20&amp;%20b%20%281%29.png"
        )
        (candidate,) = find_attachment_urls(f'<img src="{stored}">')
        assert "&amp;" not in candidate
        assert parse_attachment_url(candidate, FAKE_ORG) == (FAKE_GUID, "a & b (1).png")

    @pytest.mark.parametrize(
        ("entity", "decoded"),
        [("&amp;", "&"), ("&#38;", "&"), ("&#x26;", "&"), ("&#39;", "'"), ("&quot;", '"')],
    )
    def test_the_entities_html_escaping_produces_are_decoded(self, entity, decoded):
        stored = (
            f"https://dev.azure.com/{FAKE_ORG}/_apis/wit/attachments/{FAKE_GUID}"
            f"?fileName=a{entity}b.png"
        )
        assert find_attachment_urls(stored) == [stored.replace(entity, decoded)]

    def test_decoding_is_one_pass_so_a_double_escape_is_not_collapsed(self):
        stored = (
            f"https://dev.azure.com/{FAKE_ORG}/_apis/wit/attachments/{FAKE_GUID}"
            "?fileName=a&amp;amp;b.png"
        )
        assert parse_attachment_url(find_attachment_urls(stored)[0], FAKE_ORG)[1] == "a&amp;b.png"

    def test_semicolonless_legacy_entities_are_left_alone(self):
        """html.unescape() would turn "a&notb.png" into "a¬b.png". This is why
        the decoder is an explicit table of well-formed references instead."""
        stored = (
            f"https://dev.azure.com/{FAKE_ORG}/_apis/wit/attachments/{FAKE_GUID}"
            "?fileName=a&notb.png"
        )
        assert find_attachment_urls(stored) == [stored]
        assert parse_attachment_url(stored, FAKE_ORG)[1] == "a&notb.png"

    def test_the_guid_is_never_affected(self):
        stored = (
            f"https://dev.azure.com/{FAKE_ORG}/_apis/wit/attachments/{FAKE_GUID}"
            "?fileName=a%20&amp;%20b.png"
        )
        assert parse_attachment_url(find_attachment_urls(stored)[0], FAKE_ORG)[0] == FAKE_GUID

"""Unit tests for the work item attachment tools.

All HTTP is intercepted with an httpx.AsyncBaseTransport stub — no network, no
credentials, no real organization or project names.

Three groups of assertions here are load-bearing rather than nice-to-have:

1. **structured_output=False.** Removing it from the decorator does not fail
   quietly — the SDK cannot derive an output schema for `Image`. The tests below
   pin both halves of that: that the registered tool really produces an
   ImageContent block through the SDK's own convert_result path, and that the
   two ways of getting it wrong (dropping the flag, or "correcting" the
   annotation to `-> str`) each blow up.

2. **SSRF rejection asserts `transport.requests == []` and that build_headers
   was never awaited** — i.e. no bearer token was ever minted, let alone sent.
   Merely returning an error would not prove that.

3. **Magic-byte verification defeats a renamed non-image file** on the upload
   path, with zero requests issued. Without it the extension allowlist is
   decorative: any file can be renamed .png.
"""

import base64
import json
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.tools.base import Tool
from mcp.server.mcpserver.utilities.types import Image
from pydantic import ValidationError
from pydantic.errors import PydanticSchemaGenerationError

import devops_mcp.server  # noqa: F401 — side-effect: registers every tool
import devops_mcp.tools.attachments as attachments_module
from devops_mcp._app import mcp
from devops_mcp.models import GetWorkItemAttachmentInput, UploadWorkItemAttachmentInput
from devops_mcp.tools.attachments import (
    devops_get_work_item_attachment,
    devops_upload_work_item_attachment,
)

# ---------------------------------------------------------------------------
# Fake constants — no real organizations, projects, or identifiers
# ---------------------------------------------------------------------------

FAKE_ORG = "fake-org"
FAKE_PROJECT = "fake-project"
FAKE_GUID = "a5cedde4-2dd5-4fcf-befe-fd0977dd3433"
FAKE_UPLOAD_GUID = "bb1e6c14-9f4a-4d1e-9a0d-2f8b6b3c5a11"
# The upload response url is project-GUID-scoped, not org-scoped (live-verified).
FAKE_PROJECT_GUID = "0f1e2d3c-4b5a-4968-8776-655443332211"

API_VERSION = "7.2-preview.4"

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4
JPEG_BYTES = b"\xff\xd8\xff\xe0" + bytes(range(256)) * 4
GIF_BYTES = b"GIF89a" + bytes(range(256)) * 4
WEBP_BYTES = b"RIFF" + b"\x24\x00\x00\x00" + b"WEBP" + bytes(range(256)) * 4
PDF_BYTES = b"%PDF-1.7\n" + bytes(range(256)) * 4
TEXT_BYTES = b"-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEA\n"


def _attachment_url(org: str = FAKE_ORG, guid: str = FAKE_GUID) -> str:
    return f"https://dev.azure.com/{org}/_apis/wit/attachments/{guid}"


# ---------------------------------------------------------------------------
# Transport plumbing (same shape as tests/test_work_item_field_format.py)
# ---------------------------------------------------------------------------


class CapturingTransport(httpx.AsyncBaseTransport):
    """Intercept every HTTP request; dispatch to a handler."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self._handler = handler
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)


def _make_handler(
    *,
    download_body: bytes = PNG_BYTES,
    download_status: int = 200,
    upload_status: int = 201,
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/wit/attachments/" in req.url.path:
            if download_status != 200:
                return httpx.Response(
                    status_code=download_status,
                    headers={"Content-Type": "application/json"},
                    content=json.dumps({
                        "message": "TF401232: Attachment not found.",
                        "typeKey": "AttachmentNotFoundException",
                    }).encode(),
                    request=req,
                )
            return httpx.Response(
                status_code=200,
                # The generic type the live service returns when the request
                # carries no fileName (with one it returns a real image/png).
                # Either way the tool must never branch on this header.
                headers={"Content-Type": "application/octet-stream"},
                content=download_body,
                request=req,
            )
        if req.method == "POST" and req.url.path.endswith("/wit/attachments"):
            file_name = dict(req.url.params).get("fileName", "upload.png")
            return httpx.Response(
                status_code=upload_status,
                headers={"Content-Type": "application/json"},
                content=json.dumps({
                    "id": FAKE_UPLOAD_GUID,
                    # Project-GUID-scoped, as the live service really answers —
                    # not the org-scoped shape the published samples show.
                    "url": (
                        f"https://dev.azure.com/{FAKE_ORG}/{FAKE_PROJECT_GUID}"
                        f"/_apis/wit/attachments/{FAKE_UPLOAD_GUID}?fileName={file_name}"
                    ),
                }).encode(),
                request=req,
            )
        raise AssertionError(f"Unexpected request: {req.method} {req.url}")

    return handler


def _make_ctx(transport: CapturingTransport) -> MagicMock:
    app_ctx = MagicMock()
    app_ctx.organization = FAKE_ORG
    app_ctx.project = FAKE_PROJECT
    app_ctx.http_client = httpx.AsyncClient(transport=transport)
    mcp_ctx = MagicMock()
    mcp_ctx.request_context.lifespan_context = app_ctx
    return mcp_ctx


async def _fake_build_headers(app_ctx, *, include_content_type: bool = False, extra=None) -> dict:
    """Stand-in for client.build_headers with the same contract, minus the token call."""
    headers = {"Authorization": "Bearer fake-token", "Accept": "application/json"}
    if include_content_type:
        headers["Content-Type"] = "application/json"
    if extra:
        headers.update(extra)
    return headers


@pytest.fixture()
def build_headers_mock():
    """AsyncMock standing in for build_headers, so token minting is observable."""
    return AsyncMock(side_effect=_fake_build_headers)


@pytest.fixture()
def auth_patches(build_headers_mock):
    """Start/stop the auth + resolution patches around a tool call."""
    patches = [
        patch("devops_mcp.tools.attachments.build_headers", new=build_headers_mock),
        patch("devops_mcp.tools.attachments.resolve_org", return_value=FAKE_ORG),
        patch("devops_mcp.tools.attachments.resolve_project", return_value=FAKE_PROJECT),
    ]
    for p in patches:
        p.start()
    try:
        yield
    finally:
        for p in patches:
            p.stop()


def _blocks_to_metadata(blocks: list) -> dict:
    """Element 0 of a media tool's return is always the JSON metadata string."""
    assert isinstance(blocks[0], str), f"Element 0 must be a JSON string, got {type(blocks[0])}"
    return json.loads(blocks[0])


# ---------------------------------------------------------------------------
# Download — image classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected_format", "expected_mime"),
    [
        (PNG_BYTES, "png", "image/png"),
        (JPEG_BYTES, "jpeg", "image/jpeg"),
        (GIF_BYTES, "gif", "image/gif"),
        (WEBP_BYTES, "webp", "image/webp"),
    ],
)
async def test_download_returns_image_block_per_format(auth_patches, body, expected_format, expected_mime):
    transport = CapturingTransport(_make_handler(download_body=body))
    blocks = await devops_get_work_item_attachment(
        GetWorkItemAttachmentInput(attachment_id=FAKE_GUID), _make_ctx(transport)
    )

    assert len(blocks) == 2
    metadata = _blocks_to_metadata(blocks)
    assert metadata["is_image"] is True
    assert metadata["content_type"] == expected_mime
    assert metadata["detected_by"] == "magic_bytes"
    assert metadata["attachment_id"] == FAKE_GUID
    assert metadata["size_bytes"] == len(body)

    image = blocks[1]
    assert isinstance(image, Image)
    assert image._mime_type == expected_mime
    content = image.to_image_content()
    assert content.mime_type == expected_mime
    assert base64.b64decode(content.data) == body


async def test_magic_bytes_beat_a_lying_file_name(auth_patches):
    """A JPEG served under a .png name must be reported as image/jpeg."""
    transport = CapturingTransport(_make_handler(download_body=JPEG_BYTES))
    blocks = await devops_get_work_item_attachment(
        GetWorkItemAttachmentInput(attachment_id=FAKE_GUID, file_name="screenshot.png"),
        _make_ctx(transport),
    )

    metadata = _blocks_to_metadata(blocks)
    assert metadata["content_type"] == "image/jpeg"
    assert metadata["detected_by"] == "magic_bytes"
    assert blocks[1]._mime_type == "image/jpeg"


async def test_non_image_returns_metadata_only_and_no_base64(auth_patches):
    """A PDF is a legitimate outcome, not an error — but its bytes are token burn."""
    transport = CapturingTransport(_make_handler(download_body=PDF_BYTES))
    blocks = await devops_get_work_item_attachment(
        GetWorkItemAttachmentInput(attachment_id=FAKE_GUID, file_name="report.pdf"),
        _make_ctx(transport),
    )

    assert len(blocks) == 1
    metadata = _blocks_to_metadata(blocks)
    assert metadata["is_image"] is False
    assert "error" not in metadata
    assert metadata["url"].endswith("?fileName=report.pdf")
    assert base64.b64encode(PDF_BYTES).decode() not in blocks[0]


async def test_extension_fallback_when_signature_is_inconclusive(auth_patches):
    """Unrecognised bytes under an image name fall back to the extension."""
    transport = CapturingTransport(_make_handler(download_body=b"\x00\x01\x02\x03" * 16))
    blocks = await devops_get_work_item_attachment(
        GetWorkItemAttachmentInput(attachment_id=FAKE_GUID, file_name="mystery.gif"),
        _make_ctx(transport),
    )

    metadata = _blocks_to_metadata(blocks)
    assert metadata["detected_by"] == "file_extension"
    assert metadata["content_type"] == "image/gif"


async def test_image_over_inline_cap_is_an_error_with_both_sizes(auth_patches):
    oversized = b"\x89PNG\r\n\x1a\n" + b"\x00" * (attachments_module._MAX_IMAGE_BYTES + 1)
    transport = CapturingTransport(_make_handler(download_body=oversized))
    blocks = await devops_get_work_item_attachment(
        GetWorkItemAttachmentInput(attachment_id=FAKE_GUID, file_name="huge.png"),
        _make_ctx(transport),
    )

    assert len(blocks) == 1
    metadata = _blocks_to_metadata(blocks)
    assert metadata["error"] is True
    assert metadata["size_bytes"] == len(oversized)
    assert metadata["max_bytes"] == attachments_module._MAX_IMAGE_BYTES
    assert f"{len(oversized):,}" in metadata["message"]
    assert f"{attachments_module._MAX_IMAGE_BYTES:,}" in metadata["message"]
    assert metadata["url"]


# ---------------------------------------------------------------------------
# Download — request shape
# ---------------------------------------------------------------------------


async def test_download_request_is_org_scoped_with_explicit_api_version(auth_patches):
    transport = CapturingTransport(_make_handler())
    await devops_get_work_item_attachment(
        GetWorkItemAttachmentInput(attachment_id=FAKE_GUID), _make_ctx(transport)
    )

    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.method == "GET"
    assert request.url.path == f"/{FAKE_ORG}/_apis/wit/attachments/{FAKE_GUID}"
    # build_params() would have injected 7.1 here.
    assert dict(request.url.params)["api-version"] == API_VERSION
    assert "fileName" not in dict(request.url.params)


async def test_url_input_is_rebuilt_not_followed(auth_patches):
    """The supplied URL is parsed and discarded; the request is rebuilt."""
    transport = CapturingTransport(_make_handler())
    supplied = (
        f"https://dev.azure.com/{FAKE_ORG}/fake-other-project/_apis/wit/attachments/"
        f"{FAKE_GUID}?fileName=pasted%20image.png&download=false"
    )
    blocks = await devops_get_work_item_attachment(
        GetWorkItemAttachmentInput(url=supplied), _make_ctx(transport)
    )

    request = transport.requests[0]
    # Project segment and download=false from the supplied URL are gone.
    assert request.url.path == f"/{FAKE_ORG}/_apis/wit/attachments/{FAKE_GUID}"
    params = dict(request.url.params)
    assert params["fileName"] == "pasted image.png"
    assert params["api-version"] == API_VERSION
    assert "download" not in params
    assert _blocks_to_metadata(blocks)["is_image"] is True


async def test_bare_ampersand_in_the_url_file_name_survives(auth_patches):
    """Regression: parse_qs split on the service's own unencoded '&' and reported "a".

    The url here is the shape a live upload really returns — project-GUID-scoped,
    space percent-encoded, ampersand not.
    """
    transport = CapturingTransport(_make_handler())
    supplied = (
        f"https://dev.azure.com/{FAKE_ORG}/{FAKE_PROJECT_GUID}/_apis/wit/attachments/"
        f"{FAKE_GUID}?fileName=a%20&%20b%20(1).png"
    )
    blocks = await devops_get_work_item_attachment(
        GetWorkItemAttachmentInput(url=supplied), _make_ctx(transport)
    )

    assert dict(transport.requests[0].url.params)["fileName"] == "a & b (1).png"
    metadata = _blocks_to_metadata(blocks)
    assert metadata["file_name"] == "a & b (1).png"
    # The canonical url must re-encode it, so round-tripping it is lossless.
    assert metadata["url"] == (
        f"https://dev.azure.com/{FAKE_ORG}/_apis/wit/attachments/{FAKE_GUID}"
        "?fileName=a%20%26%20b%20%281%29.png"
    )


async def test_explicit_file_name_overrides_the_url_query(auth_patches):
    transport = CapturingTransport(_make_handler())
    await devops_get_work_item_attachment(
        GetWorkItemAttachmentInput(url=f"{_attachment_url()}?fileName=from-url.png", file_name="override.png"),
        _make_ctx(transport),
    )

    assert dict(transport.requests[0].url.params)["fileName"] == "override.png"


# ---------------------------------------------------------------------------
# Download — SSRF guard (no request, and no token minted)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("bad_url", "why"),
    [
        (f"https://evil.example/_apis/wit/attachments/{FAKE_GUID}", "foreign host"),
        (f"https://dev.azure.com@evil.example/{FAKE_ORG}/_apis/wit/attachments/{FAKE_GUID}", "userinfo"),
        (f"http://dev.azure.com/{FAKE_ORG}/_apis/wit/attachments/{FAKE_GUID}", "plain http"),
        (f"https://dev.azure.com:8443/{FAKE_ORG}/_apis/wit/attachments/{FAKE_GUID}", "non-443 port"),
        (f"https://dev.azure.com/{FAKE_ORG}/_apis/wit/attachments/not-a-guid", "non-guid id"),
        (f"https://other-org.visualstudio.com/_apis/wit/attachments/{FAKE_GUID}", "foreign org, legacy host"),
        (f"https://dev.azure.com/other-org/_apis/wit/attachments/{FAKE_GUID}", "foreign org"),
    ],
)
async def test_ssrf_url_is_refused_before_any_token_is_minted(
    auth_patches, build_headers_mock, bad_url, why
):
    transport = CapturingTransport(_make_handler())
    blocks = await devops_get_work_item_attachment(
        GetWorkItemAttachmentInput(url=bad_url), _make_ctx(transport)
    )

    assert len(blocks) == 1
    assert _blocks_to_metadata(blocks)["error"] is True
    # The whole point: nothing left the process, and no credential was even
    # acquired — so a crafted URL cannot exfiltrate a bearer token.
    assert transport.requests == []
    assert build_headers_mock.await_count == 0


async def test_org_mismatch_message_names_both_orgs(auth_patches, build_headers_mock):
    transport = CapturingTransport(_make_handler())
    blocks = await devops_get_work_item_attachment(
        GetWorkItemAttachmentInput(url=_attachment_url(org="other-org")), _make_ctx(transport)
    )

    message = _blocks_to_metadata(blocks)["message"]
    assert "other-org" in message and FAKE_ORG in message
    assert transport.requests == []
    assert build_headers_mock.await_count == 0


# ---------------------------------------------------------------------------
# Download — API errors
# ---------------------------------------------------------------------------


async def test_download_404_returns_actionable_json_error(auth_patches):
    transport = CapturingTransport(_make_handler(download_status=404))
    blocks = await devops_get_work_item_attachment(
        GetWorkItemAttachmentInput(attachment_id=FAKE_GUID), _make_ctx(transport)
    )

    assert len(blocks) == 1
    metadata = _blocks_to_metadata(blocks)
    assert metadata["error"] is True
    assert FAKE_GUID in metadata["message"]
    assert FAKE_ORG in metadata["message"]


async def test_download_500_is_reported_not_raised(auth_patches):
    transport = CapturingTransport(_make_handler(download_status=500))
    blocks = await devops_get_work_item_attachment(
        GetWorkItemAttachmentInput(attachment_id=FAKE_GUID), _make_ctx(transport)
    )

    assert _blocks_to_metadata(blocks)["error"] is True


async def test_unresolvable_org_is_reported_as_json(build_headers_mock):
    """A ValueError from org resolution must not escape the tool."""
    transport = CapturingTransport(_make_handler())
    with patch("devops_mcp.tools.attachments.build_headers", new=build_headers_mock):
        app_ctx = MagicMock()
        app_ctx.organization = None
        app_ctx.http_client = httpx.AsyncClient(transport=transport)
        mcp_ctx = MagicMock()
        mcp_ctx.request_context.lifespan_context = app_ctx

        blocks = await devops_get_work_item_attachment(
            GetWorkItemAttachmentInput(attachment_id=FAKE_GUID), mcp_ctx
        )

    assert _blocks_to_metadata(blocks)["error"] is True
    assert transport.requests == []


# ---------------------------------------------------------------------------
# structured_output=False — the load-bearing registration detail
# ---------------------------------------------------------------------------


async def test_registered_tool_emits_a_real_image_content_block(auth_patches):
    """End to end through the SDK's own convert_result path.

    This is the assertion that would surface a Pydantic ValidationError if the
    registered tool ever acquired an output schema.
    """
    transport = CapturingTransport(_make_handler())
    tool = mcp._tool_manager.get_tool("devops_get_work_item_attachment")

    result = await tool.run(
        {"params": {"attachment_id": FAKE_GUID}}, _make_ctx(transport), convert_result=True
    )

    assert [block.type for block in result.content] == ["text", "image"]
    assert result.content[1].mime_type == "image/png"
    assert base64.b64decode(result.content[1].data) == PNG_BYTES
    assert json.loads(result.content[0].text)["is_image"] is True


def test_registered_tool_has_no_output_schema():
    tool = mcp._tool_manager.get_tool("devops_get_work_item_attachment")
    assert tool.fn_metadata.output_schema is None


def test_dropping_structured_output_false_breaks_registration():
    """Guard has teeth: without the flag the SDK cannot build the output model.

    `list[str | Image]` is not a schema-able annotation, and Tool.from_function
    raises while *creating* the wrapped model — outside the try/except that
    swallows schema-generation failures. So deleting `structured_output=False`
    from the decorator is an import-time crash, not a silent regression.
    """
    with pytest.raises(PydanticSchemaGenerationError):
        Tool.from_function(devops_get_work_item_attachment, name="canary_no_flag")


def test_str_annotation_would_reject_the_image_at_call_time():
    """The other way to get it wrong: 'correcting' the annotation to -> str.

    That produces an output schema, and the Image fails validation when the tool
    is called — long after registration succeeded.
    """

    async def canary(params: GetWorkItemAttachmentInput, ctx: Context) -> str:  # pragma: no cover
        ...

    tool = Tool.from_function(canary, name="canary_str_annotation")
    assert tool.fn_metadata.output_schema is not None
    with pytest.raises(ValidationError):
        tool.fn_metadata.convert_result(["{}", Image(data=PNG_BYTES, format="png")])


# ---------------------------------------------------------------------------
# Upload — request shape
# ---------------------------------------------------------------------------


def _write_image(tmp_path, name: str, body: bytes):
    path = tmp_path / name
    path.write_bytes(body)
    return path


async def test_upload_posts_project_scoped_octet_stream(auth_patches, tmp_path):
    transport = CapturingTransport(_make_handler())
    path = _write_image(tmp_path, "diagram.png", PNG_BYTES)

    result = json.loads(await devops_upload_work_item_attachment(
        UploadWorkItemAttachmentInput(file_path=str(path)), _make_ctx(transport)
    ))

    assert "error" not in result, result
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.method == "POST"
    assert request.url.path == f"/{FAKE_ORG}/{FAKE_PROJECT}/_apis/wit/attachments"
    params = dict(request.url.params)
    assert params["fileName"] == "diagram.png"
    assert params["api-version"] == API_VERSION
    assert request.headers["content-type"] == "application/octet-stream"
    assert request.content == PNG_BYTES

    assert result["id"] == FAKE_UPLOAD_GUID
    assert result["file_name"] == "diagram.png"
    assert result["size_bytes"] == len(PNG_BYTES)
    assert result["content_type"] == "image/png"
    assert set(result["embed"]) == {"markdown", "html", "usage"}


async def test_upload_forwards_area_path(auth_patches, tmp_path):
    transport = CapturingTransport(_make_handler())
    path = _write_image(tmp_path, "diagram.png", PNG_BYTES)

    await devops_upload_work_item_attachment(
        UploadWorkItemAttachmentInput(file_path=str(path), area_path="Area\\Sub"),
        _make_ctx(transport),
    )

    assert dict(transport.requests[0].url.params)["areaPath"] == "Area\\Sub"


async def test_data_base64_body_is_byte_identical_to_file_path_body(auth_patches, tmp_path):
    transport_file = CapturingTransport(_make_handler())
    transport_b64 = CapturingTransport(_make_handler())
    path = _write_image(tmp_path, "same.png", PNG_BYTES)

    await devops_upload_work_item_attachment(
        UploadWorkItemAttachmentInput(file_path=str(path)), _make_ctx(transport_file)
    )
    await devops_upload_work_item_attachment(
        UploadWorkItemAttachmentInput(
            data_base64=base64.b64encode(PNG_BYTES).decode(), file_name="same.png"
        ),
        _make_ctx(transport_b64),
    )

    assert transport_file.requests[0].content == transport_b64.requests[0].content == PNG_BYTES


async def test_upload_returns_exact_embed_snippets(auth_patches):
    """Character-for-character: markdown percent-encoding and HTML entity escaping."""
    transport = CapturingTransport(_make_handler())

    result = json.loads(await devops_upload_work_item_attachment(
        UploadWorkItemAttachmentInput(
            data_base64=base64.b64encode(PNG_BYTES).decode(),
            file_name="before & after (v2).png",
        ),
        _make_ctx(transport),
    ))

    returned_url = result["url"]
    assert "&" in returned_url and " " in returned_url  # the stub echoes fileName back raw
    assert result["embed"]["markdown"] == (
        f"![before & after (v2).png]({returned_url.replace(' ', '%20').replace('(', '%28').replace(')', '%29')})"
    )
    assert result["embed"]["html"] == (
        f'<img src="{returned_url.replace("&", "&amp;")}" alt="before &amp; after (v2).png" />'
    )
    assert "&amp;" in result["embed"]["html"]
    assert "%20" in result["embed"]["markdown"]


# ---------------------------------------------------------------------------
# Upload — guards (nothing may leave the machine)
# ---------------------------------------------------------------------------


async def test_renamed_non_image_is_rejected_with_zero_requests(auth_patches, build_headers_mock, tmp_path):
    """`cp ~/.ssh/id_rsa /tmp/x.png` must not defeat the extension allowlist."""
    transport = CapturingTransport(_make_handler())
    path = _write_image(tmp_path, "innocent.png", TEXT_BYTES)

    result = json.loads(await devops_upload_work_item_attachment(
        UploadWorkItemAttachmentInput(file_path=str(path)), _make_ctx(transport)
    ))

    assert result["error"] is True
    assert "not a PNG, JPEG, GIF or WEBP" in result["message"]
    assert transport.requests == []
    assert build_headers_mock.await_count == 0


async def test_renamed_non_image_via_data_base64_is_rejected(auth_patches, build_headers_mock):
    transport = CapturingTransport(_make_handler())

    result = json.loads(await devops_upload_work_item_attachment(
        UploadWorkItemAttachmentInput(
            data_base64=base64.b64encode(TEXT_BYTES).decode(), file_name="innocent.png"
        ),
        _make_ctx(transport),
    ))

    assert result["error"] is True
    assert transport.requests == []
    assert build_headers_mock.await_count == 0


async def test_upload_over_size_cap_is_rejected_with_zero_requests(auth_patches, tmp_path, monkeypatch):
    monkeypatch.setattr(attachments_module, "_MAX_UPLOAD_BYTES", 64)
    transport = CapturingTransport(_make_handler())
    path = _write_image(tmp_path, "big.png", PNG_BYTES)

    result = json.loads(await devops_upload_work_item_attachment(
        UploadWorkItemAttachmentInput(file_path=str(path)), _make_ctx(transport)
    ))

    assert result["error"] is True
    assert "upload limit" in result["message"]
    assert transport.requests == []


async def test_missing_file_is_reported_not_raised(auth_patches, tmp_path):
    transport = CapturingTransport(_make_handler())

    result = json.loads(await devops_upload_work_item_attachment(
        UploadWorkItemAttachmentInput(file_path=str(tmp_path / "nope.png")), _make_ctx(transport)
    ))

    assert result["error"] is True
    assert "not a readable regular file" in result["message"]
    assert transport.requests == []


async def test_directory_named_like_an_image_is_rejected(auth_patches, tmp_path):
    transport = CapturingTransport(_make_handler())
    directory = tmp_path / "folder.png"
    directory.mkdir()

    result = json.loads(await devops_upload_work_item_attachment(
        UploadWorkItemAttachmentInput(file_path=str(directory)), _make_ctx(transport)
    ))

    assert result["error"] is True
    assert transport.requests == []


async def test_attachment_root_confines_reads(auth_patches, tmp_path, monkeypatch):
    inside = tmp_path / "allowed"
    inside.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    monkeypatch.setenv("AZDO_ATTACHMENT_ROOT", str(inside))

    allowed_path = _write_image(inside, "ok.png", PNG_BYTES)
    denied_path = _write_image(outside, "nope.png", PNG_BYTES)

    transport_ok = CapturingTransport(_make_handler())
    ok = json.loads(await devops_upload_work_item_attachment(
        UploadWorkItemAttachmentInput(file_path=str(allowed_path)), _make_ctx(transport_ok)
    ))
    assert "error" not in ok, ok
    assert len(transport_ok.requests) == 1

    transport_denied = CapturingTransport(_make_handler())
    denied = json.loads(await devops_upload_work_item_attachment(
        UploadWorkItemAttachmentInput(file_path=str(denied_path)), _make_ctx(transport_denied)
    ))
    assert denied["error"] is True
    assert "AZDO_ATTACHMENT_ROOT" in denied["message"]
    assert transport_denied.requests == []


async def test_traversal_out_of_attachment_root_is_rejected(auth_patches, tmp_path, monkeypatch):
    """resolve() collapses '..' before the check, so the checked path is the opened path."""
    inside = tmp_path / "allowed"
    inside.mkdir()
    monkeypatch.setenv("AZDO_ATTACHMENT_ROOT", str(inside))
    outside_path = _write_image(tmp_path, "escape.png", PNG_BYTES)

    transport = CapturingTransport(_make_handler())
    result = json.loads(await devops_upload_work_item_attachment(
        UploadWorkItemAttachmentInput(file_path=str(inside / ".." / outside_path.name)),
        _make_ctx(transport),
    ))

    assert result["error"] is True
    assert transport.requests == []


async def test_upload_error_response_is_reported_as_json(auth_patches, tmp_path):
    transport = CapturingTransport(_make_handler(upload_status=403))
    path = _write_image(tmp_path, "diagram.png", PNG_BYTES)

    result = json.loads(await devops_upload_work_item_attachment(
        UploadWorkItemAttachmentInput(file_path=str(path)), _make_ctx(transport)
    ))

    assert result["error"] is True
    assert FAKE_PROJECT in result["message"]


async def test_upload_gateway_error_warns_about_a_possible_orphan(auth_patches, tmp_path):
    """A POST is never retried on 5xx — the caller must know a blob may exist."""
    transport = CapturingTransport(_make_handler(upload_status=503))
    path = _write_image(tmp_path, "diagram.png", PNG_BYTES)

    result = json.loads(await devops_upload_work_item_attachment(
        UploadWorkItemAttachmentInput(file_path=str(path)), _make_ctx(transport)
    ))

    assert result["error"] is True
    assert "unreferenced attachment" in result["message"]
    assert len(transport.requests) == 1  # not retried


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


async def test_download_tool_is_registered_unconditionally():
    names = {tool.name for tool in await mcp.list_tools()}
    assert "devops_get_work_item_attachment" in names


async def test_upload_tool_registration_follows_the_write_gate():
    from devops_mcp._app import _ALLOW_WRITE

    names = {tool.name for tool in await mcp.list_tools()}
    assert ("devops_upload_work_item_attachment" in names) is _ALLOW_WRITE

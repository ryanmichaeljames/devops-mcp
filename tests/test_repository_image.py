"""Unit tests for devops_get_repository_image.

All HTTP is intercepted with an httpx.AsyncBaseTransport stub — no network, no
credentials, no real organization or project names.

Three things are pinned here rather than assumed:

1. **structured_output=False.** Removing it does not fail quietly — the SDK
   cannot derive an output schema for `Image`. Both ways of getting it wrong are
   covered: dropping the flag (an import-time crash) and "correcting" the
   annotation to `-> str` (a call-time ValidationError).

2. **devops_get_file_content's contract is unchanged.** This is a sibling tool,
   not a change to the text reader — a text read must still return a plain JSON
   string, and its binary refusal must now name this tool as the way forward.

3. **`$format=octetStream` reaches the wire.** The transport stub deliberately
   behaves like the real Git Items route: *without* that parameter it answers
   the item's metadata JSON, not the blob, with HTTP 200 and a plausible
   length. A tool that forgets it therefore fails here instead of shipping —
   which is the bug that shipped once already, because the previous stub
   returned PNG bytes unconditionally and the request assertions checked only
   `path` and `api-version`.
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
import devops_mcp.tools.repositories as repositories_module
from devops_mcp._app import mcp
from devops_mcp.models import GetFileContentInput, GetRepositoryImageInput
from devops_mcp.tools.repositories import (
    devops_get_file_content,
    devops_get_repository_image,
)

# ---------------------------------------------------------------------------
# Fake constants — no real organizations, projects, or identifiers
# ---------------------------------------------------------------------------

FAKE_ORG = "fake-org"
FAKE_PROJECT = "fake-project"
FAKE_REPO = "fake-repo"
FAKE_PATH = "/docs/architecture.png"

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4
JPEG_BYTES = b"\xff\xd8\xff\xe0" + bytes(range(256)) * 4
GIF_BYTES = b"GIF89a" + bytes(range(256)) * 4
WEBP_BYTES = b"RIFF" + b"\x24\x00\x00\x00" + b"WEBP" + bytes(range(256)) * 4
PDF_BYTES = b"%PDF-1.7\n" + bytes(range(256)) * 4
TEXT_BYTES = b"# Architecture\n\nSome markdown.\n"

# What the Git Items route really answers when $format=octetStream is absent:
# the item's metadata, as JSON, with HTTP 200. Shape (and the fact that it is
# longer than the small file it describes) taken from a live response.
ITEM_METADATA = {
    "objectId": "00bcb6e37e3b7c2f4a1d9e8c5b3a2f1d0e9c8b7a",
    "gitObjectType": "blob",
    "commitId": "1f2e3d4c5b6a798877665544332211000fedcba9",
    "path": FAKE_PATH,
    "url": (
        f"https://dev.azure.com/{FAKE_ORG}/{FAKE_PROJECT}/_apis/git/repositories/"
        f"{FAKE_REPO}/items?path={FAKE_PATH}&versionType=Branch&versionOptions=None"
    ),
}
ITEM_METADATA_BYTES = json.dumps(ITEM_METADATA).encode()


# ---------------------------------------------------------------------------
# Transport plumbing
# ---------------------------------------------------------------------------


class CapturingTransport(httpx.AsyncBaseTransport):
    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self._handler = handler
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)


def _make_handler(
    *,
    body: bytes = PNG_BYTES,
    status: int = 200,
    content_type: str = "application/octet-stream",
) -> Callable[[httpx.Request], httpx.Response]:
    """A stub that models the real Git Items route, blob-vs-metadata included.

    The fidelity that matters: `$format=octetStream` is what asks for the file.
    Omit it and the service returns the *item metadata* as JSON — 200 OK, no
    error, no header that looks wrong — which is precisely why this bug reached
    a live org twice. Returning `body` unconditionally, as this stub used to,
    makes that class of defect untestable.
    """

    def handler(req: httpx.Request) -> httpx.Response:
        if status != 200:
            return httpx.Response(
                status_code=status,
                headers={"Content-Type": "application/json"},
                content=json.dumps({
                    "message": "TF401174: The item could not be found.",
                    "typeKey": "GitItemNotFoundException",
                }).encode(),
                request=req,
            )
        if req.url.params.get("$format") != "octetStream":
            return httpx.Response(
                status_code=200,
                headers={"Content-Type": "application/json; charset=utf-8"},
                content=ITEM_METADATA_BYTES,
                request=req,
            )
        return httpx.Response(
            status_code=200,
            headers={"Content-Type": content_type},
            content=body,
            request=req,
        )

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
    headers = {"Authorization": "Bearer fake-token", "Accept": "application/json"}
    if include_content_type:
        headers["Content-Type"] = "application/json"
    if extra:
        headers.update(extra)
    return headers


@pytest.fixture()
def build_headers_mock():
    return AsyncMock(side_effect=_fake_build_headers)


@pytest.fixture()
def auth_patches(build_headers_mock):
    patches = [
        patch("devops_mcp.tools.repositories.build_headers", new=build_headers_mock),
        patch("devops_mcp.tools.repositories.resolve_org", return_value=FAKE_ORG),
        patch("devops_mcp.tools.repositories.resolve_project", return_value=FAKE_PROJECT),
    ]
    for p in patches:
        p.start()
    try:
        yield
    finally:
        for p in patches:
            p.stop()


def _blocks_to_metadata(blocks: list) -> dict:
    assert isinstance(blocks[0], str), f"Element 0 must be a JSON string, got {type(blocks[0])}"
    return json.loads(blocks[0])


async def _get_image(transport: CapturingTransport, **kwargs) -> list:
    kwargs.setdefault("path", FAKE_PATH)
    return await devops_get_repository_image(
        GetRepositoryImageInput(repository_id=FAKE_REPO, **kwargs),
        _make_ctx(transport),
    )


# ---------------------------------------------------------------------------
# Happy path
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
async def test_returns_metadata_plus_image_block(auth_patches, body, expected_format, expected_mime):
    transport = CapturingTransport(_make_handler(body=body))
    blocks = await _get_image(transport)

    assert len(blocks) == 2
    metadata = _blocks_to_metadata(blocks)
    assert metadata["is_image"] is True
    assert metadata["content_type"] == expected_mime
    assert metadata["detected_by"] == "magic_bytes"
    assert metadata["path"] == FAKE_PATH
    assert metadata["repository_id"] == FAKE_REPO
    assert metadata["size_bytes"] == len(body)

    image = blocks[1]
    assert isinstance(image, Image)
    assert image._mime_type == expected_mime
    assert base64.b64decode(image.to_image_content().data) == body


async def test_magic_bytes_beat_the_path_extension(auth_patches):
    transport = CapturingTransport(_make_handler(body=JPEG_BYTES))
    blocks = await _get_image(transport)  # path says .png

    metadata = _blocks_to_metadata(blocks)
    assert metadata["content_type"] == "image/jpeg"
    assert metadata["detected_by"] == "magic_bytes"


async def test_extension_fallback_when_the_signature_is_inconclusive(auth_patches):
    transport = CapturingTransport(_make_handler(body=b"\x00\x01\x02\x03" * 16))
    blocks = await _get_image(transport, path="/docs/mystery.gif")

    metadata = _blocks_to_metadata(blocks)
    assert metadata["detected_by"] == "file_extension"
    assert metadata["content_type"] == "image/gif"


async def test_request_shape_matches_get_file_content(auth_patches):
    transport = CapturingTransport(_make_handler())
    await _get_image(transport)

    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.method == "GET"
    assert request.url.path == f"/{FAKE_ORG}/{FAKE_PROJECT}/_apis/git/repositories/{FAKE_REPO}/items"
    params = dict(request.url.params)
    assert params["path"] == FAKE_PATH
    assert params["api-version"] == "7.1"
    # Load-bearing: without it the route answers metadata JSON, not the blob.
    assert params["$format"] == "octetStream"


@pytest.mark.parametrize("branch", [None, "main"])
async def test_format_octet_stream_is_sent_on_every_image_read(auth_patches, branch):
    """Including with a versionDescriptor — the live symptom was size_bytes
    changing 972 -> 998 when branch was passed, because the *metadata* grew."""
    transport = CapturingTransport(_make_handler())
    kwargs = {"branch": branch} if branch else {}
    blocks = await _get_image(transport, **kwargs)

    assert dict(transport.requests[0].url.params)["$format"] == "octetStream"
    metadata = _blocks_to_metadata(blocks)
    assert metadata["size_bytes"] == len(PNG_BYTES)


async def test_metadata_json_would_be_returned_without_the_format_parameter():
    """Pins the stub's fidelity: this is what the real service does, and it is
    why forgetting $format produces an image block whose bytes are `{"object`."""
    transport = CapturingTransport(_make_handler())
    client = httpx.AsyncClient(transport=transport)
    response = await client.get(
        f"https://dev.azure.com/{FAKE_ORG}/{FAKE_PROJECT}/_apis/git/repositories/{FAKE_REPO}/items",
        params={"path": FAKE_PATH, "api-version": "7.1"},
    )

    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("application/json")
    assert response.content.startswith(b'{"object')
    assert response.content != PNG_BYTES


async def test_branch_version_descriptor(auth_patches):
    transport = CapturingTransport(_make_handler())
    blocks = await _get_image(transport, branch="feature/x")

    params = dict(transport.requests[0].url.params)
    assert params["versionDescriptor.version"] == "feature/x"
    assert params["versionDescriptor.versionType"] == "branch"
    assert _blocks_to_metadata(blocks)["branch"] == "feature/x"


async def test_commit_id_takes_precedence_over_branch(auth_patches):
    transport = CapturingTransport(_make_handler())
    blocks = await _get_image(transport, branch="main", commit_id="abc123")

    params = dict(transport.requests[0].url.params)
    assert params["versionDescriptor.version"] == "abc123"
    assert params["versionDescriptor.versionType"] == "commit"
    metadata = _blocks_to_metadata(blocks)
    assert metadata["commit_id"] == "abc123"
    assert "branch" not in metadata


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


async def test_non_image_is_refused_naming_what_it_is(auth_patches):
    transport = CapturingTransport(
        _make_handler(body=PDF_BYTES, content_type="application/octet-stream")
    )
    blocks = await _get_image(transport, path="/docs/report.pdf")

    assert len(blocks) == 1
    metadata = _blocks_to_metadata(blocks)
    assert metadata["error"] is True
    assert metadata["is_image"] is False
    assert metadata["content_type"] == "application/octet-stream"
    assert "/docs/report.pdf" in metadata["message"]
    assert "devops_get_file_content" in metadata["message"]
    # No token burn: the bytes are not smuggled back as base64.
    assert base64.b64encode(PDF_BYTES).decode() not in blocks[0]


async def test_text_file_is_refused_and_points_at_the_text_tool(auth_patches):
    transport = CapturingTransport(_make_handler(body=TEXT_BYTES, content_type="text/plain"))
    blocks = await _get_image(transport, path="/docs/notes.md")

    metadata = _blocks_to_metadata(blocks)
    assert metadata["error"] is True
    assert metadata["is_image"] is False
    assert "text/plain" in metadata["message"]
    assert "devops_get_file_content" in metadata["message"]


async def test_oversize_image_reports_both_sizes_and_returns_no_bytes(auth_patches, monkeypatch):
    monkeypatch.setattr(repositories_module, "_MAX_IMAGE_BYTES", 512)
    oversized = b"\x89PNG\r\n\x1a\n" + b"\x00" * 1024
    transport = CapturingTransport(_make_handler(body=oversized))
    blocks = await _get_image(transport)

    assert len(blocks) == 1
    metadata = _blocks_to_metadata(blocks)
    assert metadata["error"] is True
    assert metadata["size_bytes"] == len(oversized)
    assert metadata["max_bytes"] == 512
    assert f"{len(oversized):,}" in metadata["message"]


async def test_404_is_reported_as_actionable_json(auth_patches):
    transport = CapturingTransport(_make_handler(status=404))
    blocks = await _get_image(transport)

    assert len(blocks) == 1
    metadata = _blocks_to_metadata(blocks)
    assert metadata["error"] is True
    assert FAKE_PATH in metadata["message"]
    assert "devops_list_repository_items" in metadata["message"]


async def test_500_is_reported_not_raised(auth_patches):
    transport = CapturingTransport(_make_handler(status=500))
    blocks = await _get_image(transport)

    assert _blocks_to_metadata(blocks)["error"] is True


async def test_unresolvable_org_is_reported_as_json(build_headers_mock):
    transport = CapturingTransport(_make_handler())
    with patch("devops_mcp.tools.repositories.build_headers", new=build_headers_mock):
        app_ctx = MagicMock()
        app_ctx.organization = None
        app_ctx.project = None
        app_ctx.http_client = httpx.AsyncClient(transport=transport)
        mcp_ctx = MagicMock()
        mcp_ctx.request_context.lifespan_context = app_ctx

        blocks = await devops_get_repository_image(
            GetRepositoryImageInput(repository_id=FAKE_REPO, path=FAKE_PATH), mcp_ctx
        )

    assert _blocks_to_metadata(blocks)["error"] is True
    assert transport.requests == []


# ---------------------------------------------------------------------------
# devops_get_file_content is unchanged apart from its refusal message
# ---------------------------------------------------------------------------


async def _get_file(transport: CapturingTransport, **kwargs) -> dict:
    kwargs.setdefault("path", "/docs/notes.md")
    raw = await devops_get_file_content(
        GetFileContentInput(repository_id=FAKE_REPO, **kwargs), _make_ctx(transport)
    )
    assert isinstance(raw, str), "the text reader must still return a plain JSON string"
    return json.loads(raw)


async def test_get_file_content_still_returns_a_plain_json_string(auth_patches):
    transport = CapturingTransport(_make_handler(body=TEXT_BYTES, content_type="text/plain"))
    assert (await _get_file(transport))["content"] == TEXT_BYTES.decode()


async def test_get_file_content_sends_format_octet_stream(auth_patches):
    """The shipped bug: without it every file's 'content' was the item metadata
    JSON — `{"objectId":"…","gitObjectType":"blob",…}` — not the file."""
    transport = CapturingTransport(_make_handler(body=TEXT_BYTES, content_type="text/plain"))
    result = await _get_file(transport)

    assert dict(transport.requests[0].url.params)["$format"] == "octetStream"
    assert result["content"] == TEXT_BYTES.decode()
    assert "gitObjectType" not in result["content"]


@pytest.mark.parametrize("branch_kwargs", [{}, {"branch": "main"}, {"commit_id": "abc123"}])
async def test_get_file_content_sends_format_with_every_version_descriptor(
    auth_patches, branch_kwargs
):
    transport = CapturingTransport(_make_handler(body=TEXT_BYTES, content_type="text/plain"))
    await _get_file(transport, **branch_kwargs)

    assert dict(transport.requests[0].url.params)["$format"] == "octetStream"


async def test_get_file_content_binary_refusal_points_at_the_image_tool(auth_patches):
    transport = CapturingTransport(_make_handler(body=PNG_BYTES, content_type="image/png"))
    result = await _get_file(transport, path=FAKE_PATH)

    assert result["error"] is True
    assert "devops_get_repository_image" in result["message"]
    assert "image/png" in result["message"]


async def test_get_file_content_refuses_binary_served_as_octet_stream(auth_patches):
    """The refusal branch is byte-driven, so it still fires when the service
    gives the generic content type for an extension it does not recognise."""
    transport = CapturingTransport(
        _make_handler(body=PDF_BYTES, content_type="application/octet-stream")
    )
    result = await _get_file(transport, path="/docs/report.pdf")

    assert result["error"] is True
    assert "binary" in result["message"]
    assert "content" not in result


async def test_get_file_content_returns_text_served_as_octet_stream(auth_patches):
    """The converse, and the reason the refusal is not header-driven: Azure
    DevOps answers application/octet-stream for any extension it does not map,
    including ordinary text ones. Refusing on the header alone would break
    reading a .tf/.tfvars/.editorconfig-style file entirely."""
    transport = CapturingTransport(
        _make_handler(body=TEXT_BYTES, content_type="application/octet-stream")
    )
    result = await _get_file(transport, path="/infra/main.tf")

    assert result["content"] == TEXT_BYTES.decode()
    assert "error" not in result


async def test_get_file_content_reports_the_version_it_read(auth_patches):
    transport = CapturingTransport(_make_handler(body=TEXT_BYTES, content_type="text/plain"))
    result = await _get_file(transport, branch="feature/x")

    assert result["branch"] == "feature/x"
    params = dict(transport.requests[0].url.params)
    assert params["versionDescriptor.version"] == "feature/x"
    assert params["versionDescriptor.versionType"] == "branch"


def test_get_file_content_still_has_an_output_schema():
    """The text tool must NOT have been converted into a media tool."""
    tool = mcp._tool_manager.get_tool("devops_get_file_content")
    assert tool.fn_metadata.output_schema is not None


# ---------------------------------------------------------------------------
# structured_output=False — the load-bearing registration detail
# ---------------------------------------------------------------------------


async def test_registered_tool_emits_a_real_image_content_block(auth_patches):
    transport = CapturingTransport(_make_handler())
    tool = mcp._tool_manager.get_tool("devops_get_repository_image")

    result = await tool.run(
        {"params": {"repository_id": FAKE_REPO, "path": FAKE_PATH}},
        _make_ctx(transport),
        convert_result=True,
    )

    assert [block.type for block in result.content] == ["text", "image"]
    assert result.content[1].mime_type == "image/png"
    assert base64.b64decode(result.content[1].data) == PNG_BYTES
    assert json.loads(result.content[0].text)["is_image"] is True


def test_registered_tool_has_no_output_schema():
    tool = mcp._tool_manager.get_tool("devops_get_repository_image")
    assert tool.fn_metadata.output_schema is None


def test_dropping_structured_output_false_breaks_registration():
    with pytest.raises(PydanticSchemaGenerationError):
        Tool.from_function(devops_get_repository_image, name="canary_no_flag")


def test_str_annotation_would_reject_the_image_at_call_time():
    async def canary(params: GetRepositoryImageInput, ctx: Context) -> str:  # pragma: no cover
        ...

    tool = Tool.from_function(canary, name="canary_str_annotation_repo_image")
    assert tool.fn_metadata.output_schema is not None
    with pytest.raises(ValidationError):
        tool.fn_metadata.convert_result(["{}", Image(data=PNG_BYTES, format="png")])


async def test_tool_is_registered_unconditionally():
    names = {tool.name for tool in await mcp.list_tools()}
    assert "devops_get_repository_image" in names

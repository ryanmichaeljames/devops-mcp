"""Work item attachment tools for Azure DevOps MCP.

Four tools that together let an agent find, *see*, upload and attach the images
on a work item:

- ``devops_list_work_item_attachments`` enumerates both attachment surfaces.
  They are independent: a file on the Attachments tab is an ``AttachedFile``
  relation, while a pasted screenshot is only a URL inside a large-text field,
  so a relations-only listing silently misses every inline image. This tool
  scans both and de-duplicates by attachment GUID.
- ``devops_get_work_item_attachment`` downloads an attachment with the server's
  authenticated client and returns it as a real MCP image block. Without it the
  URL sitting in a description's ``<img src>`` is a dead end: the endpoint needs
  the Entra bearer token that only this server holds, so no client-side fetch
  tool can follow it.
- ``devops_upload_work_item_attachment`` uploads an image and returns the
  attachment reference plus ready-to-paste embed snippets for both the markdown
  and HTML field formats.
- ``devops_link_work_item_attachment`` adds the ``AttachedFile`` relation that
  puts an uploaded attachment on the Attachments tab — the separate second step
  that upload deliberately does not perform.

Deviations from the house contract, both deliberate:

1. ``devops_get_work_item_attachment`` returns ``list[str | Image]`` rather than
   a JSON ``str``. Base64 inside a JSON string is not an image to the model, it
   is tens of thousands of tokens of garbage. Element 0 is always the JSON
   metadata string; element 1, when present, is the image. Errors are still a
   single-element list holding a JSON error string so the shape stays uniform.
   The tool MUST stay registered with ``structured_output=False`` — see the
   comment on the decorator.
2. Neither tool uses ``build_params()``: it hard-codes ``api-version=7.1``,
   which is wrong for the work item attachment endpoints.

Security, in short (the full rationale lives in ``devops_mcp.attachment_media``):

- A caller-supplied ``url`` is attacker-influenceable — it usually comes from a
  work item description, which is user-authored content and a prompt-injection
  surface. It is parsed for a GUID and a file name and then **discarded**; the
  request is rebuilt against the resolved organization. Rejection happens before
  ``build_headers()`` so a bad URL never even causes a token to be minted.
  Every URL scraped out of field or comment text by the listing tool gets the
  same treatment, and one that fails is dropped and *counted* rather than
  reported back — echoing it would simply relay an injected payload to the
  model. ``devops_link_work_item_attachment`` needs the guard most of all: a URL
  it stored unvalidated would be persisted on the work item for every future
  reader, not fetched once.
- The upload path reads a local file chosen by an LLM, i.e. a potential
  data-exfiltration primitive. It is gated behind ``AZDO_ALLOW_WRITE``, limited
  to an image extension allowlist, and — the guard that makes the allowlist mean
  anything — the actual bytes must carry a matching image signature, so
  ``cp ~/.ssh/id_rsa /tmp/x.png`` is refused. ``AZDO_ATTACHMENT_ROOT`` optionally
  confines reads to one directory tree.
"""

import asyncio
import base64
import json
import logging
import os
from pathlib import Path
from urllib.parse import quote

import httpx
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.utilities.types import Image

from devops_mcp._app import mcp, write_tool
from devops_mcp.attachment_media import (
    MAX_INLINE_IMAGE_BYTES,
    build_embed_snippets,
    detect_image_format,
    find_attachment_urls,
    mime_type_for_format,
    parse_attachment_url,
    sniff_image_format,
)
from devops_mcp.client import (
    AppContext,
    build_headers,
    build_org_url,
    build_url,
    extract_error_message,
    finalize_response,
    request_with_retry,
    resolve_org,
    resolve_project,
)
from devops_mcp.models import (
    GetWorkItemAttachmentInput,
    LinkWorkItemAttachmentInput,
    ListWorkItemAttachmentsInput,
    UploadWorkItemAttachmentInput,
)

# The single source of truth for "which fields are large-text fields" already
# lives beside the /multilineFieldsFormat logic that needs it. Imported rather
# than restated so adding a field there automatically extends the inline scan
# here; a second hand-maintained copy would drift the first time one is added.
from devops_mcp.tools.work_items import _MULTILINE_TEXT_FIELDS

logger = logging.getLogger(__name__)

# The attachment operations are 7.2-preview.4. build_params() cannot be used
# here — it injects api-version=7.1.
#
# Live-verified, because the published reference only ever shows 7.1 for these
# two operations: 7.2-preview.4 is accepted on GET as well as POST. The 200 is
# meaningful rather than an ignored parameter — the same GET sent with a
# nonsense api-version=9.9 control returns HTTP 400, so the service really is
# reading and validating the value.
_WIT_ATTACHMENTS_API_VERSION = "7.2-preview.4"

# Work item read/JSON-Patch operations are a *different* preview revision from
# the attachment operations above — .3, not .4. Same reason build_params() is
# unusable here: it injects api-version=7.1.
_WIT_WORKITEMS_API_VERSION = "7.2-preview.3"

# Comments have no 7.2 revision at all; -preview.4 is the one that honours
# `format`, and it is what tools/work_items.py already uses.
_WIT_COMMENTS_API_VERSION = "7.1-preview.4"

# Comments page size. 200 is the endpoint's documented maximum; a work item with
# more than that is reported as truncated rather than silently under-scanned.
_COMMENTS_PAGE_SIZE = 200

# Raw byte cap for an inline image; see MAX_INLINE_IMAGE_BYTES for the rationale.
# Kept as a module-local name because it is part of this module's error payloads.
_MAX_IMAGE_BYTES = MAX_INLINE_IMAGE_BYTES

# Upload cap, well under the service's fixed 60 MB per attachment. Checked
# before the request so a doomed 25 MB write is never issued.
_MAX_UPLOAD_BYTES = 25_000_000

_NOT_AN_IMAGE_MESSAGE = (
    "Attachment is not a viewable image (png/jpeg/gif/webp). Bytes were not "
    "returned. Download it from 'url' with an authenticated client."
)


def _canonical_attachment_url(organization: str, attachment_id: str, file_name: str | None) -> str:
    """Build the canonical org-scoped attachment URL, with ?fileName when known."""
    url = build_org_url(organization, f"wit/attachments/{attachment_id}")
    if file_name:
        return f"{url}?fileName={quote(file_name, safe='')}"
    return url


def _download_error_message(status_code: int, msg: str, *, organization: str, attachment_id: str) -> str:
    if status_code == 401:
        return (
            "Authentication failed (HTTP 401). Re-check AZDO_AUTH_TYPE / your sign-in; "
            "reading attachments needs the `vso.work` scope."
        )
    if status_code == 403:
        return (
            "Access denied (HTTP 403). The signed-in identity cannot read attachments "
            f"in organization '{organization}'."
        )
    if status_code == 404:
        return (
            f"Attachment {attachment_id} not found in organization '{organization}'. "
            "The GUID may be wrong, the attachment may have been deleted, or it may "
            "belong to a different organization."
        )
    if status_code == 400:
        return (
            "Azure DevOps rejected the request (HTTP 400) — check that the attachment "
            f"id is a valid GUID. api-version={_WIT_ATTACHMENTS_API_VERSION}."
        )
    return f"Azure DevOps returned HTTP {status_code}: {msg}"


def _upload_error_message(status_code: int, msg: str, *, project: str, file_name: str) -> str:
    if status_code == 401:
        return (
            "Authentication failed (HTTP 401). Re-check AZDO_AUTH_TYPE / your sign-in; "
            "uploading attachments needs the `vso.work_write` scope."
        )
    if status_code == 403:
        return (
            "Access denied (HTTP 403). The signed-in identity needs write access to "
            f"work items in project '{project}'."
        )
    if status_code == 404:
        return f"Project '{project}' not found, or the attachments endpoint is unavailable for it."
    if status_code == 413:
        return (
            f"Azure DevOps rejected '{file_name}' as too large (HTTP 413). Azure DevOps "
            "Services caps an attachment at 60 MB; Azure DevOps Server defaults to 4 MB."
        )
    if status_code in (502, 503, 504):
        return (
            f"Azure DevOps returned HTTP {status_code} on the upload of '{file_name}'. "
            "The request was NOT retried, because a POST may have committed server-side "
            "before the error surfaced — an unreferenced attachment may now exist whose "
            "id was never returned. Retrying is safe but may create a second copy."
        )
    return f"Azure DevOps returned HTTP {status_code}: {msg}"


def _work_item_error_message(status_code: int, msg: str, *, work_item_id: int, project: str) -> str:
    if status_code == 401:
        return (
            "Authentication failed (HTTP 401). Re-check AZDO_AUTH_TYPE / your sign-in; "
            "work item attachments need the `vso.work` scope (`vso.work_write` to link)."
        )
    if status_code == 403:
        return (
            f"Access denied (HTTP 403) on work item {work_item_id} in project '{project}'. "
            "The signed-in identity lacks the required work item permission."
        )
    if status_code == 404:
        return (
            f"Work item {work_item_id} was not found in project '{project}'. It may belong "
            "to a different project, may have been deleted, or the ID may be wrong."
        )
    return f"Azure DevOps returned HTTP {status_code}: {msg}"


class _AttachmentIndex:
    """Collects attachment references from both surfaces, de-duplicated by GUID.

    Every URL handed to :meth:`add` — whether it came off an ``AttachedFile``
    relation or was scraped out of user-authored field text — is validated with
    ``parse_attachment_url`` against the resolved organization. A URL that fails
    is dropped and counted; it is never echoed back, because work item text is a
    prompt-injection surface and repeating a rejected URL to the model relays
    exactly the payload the guard just caught. Only canonical rebuilt URLs are
    ever surfaced.
    """

    def __init__(self, organization: str) -> None:
        self._organization = organization
        self._entries: dict[str, dict] = {}
        # Raw strings, kept only so a URL repeated across several fields counts
        # once. Deliberately never read out of this object.
        self._rejected: set[str] = set()

    @property
    def rejected_count(self) -> int:
        return len(self._rejected)

    def add(
        self,
        raw_url: str | None,
        *,
        source: str,
        file_name: str | None = None,
        size_bytes: int | None = None,
        comment: str | None = None,
        field: str | None = None,
        comment_id: int | None = None,
    ) -> None:
        if not raw_url:
            return
        try:
            attachment_id, url_file_name = parse_attachment_url(raw_url, self._organization)
        except ValueError as exc:
            self._rejected.add(raw_url)
            # stderr only — the reason, never the URL itself.
            logger.warning("Dropped a %s attachment URL that failed validation: %s", source, exc)
            return

        entry = self._entries.setdefault(
            attachment_id.casefold(),
            {
                "attachment_id": attachment_id,
                "file_name": None,
                "sources": set(),
                "fields": [],
                "comment_ids": [],
            },
        )
        entry["sources"].add(source)
        if entry["file_name"] is None:
            # A relation's attributes.name is authoritative; the ?fileName= hint
            # on an inline URL is the fallback.
            entry["file_name"] = file_name or url_file_name
        if size_bytes is not None and "size_bytes" not in entry:
            entry["size_bytes"] = size_bytes
        if comment and "comment" not in entry:
            entry["comment"] = comment
        if field and field not in entry["fields"]:
            entry["fields"].append(field)
        if comment_id is not None and comment_id not in entry["comment_ids"]:
            entry["comment_ids"].append(comment_id)

    def to_list(self) -> list[dict]:
        results: list[dict] = []
        for entry in self._entries.values():
            item: dict = {
                "attachment_id": entry["attachment_id"],
                "file_name": entry["file_name"],
                "url": _canonical_attachment_url(
                    self._organization, entry["attachment_id"], entry["file_name"]
                ),
                # Fixed order, so the value is stable regardless of scan order.
                "sources": [s for s in ("relation", "inline") if s in entry["sources"]],
            }
            if "size_bytes" in entry:
                item["size_bytes"] = entry["size_bytes"]
            if "comment" in entry:
                item["comment"] = entry["comment"]
            if entry["fields"]:
                item["fields"] = entry["fields"]
            if entry["comment_ids"]:
                item["comment_ids"] = entry["comment_ids"]
            results.append(item)
        return results


@mcp.tool(
    name="devops_list_work_item_attachments",
    annotations={
        "title": "List Work Item Attachments",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def devops_list_work_item_attachments(params: ListWorkItemAttachmentsInput, ctx: Context) -> str:
    """List every attachment a work item references, from both of its surfaces.

    A work item holds attachments in two independent places, and looking at only
    one of them misses files:

    - AttachedFile relations — the files shown on the Attachments tab.
    - Inline images — a screenshot pasted into the description, acceptance
      criteria, repro steps or a comment is stored as a URL inside that field's
      text. It is not necessarily a relation, so it does not appear on the
      Attachments tab and is invisible to a relations-only listing.

    This tool reads the work item's relations and scans its large-text fields
    for attachment URLs, de-duplicating by attachment GUID — the same file found
    both ways is reported once, with sources ["relation", "inline"] and the
    field names it was found in.

    The most recent comment is always scanned, even without include_comments:
    System.History is one of the large-text fields and carries the newest
    comment's text, so an image pasted into the latest comment is found for
    free, reported in 'fields' as System.History and with no comment id. Set
    include_comments=true to reach *older* comments and to get comment ids; it
    costs one extra API call.

    Feed 'attachment_id' or 'url' from a result straight into
    devops_get_work_item_attachment to view the image. Only canonical URLs
    rebuilt against the resolved organization are returned; URLs found in field
    text that do not validate as Azure DevOps attachment URLs are dropped and
    counted in 'rejected_urls' rather than shown, because work item text is
    user-authored and may be crafted.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    project = params.project or ""
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        url = build_url(organization, project, f"wit/workitems/{params.work_item_id}")
        headers = await build_headers(app_ctx)

        # No `fields` projection: `$expand=relations` already returns the full
        # field set, which is every large-text field this tool has to scan, so
        # narrowing would only risk the documented-as-alternative `fields` +
        # `$expand` combination for no gain.
        response = await request_with_retry(
            app_ctx.http_client,
            "GET",
            url,
            headers=headers,
            params={"api-version": _WIT_WORKITEMS_API_VERSION, "$expand": "relations"},
        )
        response.raise_for_status()
        work_item = response.json() or {}

        index = _AttachmentIndex(organization)

        for relation in work_item.get("relations") or []:
            if not isinstance(relation, dict) or relation.get("rel") != "AttachedFile":
                continue
            attributes = relation.get("attributes") or {}
            index.add(
                relation.get("url"),
                source="relation",
                file_name=attributes.get("name"),
                size_bytes=attributes.get("resourceSize"),
                comment=attributes.get("comment"),
            )

        fields = work_item.get("fields") or {}
        if isinstance(fields, dict):
            for field_ref, value in fields.items():
                if not isinstance(value, str) or field_ref.lower() not in _MULTILINE_TEXT_FIELDS:
                    continue
                for candidate in find_attachment_urls(value):
                    index.add(candidate, source="inline", field=field_ref)

        comments_scanned: int | None = None
        comments_truncated = False
        if params.include_comments:
            comments_url = build_url(
                organization, project, f"wit/workItems/{params.work_item_id}/comments"
            )
            comments_response = await request_with_retry(
                app_ctx.http_client,
                "GET",
                comments_url,
                headers=headers,
                params={
                    "api-version": _WIT_COMMENTS_API_VERSION,
                    "$top": _COMMENTS_PAGE_SIZE,
                },
            )
            comments_response.raise_for_status()
            comments_body = comments_response.json() or {}
            comments = comments_body.get("comments") or []
            comments_scanned = len(comments)
            comments_truncated = bool(comments_body.get("continuationToken"))
            for comment in comments:
                if not isinstance(comment, dict):
                    continue
                for candidate in find_attachment_urls(comment.get("text")):
                    index.add(candidate, source="inline", comment_id=comment.get("id"))

        attachments = index.to_list()
        result: dict = {
            "work_item_id": params.work_item_id,
            "attachments": attachments,
            "count": len(attachments),
            "rejected_urls": index.rejected_count,
        }
        if index.rejected_count:
            result["rejected_urls_note"] = (
                f"{index.rejected_count} URL(s) in this work item's text looked like "
                f"attachment links but are not valid attachments of organization "
                f"'{organization}'. They were dropped and are deliberately not shown: "
                "work item text is user-authored, so repeating such a URL would relay "
                "content this server just refused."
            )
        if comments_scanned is not None:
            result["comments_scanned"] = comments_scanned
            if comments_truncated:
                result["comments_truncated"] = True
                result["comments_note"] = (
                    f"Only the first {_COMMENTS_PAGE_SIZE} comments were scanned; this work "
                    "item has more."
                )
        return finalize_response(result)

    except ValueError as e:
        return finalize_response({"error": True, "message": str(e)})
    except httpx.HTTPStatusError as e:
        raw_msg = extract_error_message(e.response)
        logger.error("Azure DevOps HTTP %d: %s", e.response.status_code, raw_msg)
        return finalize_response({
            "error": True,
            "message": _work_item_error_message(
                e.response.status_code,
                raw_msg,
                work_item_id=params.work_item_id,
                project=project,
            ),
        })
    except Exception as e:
        logger.exception("Unexpected error in devops_list_work_item_attachments")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


@mcp.tool(
    name="devops_get_work_item_attachment",
    # structured_output=False is load-bearing, not decoration. Without it the
    # SDK derives an output schema from the return annotation and validates the
    # returned value against it — and it cannot build a schema for `Image`, so
    # registration raises PydanticSchemaGenerationError at import time (and, if
    # the annotation were ever "corrected" to `-> str`, the tool would instead
    # fail with a Pydantic ValidationError at call time, reading like an
    # unrelated bug). See tests/test_work_item_attachments.py.
    structured_output=False,
    annotations={
        "title": "Get Work Item Attachment",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def devops_get_work_item_attachment(
    params: GetWorkItemAttachmentInput, ctx: Context
) -> list[str | Image]:
    """Download a work item attachment and return it as viewable image content.

    Use this to actually look at an image referenced by a work item — a pasted
    screenshot in a description or comment, or a file on the Attachments tab.
    The attachment endpoint requires this server's Entra bearer token, so a URL
    found in a description cannot be fetched by any other tool.

    Supply either attachment_id (the GUID) or url (paste the <img src> straight
    out of a description). A supplied url is never fetched as given: it is
    parsed for the GUID and fileName and the request is rebuilt against the
    resolved organization, and a url naming another host or another
    organization is refused. The endpoint is organization-scoped, so no project
    is needed even for an attachment in a different project (verified against
    the live service, attachment uploaded to one project and read back with the
    organization alone).

    Returns two content blocks for an image — a JSON metadata string and the
    image itself — and a single JSON block otherwise. A non-image attachment
    (PDF, zip, log) is reported as metadata with is_image false and its bytes
    are deliberately not returned; an image over the inline size limit is
    reported as an error carrying its size and url. The image format is decided
    by the file's magic bytes, not the response Content-Type (which is
    unreliable: a PNG read by GUID alone comes back as application/octet-stream)
    and not the file extension.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    organization = ""
    attachment_id = params.attachment_id or ""
    try:
        organization = resolve_org(app_ctx, params.organization)

        file_name = params.file_name
        if params.url:
            # Parses and validates only; the URL itself is then discarded. This
            # must stay ahead of build_headers() so no token is minted for a
            # URL we are going to refuse.
            attachment_id, url_file_name = parse_attachment_url(params.url, organization)
            file_name = file_name or url_file_name

        url = build_org_url(organization, f"wit/attachments/{attachment_id}")
        # `download` is deliberately not sent: measured against the live service,
        # download=true, download=false and omitting it return the same bytes and
        # the same headers, so it is a no-op here. `fileName` is not a no-op —
        # supplying it makes the service return a real Content-Type (image/png)
        # instead of application/octet-stream — but the tool can be called with a
        # GUID alone, so the format is still decided by sniffing the bytes.
        query_params: dict[str, str] = {"api-version": _WIT_ATTACHMENTS_API_VERSION}
        if file_name:
            query_params["fileName"] = file_name

        response = await request_with_retry(
            app_ctx.http_client,
            "GET",
            url,
            headers=await build_headers(app_ctx),
            params=query_params,
        )
        response.raise_for_status()

        content = response.content
        canonical_url = _canonical_attachment_url(organization, attachment_id, file_name)

        # Magic bytes decide; the file name is a fallback only, since it is
        # caller-influenceable. Shared with devops_get_repository_image so both
        # media tools classify identically.
        image_format, detected_by = detect_image_format(content, file_name)

        metadata: dict = {
            "attachment_id": attachment_id,
            "file_name": file_name,
            "url": canonical_url,
            "size_bytes": len(content),
        }

        if image_format is None:
            return [finalize_response({**metadata, "is_image": False, "message": _NOT_AN_IMAGE_MESSAGE})]

        if len(content) > _MAX_IMAGE_BYTES:
            return [
                finalize_response({
                    **metadata,
                    "error": True,
                    "message": (
                        f"Attachment '{file_name or attachment_id}' is {len(content):,} bytes, "
                        f"over the {_MAX_IMAGE_BYTES:,}-byte inline limit for image content "
                        "(base64 expansion would exceed the 5 MB response ceiling). The bytes "
                        "were not returned; download it from 'url' with an authenticated client."
                    ),
                    "max_bytes": _MAX_IMAGE_BYTES,
                })
            ]

        return [
            finalize_response({
                **metadata,
                "content_type": mime_type_for_format(image_format),
                "detected_by": detected_by,
                "is_image": True,
            }),
            Image(data=content, format=image_format),
        ]

    except ValueError as e:
        return [finalize_response({"error": True, "message": str(e)})]
    except httpx.HTTPStatusError as e:
        raw_msg = extract_error_message(e.response)
        logger.error("Azure DevOps HTTP %d: %s", e.response.status_code, raw_msg)
        message = _download_error_message(
            e.response.status_code,
            raw_msg,
            organization=organization,
            attachment_id=attachment_id,
        )
        return [finalize_response({"error": True, "message": message})]
    except Exception as e:
        logger.exception("Unexpected error in devops_get_work_item_attachment")
        return [finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})]


def _resolve_local_image_path(file_path: str) -> Path:
    """Resolve *file_path* and enforce the filesystem guards. Raises ValueError.

    ``resolve()`` collapses ``..`` before the checks, so the path that is opened
    is the path that was checked. ``is_file()`` rejects directories and anything
    that is not a regular file (FIFOs, device nodes) — a read on those can block
    forever.
    """
    try:
        resolved = Path(file_path).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"'file_path' could not be resolved: {exc}")

    root = os.environ.get("AZDO_ATTACHMENT_ROOT", "").strip()
    if root:
        try:
            allowed_root = Path(root).expanduser().resolve()
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"AZDO_ATTACHMENT_ROOT could not be resolved: {exc}")
        if not resolved.is_relative_to(allowed_root):
            raise ValueError(
                f"'file_path' resolves outside AZDO_ATTACHMENT_ROOT ({allowed_root}). "
                "This server is configured to upload only files from within that directory."
            )

    if not resolved.is_file():
        raise ValueError(
            f"'file_path' is not a readable regular file: {resolved}. "
            "Supply an absolute path to an existing image file on the machine "
            "running this MCP server."
        )

    return resolved


async def _read_upload_bytes(params: UploadWorkItemAttachmentInput) -> tuple[bytes, str]:
    """Materialise the upload bytes and run every pre-flight guard.

    Returns ``(data, image_format)`` — the format is the one the *bytes* prove,
    never the one the file name claims.

    Guard order is the only one the file path permits — the extension allowlist
    already ran in the model, the filesystem checks must precede the read, and
    magic-byte verification needs the bytes in hand:

        extension allowlist (model) -> resolve()/is_file() -> AZDO_ATTACHMENT_ROOT
        -> size cap -> read -> magic-byte verification

    Every check happens before any HTTP call. Raises ValueError on refusal.
    """
    if params.data_base64 is not None:
        # The safer of the two inputs: it touches no filesystem at all.
        try:
            data = base64.b64decode(params.data_base64, validate=True)
        except Exception as exc:
            raise ValueError(f"'data_base64' is not valid base64 ({exc}).")
    else:
        resolved = _resolve_local_image_path(params.file_path or "")
        try:
            declared_size = resolved.stat().st_size
        except OSError as exc:
            raise ValueError(f"'file_path' could not be inspected: {exc}")
        if declared_size > _MAX_UPLOAD_BYTES:
            raise ValueError(
                f"'{params.file_name}' is {declared_size:,} bytes, over the "
                f"{_MAX_UPLOAD_BYTES:,}-byte upload limit."
            )
        try:
            # Reading up to 25 MB would block the event loop; do it in a thread.
            data = await asyncio.to_thread(resolved.read_bytes)
        except OSError as exc:
            raise ValueError(f"'file_path' could not be read: {exc}")

    if not data:
        raise ValueError("The image is empty (0 bytes); nothing to upload.")

    if len(data) > _MAX_UPLOAD_BYTES:
        raise ValueError(
            f"'{params.file_name}' is {len(data):,} bytes, over the "
            f"{_MAX_UPLOAD_BYTES:,}-byte upload limit."
        )

    # The load-bearing guard: without it the extension allowlist means nothing,
    # since any file can be renamed .png.
    image_format = sniff_image_format(data)
    if image_format is None:
        raise ValueError(
            f"The bytes for '{params.file_name}' are not a PNG, JPEG, GIF or WEBP image. "
            "Only real images can be uploaded by this tool — the file name's extension "
            "is not trusted on its own. Rename it accurately or supply an actual image."
        )

    return data, image_format


@write_tool(
    name="devops_upload_work_item_attachment",
    annotations={
        "title": "Upload Work Item Attachment",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def devops_upload_work_item_attachment(
    params: UploadWorkItemAttachmentInput, ctx: Context
) -> str:
    """Upload an image as an Azure DevOps work item attachment.

    Supply exactly one of file_path (a local image on the machine running this
    MCP server) or data_base64 (bytes you already hold — preferred for images
    generated in memory, since it touches no filesystem). Only real images are
    accepted: the file name must end in .png/.jpg/.jpeg/.gif/.webp *and* the
    bytes must carry a matching image signature, so a renamed non-image file is
    refused before anything is sent.

    Uploading attaches the file to nothing. It returns an attachment reference
    plus 'embed' snippets — use embed.markdown when writing a large-text field
    with format='markdown' (the default for devops_create_work_item /
    devops_update_work_item) and embed.html when writing it with format='html';
    mixing them renders the snippet as literal text. To put the file on the
    Attachments tab instead, add an AttachedFile relation referencing the
    returned url.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    project = ""
    file_name = params.file_name or ""
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)

        # Every guard runs before a single byte leaves the machine.
        data, image_format = await _read_upload_bytes(params)

        url = build_url(organization, project, "wit/attachments")
        query_params: dict[str, str] = {
            "api-version": _WIT_ATTACHMENTS_API_VERSION,
            "fileName": file_name,
        }
        if params.area_path:
            query_params["areaPath"] = params.area_path

        response = await request_with_retry(
            app_ctx.http_client,
            "POST",
            url,
            headers=await build_headers(app_ctx, extra={"Content-Type": "application/octet-stream"}),
            params=query_params,
            content=data,
        )
        response.raise_for_status()

        reference = response.json() or {}
        # Live-verified: a successful upload answers 201, and the returned url is
        # *project-GUID*-scoped —
        # https://dev.azure.com/{org}/{projectGuid}/_apis/wit/attachments/{guid}?fileName=…
        # — not org-scoped as the published samples show. Use it verbatim rather
        # than rebuilding it. Note the service does NOT encode an '&' inside the
        # fileName value, so this string can carry a bare '&'; that is what
        # build_embed_snippets' html.escape guards against.
        attachment_url = reference.get("url") or ""

        return finalize_response({
            "id": reference.get("id"),
            "url": attachment_url,
            "file_name": file_name,
            "size_bytes": len(data),
            "content_type": mime_type_for_format(image_format),
            "embed": build_embed_snippets(attachment_url, file_name),
        })

    except ValueError as e:
        return finalize_response({"error": True, "message": str(e)})
    except httpx.HTTPStatusError as e:
        raw_msg = extract_error_message(e.response)
        logger.error("Azure DevOps HTTP %d: %s", e.response.status_code, raw_msg)
        message = _upload_error_message(
            e.response.status_code,
            raw_msg,
            project=project,
            file_name=file_name,
        )
        return finalize_response({"error": True, "message": message})
    except Exception as e:
        logger.exception("Unexpected error in devops_upload_work_item_attachment")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


def _find_attached_file_relation(
    relations, attachment_id: str, organization: str
) -> dict | None:
    """Return the AttachedFile relation for *attachment_id*, or None.

    Each candidate relation's URL is put through ``parse_attachment_url``
    rather than string-compared. Two consequences, both wanted: a relation whose
    URL names another host or another organization never counts as a match (so
    it is never echoed back to the model as "already linked"), and a relation
    pointing at the same GUID via a differently-shaped URL — project-scoped
    versus org-scoped, with or without ``?fileName=`` — still matches.
    """
    if not isinstance(relations, list):
        return None
    for relation in relations:
        if not isinstance(relation, dict) or relation.get("rel") != "AttachedFile":
            continue
        try:
            existing_id, _ = parse_attachment_url(relation.get("url") or "", organization)
        except ValueError:
            continue
        if existing_id.casefold() == attachment_id.casefold():
            return {
                "rel": relation.get("rel"),
                "url": relation.get("url"),
                "attributes": relation.get("attributes") or {},
            }
    return None


@write_tool(
    name="devops_link_work_item_attachment",
    annotations={
        "title": "Link Work Item Attachment",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def devops_link_work_item_attachment(params: LinkWorkItemAttachmentInput, ctx: Context) -> str:
    """Attach an already-uploaded attachment to a work item's Attachments tab.

    devops_upload_work_item_attachment stores the bytes and attaches them to
    nothing — this is the second step that adds the AttachedFile relation and
    makes the file appear on the work item. Pass the attachment_id or url that
    upload returned, plus an optional comment.

    This is not how you put an image *inline* in a description: for that, write
    the upload's embed.markdown / embed.html snippet into the field with
    devops_update_work_item. The two surfaces are independent — a relation does
    not render in the text, and an inline image is not on the Attachments tab.
    devops_list_work_item_attachments reports both.

    Idempotent: the work item's current relations are read first, and if this
    attachment is already linked nothing is written and 'changed' is false. When
    it does write, the patch carries a /rev test op, so a concurrent edit fails
    the call rather than silently overwriting. The returned relation is read
    back from the server's response, not predicted locally.

    A supplied url is never stored as given. It is parsed for the attachment
    GUID and fileName, discarded, and the relation URL is rebuilt against the
    resolved organization — a URL naming another host or organization is
    refused. That matters more here than when downloading: a relation is
    persisted and re-read by every future reader of the work item.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    project = params.project or ""
    attachment_id = params.attachment_id or ""
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)

        file_name = params.file_name
        if params.url:
            # Validate-and-discard, ahead of build_headers() so a refused URL
            # never causes a token to be minted.
            attachment_id, url_file_name = parse_attachment_url(params.url, organization)
            file_name = file_name or url_file_name

        relation_url = _canonical_attachment_url(organization, attachment_id, file_name)
        url = build_url(organization, project, f"wit/workitems/{params.work_item_id}")
        headers = await build_headers(app_ctx)

        get_response = await request_with_retry(
            app_ctx.http_client,
            "GET",
            url,
            headers=headers,
            params={"api-version": _WIT_WORKITEMS_API_VERSION, "$expand": "relations"},
        )
        get_response.raise_for_status()
        work_item = get_response.json() or {}
        rev = work_item.get("rev")

        existing = _find_attached_file_relation(
            work_item.get("relations"), attachment_id, organization
        )
        if existing is not None:
            return finalize_response({
                "work_item_id": params.work_item_id,
                "attachment_id": attachment_id,
                "file_name": file_name,
                "changed": False,
                "relation": existing,
                "message": (
                    f"Attachment {attachment_id} is already linked to work item "
                    f"{params.work_item_id}; no update was sent."
                ),
            })

        attributes: dict = {}
        if params.comment:
            attributes["comment"] = params.comment

        patch_ops: list[dict] = []
        if rev is not None:
            # Optimistic concurrency, exactly as devops_update_work_item_tags
            # does it: if the work item moved between the read and the write,
            # the patch fails instead of appending to a stale state.
            patch_ops.append({"op": "test", "path": "/rev", "value": rev})
        relation_value: dict = {"rel": "AttachedFile", "url": relation_url}
        if attributes:
            relation_value["attributes"] = attributes
        patch_ops.append({"op": "add", "path": "/relations/-", "value": relation_value})

        patch_response = await request_with_retry(
            app_ctx.http_client,
            "PATCH",
            url,
            headers=await build_headers(
                app_ctx,
                extra={"Content-Type": "application/json-patch+json"},
            ),
            params={"api-version": _WIT_WORKITEMS_API_VERSION},
            content=json.dumps(patch_ops).encode(),
        )
        patch_response.raise_for_status()
        patched = patch_response.json() or {}

        # Report what the server stored, not what was sent — the same principle
        # devops_update_work_item_tags documents.
        persisted = _find_attached_file_relation(
            patched.get("relations"), attachment_id, organization
        )
        result: dict = {
            "work_item_id": params.work_item_id,
            "attachment_id": attachment_id,
            "file_name": file_name,
            "changed": True,
            "rev": patched.get("rev"),
            "relation": persisted or relation_value,
            "relation_read_back": persisted is not None,
        }
        if persisted is None:
            result["message"] = (
                "The update succeeded, but the response carried no relations to read "
                "back, so 'relation' is what was sent rather than what was stored. "
                "Call devops_list_work_item_attachments to confirm."
            )
        return finalize_response(result)

    except ValueError as e:
        return finalize_response({"error": True, "message": str(e)})
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        raw_msg = extract_error_message(e.response)
        logger.error("Azure DevOps HTTP %d: %s", status, raw_msg)
        # Live-verified: a failed `test /rev` op answers HTTP **412** with
        # `VS403351: TestPatchOperationFailedException` — not the HTTP 400 /
        # VS402625 that a JSON-Patch validation failure would give. 409 stays in
        # the tuple for the ordinary optimistic-concurrency conflict.
        if status in (409, 412):
            return finalize_response({
                "error": True,
                "message": (
                    f"Work item {params.work_item_id} changed between this tool's read and "
                    f"its write (HTTP {status}), so the /rev concurrency check failed and "
                    "nothing was linked. Nothing was overwritten — just call the tool again. "
                    f"Azure DevOps said: {raw_msg}"
                ),
            })
        return finalize_response({
            "error": True,
            "message": _work_item_error_message(
                status, raw_msg, work_item_id=params.work_item_id, project=project
            ),
        })
    except Exception as e:
        logger.exception("Unexpected error in devops_link_work_item_attachment")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})

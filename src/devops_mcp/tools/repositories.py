"""Repository tools for Azure DevOps MCP.

One deviation from the house contract lives here, and it is deliberate:
``devops_get_repository_image`` returns ``list[str | Image]`` rather than a JSON
``str``, for the same reason ``devops_get_work_item_attachment`` does — base64
inside a JSON string is not an image to the model, it is token burn. It is a
*sibling* of ``devops_get_file_content`` rather than a change to it: making the
text reader sometimes return an image would break its contract for every text
read. Media tools MUST be registered with ``structured_output=False``; see the
comment on the decorator.
"""

import logging

import httpx
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.utilities.types import Image

from devops_mcp._app import mcp
from devops_mcp.attachment_media import (
    MAX_INLINE_IMAGE_BYTES,
    detect_image_format,
    mime_type_for_format,
    sniff_image_format,
)
from devops_mcp.client import (
    AppContext,
    build_headers,
    build_params,
    build_url,
    extract_error_message,
    finalize_response,
    paginate_results,
    request_with_retry,
    resolve_org,
    resolve_project,
)
from devops_mcp.models import (
    GetCommitInput,
    GetFileContentInput,
    GetRepositoryImageInput,
    GetRepositoryInput,
    ListBranchesInput,
    ListCommitsInput,
    ListRepositoriesInput,
    ListRepositoryItemsInput,
)

logger = logging.getLogger(__name__)

# Raw byte cap for an image returned inline; shared with the work item
# attachment tools so both media paths have the same ceiling.
_MAX_IMAGE_BYTES = MAX_INLINE_IMAGE_BYTES

# The Git Items route does NOT return a blob's bytes by default. Without this
# parameter it answers the item's *metadata JSON* — objectId, gitObjectType,
# commitId, path, url — with `Content-Type: application/json; charset=utf-8`,
# and does so with HTTP 200, so nothing looks wrong. Measured live on a
# 239-byte PNG committed to a repository:
#
#   request                          content-type                       len  blob?
#   (no $format)                     application/json; charset=utf-8    972  no
#   $format=octetStream              image/png                          239  YES
#   Accept: application/octet-stream image/png                          239  yes
#   download=true                    application/json                   972  no
#   includeContent=true              application/json                  1779  no
#
# `Accept: application/octet-stream` works too, but `build_headers()` sends
# `Accept: application/json` for every request in this server, so the query
# parameter is the one that belongs at the call site.
#
# The failure mode this prevents is silent and plausible-looking: the metadata
# JSON is real content of a real length, so a text read returns a JSON document
# as the "file", and an image read hands the model an image block whose bytes
# begin `{"object`. Any change here needs a test that asserts the parameter
# reaches the wire — see tests/test_repository_image.py.
_BLOB_FORMAT_PARAM = {"$format": "octetStream"}


def _decode_text_blob(response: httpx.Response) -> str | None:
    """Decode a blob response as text, or return ``None`` if it is not text.

    Deliberately byte-driven rather than header-driven. With
    ``$format=octetStream`` Azure DevOps picks the response ``Content-Type``
    from the file's extension, which is accurate for the extensions it knows
    (a PNG really does come back as ``image/png``) and
    ``application/octet-stream`` for everything it does not — including
    perfectly ordinary text files with a less common extension. Refusing on that
    header alone would reject those, so the bytes decide: content that does not
    decode under the declared (or assumed) encoding is binary, whatever the
    header said.

    ``response.text`` cannot be used for the decision, because httpx decodes
    with ``errors="replace"`` — binary silently becomes a string of replacement
    characters rather than raising.
    """
    try:
        return response.content.decode(response.encoding or "utf-8")
    except (UnicodeDecodeError, LookupError):
        return None


@mcp.tool(
    name="devops_list_repositories",
    annotations={
        "title": "List Repositories",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def devops_list_repositories(params: ListRepositoriesInput, ctx: Context) -> str:
    """List Git repositories in an Azure DevOps project.

    Returns repository IDs, names, default branches, HTTPS and SSH clone URLs,
    web URLs, sizes, and project details. Use the repository ID or name with
    devops_get_repository or devops_list_branches.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        url = build_url(organization, project, "git/repositories")
        query_params = build_params(
            includeLinks="true" if params.include_links else None,
            includeAllUrls="true" if params.include_all_urls else None,
            includeHidden="true" if params.include_hidden else None,
        )

        response = await request_with_retry(
            app_ctx.http_client,
            "GET",
            url,
            headers=await build_headers(app_ctx),
            params=query_params,
        )
        response.raise_for_status()
        data = response.json()
        repos = data.get("value", [])
        return finalize_response({
            "repositories": repos,
            "count": data.get("count", len(repos)),
        })

    except ValueError as e:
        return finalize_response({"error": True, "message": str(e)})
    except httpx.HTTPStatusError as e:
        msg = extract_error_message(e.response)
        logger.error("Azure DevOps HTTP %d: %s", e.response.status_code, msg)
        return finalize_response({"error": True, "message": f"Azure DevOps returned HTTP {e.response.status_code}: {msg}"})
    except Exception as e:
        logger.exception("Unexpected error in devops_list_repositories")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


@mcp.tool(
    name="devops_get_repository",
    annotations={
        "title": "Get Repository",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def devops_get_repository(params: GetRepositoryInput, ctx: Context) -> str:
    """Get details of a specific Azure DevOps Git repository.

    Returns full repository metadata including ID, name, default branch,
    remote URL, SSH URL, web URL, size in bytes, fork status, maintenance
    status, and project information.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        url = build_url(organization, project, f"git/repositories/{params.repository_id}")

        response = await request_with_retry(
            app_ctx.http_client,
            "GET",
            url,
            headers=await build_headers(app_ctx),
            params=build_params(),
        )
        response.raise_for_status()
        return finalize_response(response.json())

    except ValueError as e:
        return finalize_response({"error": True, "message": str(e)})
    except httpx.HTTPStatusError as e:
        msg = extract_error_message(e.response)
        logger.error("Azure DevOps HTTP %d: %s", e.response.status_code, msg)
        return finalize_response({"error": True, "message": f"Azure DevOps returned HTTP {e.response.status_code}: {msg}"})
    except Exception as e:
        logger.exception("Unexpected error in devops_get_repository")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


@mcp.tool(
    name="devops_list_branches",
    annotations={
        "title": "List Branches",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def devops_list_branches(params: ListBranchesInput, ctx: Context) -> str:
    """List branches in an Azure DevOps Git repository.

    Returns branch names (in full ref format, e.g., refs/heads/main), commit
    SHAs, and creator information. Use filter_contains to narrow results to
    branches whose names contain a given substring.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        url = build_url(
            organization, project,
            f"git/repositories/{params.repository_id}/refs",
        )

        effective_top = params.top if params.top is not None else 100
        base_params = build_params(
            filter="heads/",
            filterContains=params.filter_contains,
            **{"$top": effective_top},
        )

        headers = await build_headers(app_ctx)
        branches, has_more = await paginate_results(
            app_ctx.http_client,
            url,
            headers,
            base_params,
            record_key="value",
            top=effective_top,
        )

        result: dict = {
            "branches": branches,
            "count": len(branches),
            "has_more": has_more,
        }
        return finalize_response(result)

    except ValueError as e:
        return finalize_response({"error": True, "message": str(e)})
    except httpx.HTTPStatusError as e:
        msg = extract_error_message(e.response)
        logger.error("Azure DevOps HTTP %d: %s", e.response.status_code, msg)
        return finalize_response({"error": True, "message": f"Azure DevOps returned HTTP {e.response.status_code}: {msg}"})
    except Exception as e:
        logger.exception("Unexpected error in devops_list_branches")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


@mcp.tool(
    name="devops_get_file_content",
    annotations={
        "title": "Get File Content",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def devops_get_file_content(params: GetFileContentInput, ctx: Context) -> str:
    """Retrieve the text content of a file from an Azure DevOps Git repository.

    Returns the raw text content of the specified file as a JSON object with
    path, content, and optional branch/commit_id fields. Binary files return
    an error — for an image (png/jpeg/gif/webp) use devops_get_repository_image
    instead, which returns it as viewable image content. Use branch or commit_id
    to read a specific version; omit both to use the repository's default branch.

    'content' is the file's own bytes decoded as text, not the Git item's
    metadata: the request sends $format=octetStream, without which Azure DevOps
    answers this route with a JSON description of the item instead of the blob.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        url = build_url(
            organization, project,
            f"git/repositories/{params.repository_id}/items",
        )

        # $format=octetStream is what makes this a file read at all; without it
        # the route returns the item's metadata JSON. See _BLOB_FORMAT_PARAM.
        query_params = build_params(path=params.path, **_BLOB_FORMAT_PARAM)
        if params.commit_id is not None:
            query_params["versionDescriptor.version"] = params.commit_id
            query_params["versionDescriptor.versionType"] = "commit"
        elif params.branch is not None:
            query_params["versionDescriptor.version"] = params.branch
            query_params["versionDescriptor.versionType"] = "branch"

        response = await request_with_retry(
            app_ctx.http_client,
            "GET",
            url,
            headers=await build_headers(app_ctx),
            params=query_params,
        )
        response.raise_for_status()

        content_type = response.headers.get("content-type", "").lower()
        is_binary = (
            content_type.startswith("image/")
            or sniff_image_format(response.content) is not None
        )
        text = None if is_binary else _decode_text_blob(response)
        if text is None:
            served_as = content_type or "unknown"
            return finalize_response({
                "error": True,
                "message": (
                    f"File '{params.path}' could not be decoded as UTF-8 text (Azure "
                    f"DevOps served it as '{served_as}'). It is either binary, or text "
                    "in another encoding — this route never declares a charset, so a "
                    "non-UTF-8 file cannot be decoded reliably and is refused rather "
                    "than mangled. If it is an image (png/jpeg/gif/webp), call "
                    "devops_get_repository_image with the same "
                    "repository_id/path/branch/commit_id to view it."
                ),
            })

        result: dict = {"path": params.path, "content": text}
        if params.commit_id is not None:
            result["commit_id"] = params.commit_id
        elif params.branch is not None:
            result["branch"] = params.branch

        return finalize_response(result)

    except ValueError as e:
        return finalize_response({"error": True, "message": str(e)})
    except httpx.HTTPStatusError as e:
        msg = extract_error_message(e.response)
        logger.error("Azure DevOps HTTP %d: %s", e.response.status_code, msg)
        return finalize_response({"error": True, "message": f"Azure DevOps returned HTTP {e.response.status_code}: {msg}"})
    except Exception as e:
        logger.exception("Unexpected error in devops_get_file_content")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


@mcp.tool(
    name="devops_get_repository_image",
    # structured_output=False is load-bearing, not decoration: the SDK cannot
    # derive an output schema for `Image`, so without it registration raises
    # PydanticSchemaGenerationError at import time — and "correcting" the
    # annotation to `-> str` instead produces a call-time ValidationError that
    # reads like an unrelated bug. See tests/test_repository_image.py.
    structured_output=False,
    annotations={
        "title": "Get Repository Image",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def devops_get_repository_image(params: GetRepositoryImageInput, ctx: Context) -> list[str | Image]:
    """Read an image file from a Git repository and return it as viewable content.

    Use this to actually look at a PNG/JPEG/GIF/WEBP committed to a repository —
    an architecture diagram in docs/, a chart, a design asset.
    devops_get_file_content cannot: it returns text and refuses binary. Address
    the file exactly as you would there (repository_id + path, plus branch or
    commit_id for a specific version; omit both for the default branch).

    Returns two content blocks for an image — a JSON metadata string and the
    image itself — and a single JSON block otherwise. Non-image content is
    reported as an error naming what was actually found and pointing back at
    devops_get_file_content; an image over the inline size limit is reported
    with its size rather than truncated. The format is decided by the file's
    magic bytes, falling back to the path's extension — never the response
    Content-Type, which Azure DevOps serves as application/octet-stream for
    binary blobs generally. A detected_by of "file_extension" on a file that
    really is an image is a signal worth reading: it means the bytes carried no
    signature.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        url = build_url(
            organization, project,
            f"git/repositories/{params.repository_id}/items",
        )

        # Identical request shape to devops_get_file_content, $format=octetStream
        # included: without it the route returns the item's metadata JSON rather
        # than the blob, and the JSON is plausible enough that the only tell is
        # detected_by falling back to "file_extension". See _BLOB_FORMAT_PARAM.
        query_params = build_params(path=params.path, **_BLOB_FORMAT_PARAM)
        if params.commit_id is not None:
            query_params["versionDescriptor.version"] = params.commit_id
            query_params["versionDescriptor.versionType"] = "commit"
        elif params.branch is not None:
            query_params["versionDescriptor.version"] = params.branch
            query_params["versionDescriptor.versionType"] = "branch"

        response = await request_with_retry(
            app_ctx.http_client,
            "GET",
            url,
            headers=await build_headers(app_ctx),
            params=query_params,
        )
        response.raise_for_status()

        content = response.content
        image_format, detected_by = detect_image_format(content, params.path)

        metadata: dict = {
            "path": params.path,
            "repository_id": params.repository_id,
            "size_bytes": len(content),
        }
        if params.commit_id is not None:
            metadata["commit_id"] = params.commit_id
        elif params.branch is not None:
            metadata["branch"] = params.branch

        if image_format is None:
            served_as = response.headers.get("content-type", "") or "unknown"
            return [
                finalize_response({
                    **metadata,
                    "error": True,
                    "is_image": False,
                    "content_type": served_as,
                    "message": (
                        f"'{params.path}' is not a viewable image: its bytes carry no "
                        "png/jpeg/gif/webp signature and its extension names no image "
                        f"format either (Azure DevOps served it as '{served_as}'). "
                        "Nothing was returned as image content. If it is a text file, "
                        "read it with devops_get_file_content — that includes SVG, "
                        "which Azure DevOps serves as text; other binary formats "
                        "(PDF, zip, ico) are not supported by either tool."
                    ),
                })
            ]

        if len(content) > _MAX_IMAGE_BYTES:
            return [
                finalize_response({
                    **metadata,
                    "error": True,
                    "message": (
                        f"'{params.path}' is {len(content):,} bytes, over the "
                        f"{_MAX_IMAGE_BYTES:,}-byte inline limit for image content "
                        "(base64 expansion would exceed the 5 MB response ceiling). "
                        "The bytes were not returned."
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
        status = e.response.status_code
        msg = extract_error_message(e.response)
        logger.error("Azure DevOps HTTP %d: %s", status, msg)
        if status == 404:
            return [
                finalize_response({
                    "error": True,
                    "message": (
                        f"'{params.path}' was not found in repository "
                        f"'{params.repository_id}' (HTTP 404). Check the path (it is "
                        "case-sensitive), the repository, and the branch or commit — "
                        "use devops_list_repository_items to browse. Azure DevOps said: "
                        f"{msg}"
                    ),
                })
            ]
        return [finalize_response({"error": True, "message": f"Azure DevOps returned HTTP {status}: {msg}"})]
    except Exception as e:
        logger.exception("Unexpected error in devops_get_repository_image")
        return [finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})]


@mcp.tool(
    name="devops_list_repository_items",
    annotations={
        "title": "List Repository Items",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def devops_list_repository_items(params: ListRepositoryItemsInput, ctx: Context) -> str:
    """List files and folders in an Azure DevOps Git repository.

    Returns item paths, object types (blob/tree), and folder flags. Use
    scope_path to browse a subfolder and recursion_level to control depth.
    Use branch or commit_id to read a specific version; omit both to use
    the repository's default branch. Pair with devops_get_file_content to
    read file contents after discovering paths.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        url = build_url(
            organization, project,
            f"git/repositories/{params.repository_id}/items",
        )

        query_params = build_params(
            scopePath=params.scope_path,
            recursionLevel=params.recursion_level,
        )
        if params.commit_id is not None:
            query_params["versionDescriptor.version"] = params.commit_id
            query_params["versionDescriptor.versionType"] = "commit"
        elif params.branch is not None:
            query_params["versionDescriptor.version"] = params.branch
            query_params["versionDescriptor.versionType"] = "branch"

        response = await request_with_retry(
            app_ctx.http_client,
            "GET",
            url,
            headers=await build_headers(app_ctx),
            params=query_params,
        )
        response.raise_for_status()
        data = response.json()
        items = data.get("value", data) if isinstance(data, dict) else data
        return finalize_response({"items": items, "count": len(items)})

    except ValueError as e:
        return finalize_response({"error": True, "message": str(e)})
    except httpx.HTTPStatusError as e:
        msg = extract_error_message(e.response)
        logger.error("Azure DevOps HTTP %d: %s", e.response.status_code, msg)
        return finalize_response({"error": True, "message": f"Azure DevOps returned HTTP {e.response.status_code}: {msg}"})
    except Exception as e:
        logger.exception("Unexpected error in devops_list_repository_items")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


@mcp.tool(
    name="devops_list_commits",
    annotations={
        "title": "List Commits",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def devops_list_commits(params: ListCommitsInput, ctx: Context) -> str:
    """List commits in an Azure DevOps Git repository.

    Returns commit IDs, authors, dates, and commit messages. Filter by branch,
    author, or date range. Use devops_get_commit to retrieve the full detail
    and file changes for a specific commit.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        url = build_url(
            organization, project,
            f"git/repositories/{params.repository_id}/commits",
        )

        query_params = build_params(**{"$top": params.top})
        if params.branch is not None:
            query_params["searchCriteria.itemVersion.version"] = params.branch
            query_params["searchCriteria.itemVersion.versionType"] = "branch"
        if params.author is not None:
            query_params["searchCriteria.author"] = params.author
        if params.from_date is not None:
            query_params["searchCriteria.fromDate"] = params.from_date
        if params.to_date is not None:
            query_params["searchCriteria.toDate"] = params.to_date

        response = await request_with_retry(
            app_ctx.http_client,
            "GET",
            url,
            headers=await build_headers(app_ctx),
            params=query_params,
        )
        response.raise_for_status()
        data = response.json()
        commits = data.get("value", data) if isinstance(data, dict) else data
        return finalize_response({"commits": commits, "count": len(commits)})

    except ValueError as e:
        return finalize_response({"error": True, "message": str(e)})
    except httpx.HTTPStatusError as e:
        msg = extract_error_message(e.response)
        logger.error("Azure DevOps HTTP %d: %s", e.response.status_code, msg)
        return finalize_response({"error": True, "message": f"Azure DevOps returned HTTP {e.response.status_code}: {msg}"})
    except Exception as e:
        logger.exception("Unexpected error in devops_list_commits")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


@mcp.tool(
    name="devops_get_commit",
    annotations={
        "title": "Get Commit",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def devops_get_commit(params: GetCommitInput, ctx: Context) -> str:
    """Get details of a specific commit from an Azure DevOps Git repository.

    Returns the commit ID, author, committer, date, message, and parents.
    Set change_count to include file changes (paths and change types) in the
    response.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        url = build_url(
            organization, project,
            f"git/repositories/{params.repository_id}/commits/{params.commit_id}",
        )

        response = await request_with_retry(
            app_ctx.http_client,
            "GET",
            url,
            headers=await build_headers(app_ctx),
            params=build_params(changeCount=params.change_count),
        )
        response.raise_for_status()
        return finalize_response(response.json())

    except ValueError as e:
        return finalize_response({"error": True, "message": str(e)})
    except httpx.HTTPStatusError as e:
        msg = extract_error_message(e.response)
        logger.error("Azure DevOps HTTP %d: %s", e.response.status_code, msg)
        return finalize_response({"error": True, "message": f"Azure DevOps returned HTTP {e.response.status_code}: {msg}"})
    except Exception as e:
        logger.exception("Unexpected error in devops_get_commit")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})

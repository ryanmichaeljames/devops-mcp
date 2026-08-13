"""Saved query (query hierarchy) tools for Azure DevOps MCP.

The wit/queries operation group is the discovery layer over WIQL: it names,
organises and persists the queries a project actually curates. Four things
about it differ from the neighbouring work-items module and are easy to get
backwards:

1. **PATCH here is merge-patch with ``Content-Type: application/json``** — you
   send a partial ``QueryHierarchyItem`` holding only the keys you are changing.
   ``PATCH wit/workitems/{id}``, one directory away, requires
   ``application/json-patch+json`` and rejects plain JSON. Copying that module's
   PATCH gets a 400 whose message says nothing about content type. There is also
   no ``test /rev`` and no ETag on this resource: no optimistic concurrency at
   all, last writer wins.
2. **``{query}`` is a GUID *or* a multi-segment path** (``Shared Queries/Team/All
   Bugs``). Its ``/`` are real route separators, so it is encoded with
   ``quote(path, safe="/")`` — which is exactly what ``build_url`` does to its
   ``path`` argument. Encoding it with ``safe=""`` yields ``%2F`` and a **404 for
   a query that plainly exists**, a failure that reads as "missing query" rather
   than "bad encoding".
3. **``$`` prefixes are inconsistent inside one operation group**: ``$depth``,
   ``$expand``, ``$filter``, ``$top``, ``$includeDeleted`` and
   ``$undeleteDescendants`` all take one; ``validateWiqlOnly`` does not. A
   dropped (or added) ``$`` is a silently ignored parameter, not an error.
4. **``$includeDeleted`` only works with a GUID reference** (verified live).
   Asking for a soft-deleted item *by path* answers ``404
   QueryItemNotFoundException / TF401243`` even with ``$includeDeleted=true`` on
   the wire; the same item by GUID answers 200 with ``isDeleted: true``. Delete
   therefore costs you the path as an address, and the only way back is to list
   the parent folder with ``include_deleted`` and read the GUID off the child.
5. **Renaming a ROOT folder is permitted by the service and bricks path
   addressing** (verified live). ``PATCH wit/queries/Shared Queries`` with
   ``{"name": "whatever"}`` answers 200 and the root really is renamed — after
   which no path under it starts with ``Shared Queries``/``My Queries`` any more,
   so every path-addressed call fails and only GUIDs still work.  Nothing in the
   API refuses it, so ``devops_update_query`` does: see ``_is_root_folder``.  The
   guard turns on the item's OWN identity, not on the name being set — a
   name-only test ("is the new name a root name?") permits renaming a healthy
   root onto the other root's name, which is worse than the damage it guards
   against.  Only a root that is already damaged may be renamed, and only back to
   a root name that is currently vacant: see ``_root_guard_error``.
"""

import logging
import uuid
from urllib.parse import quote

import httpx
from mcp.server.mcpserver import Context

from devops_mcp._app import delete_tool, mcp, write_tool
from devops_mcp.client import (
    AppContext,
    build_headers,
    build_url,
    extract_error_message,
    finalize_response,
    request_with_retry,
    resolve_org,
    resolve_project,
)
from devops_mcp.models import (
    CreateQueryFolderInput,
    CreateQueryInput,
    DeleteQueryInput,
    GetQueryInput,
    ListQueriesInput,
    RunQueryInput,
    UpdateQueryInput,
)

logger = logging.getLogger(__name__)

# The whole wit/queries group (list, search, get, create, update, delete) shares
# ONE revision, unlike the rest of WIT where every operation needs its own
# -preview.N.  Wiql query-by-id happens to be the same string today; it is kept
# as a separate constant so a future bump of one cannot silently move the other.
#
# build_params() is deliberately never used in this module: it hard-codes
# api-version=7.1, which is a *different* revision of these routes and mostly
# "works", so the mistake is invisible at runtime.  Every request below passes an
# explicit params dict.
_QUERIES_API_VERSION = "7.2-preview.2"
_WIQL_API_VERSION = "7.2-preview.2"
_WORKITEMS_BATCH_API_VERSION = "7.2-preview.1"

# Hard service cap on ids per workitemsbatch call.
_BATCH_CHUNK_SIZE = 200

# Azure DevOps Services caps a query result at 20,000 items.  The docs say the
# result is silently truncated; the field reports VS402337 instead.  Both are
# handled — an exact-20,000 result is flagged rather than presented as complete.
_QUERY_RESULT_LIMIT = 20000

_DEFAULT_HYDRATION_FIELDS = [
    "System.Id",
    "System.WorkItemType",
    "System.Title",
    "System.State",
    "System.AssignedTo",
]

# The only two roots of the query hierarchy.  Any other first segment is a 404
# that reads like a missing query, so it is rejected before a request is made.
_QUERY_ROOTS = ("shared queries", "my queries")


# ---------------------------------------------------------------------------
# Reference (GUID-or-path) handling
# ---------------------------------------------------------------------------


def _is_guid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def _validate_query_ref(value: str, field: str) -> str:
    """Validate a query reference — a GUID, or a path in the query hierarchy.

    Returns the caller's value unchanged (the service is tolerant about casing;
    humans are not, so the supplied casing is what gets sent).  Raises
    ``ValueError`` — which every tool below turns into the standard JSON error —
    for the shapes that would otherwise come back as a confusing 404.
    """
    ref = (value or "").strip()
    if not ref:
        raise ValueError(
            f"'{field}' must be a query GUID or a query path such as "
            "'Shared Queries/My Query'."
        )
    if _is_guid(ref):
        return ref
    if ref.startswith("/") or ref.endswith("/"):
        raise ValueError(
            f"'{field}' must not start or end with '/'; got {ref!r}. A query path "
            "looks like 'Shared Queries/Website team/All Bugs'."
        )
    segments = ref.split("/")
    for segment in segments:
        stripped = segment.strip()
        if not stripped:
            raise ValueError(
                f"'{field}' contains an empty path segment (a doubled '/'); got {ref!r}."
            )
        if stripped in (".", ".."):
            raise ValueError(
                f"'{field}' must not contain '.' or '..' path segments; got {ref!r}."
            )
    if segments[0].strip().lower() not in _QUERY_ROOTS:
        raise ValueError(
            f"'{field}' must be a query GUID, or a path starting with "
            f"'Shared Queries' or 'My Queries'; got {ref!r}. Those are the only two "
            "roots of the Azure DevOps query hierarchy — any other first segment "
            "returns a 404 that looks like a missing query."
        )
    return ref


def _is_root_path(ref: str) -> bool:
    """True when *ref* is a path naming a root folder ('Shared Queries').

    Only usable on a ref that already passed ``_validate_query_ref``: that
    guarantees the first segment is a root name, so "one segment" means "a root".
    A GUID is not a path and never matches here — use ``_is_root_folder`` on the
    fetched item for that.
    """
    return not _is_guid(ref) and "/" not in ref.strip()


def _root_label(item: dict) -> str:
    """The single-segment path (else name) that a root folder answers to."""
    return str(item.get("path") or item.get("name") or "").strip().strip("/")


def _is_root_folder(item: dict) -> bool:
    """True when a fetched QueryHierarchyItem is one of the two hierarchy roots.

    The durable signal is ``isFolder`` plus a SINGLE-SEGMENT ``path`` — every
    non-root item's path carries its whole parent chain, so only a root can have
    a path with no ``/`` in it.  Name-matching is not usable: a root that has
    already been renamed reports ``{"name": "whatever", "path": "whatever"}`` and
    looks nothing like a root, yet it is still the root (verified live), and it
    is exactly the item that most needs guarding.
    """
    if not isinstance(item, dict) or not item.get("isFolder"):
        return False
    label = _root_label(item)
    return bool(label) and "/" not in label


def _is_damaged_root(item: dict) -> bool:
    """True when *item* is a hierarchy root that no longer answers to a root name.

    This is the only state in which a root rename is a REPAIR rather than fresh
    damage.  A healthy root's label is ``Shared Queries``/``My Queries``; a
    damaged one's is whatever it was renamed to, and nothing beneath it can be
    reached by path until that is put back.
    """
    return _is_root_folder(item) and _root_label(item).lower() not in _QUERY_ROOTS


def _root_guard_error(item: dict, *, new_name: str | None, moving: bool) -> str | None:
    """Refuse the operations that would make a root folder unaddressable.

    Azure DevOps accepts a root rename with a plain 200 and no warning, and the
    damage is not local to the root: ``_validate_query_ref`` (and the service's
    own path routing) require a path's first segment to be ``Shared Queries`` or
    ``My Queries``, so renaming the root strands EVERY query beneath it — they
    stay addressable only by GUID.  There is no server-side undo beyond renaming
    it back, so exactly one rename stays allowed: the REPAIR of an
    already-damaged root.

    The permitted case is defined by the ITEM'S OWN IDENTITY, never by the new
    name alone.  Asking only "is the new name a valid root name?" lets through
    ``rename('Shared Queries', 'My Queries')`` — a healthy root swapped onto the
    other root's name, which collides two roots under one path and, unlike the
    original damage, has no single clean owner of that path to rename back.  So:

    * healthy root -> anything (including the OTHER root's name): refused;
    * damaged root -> a non-root name: refused (it is not a repair);
    * damaged root -> a root name: allowed, subject to the caller also checking
      that the target name is not already held by the other live root
      (``_root_repair_conflict`` — that needs HTTP and cannot live here).
    """
    if not _is_root_folder(item):
        return None
    current = _root_label(item)
    if moving:
        return (
            f"'{current}' is a ROOT folder of the query hierarchy and cannot be moved. "
            "'Shared Queries' and 'My Queries' have no parent, and re-parenting one "
            "would leave every query beneath it addressable only by GUID. Move the "
            "folders or queries INSIDE it instead."
        )
    if new_name is None:
        return None
    target = new_name.strip()
    if not _is_damaged_root(item):
        return (
            f"Refusing to rename the ROOT folder '{current}' to '{target}'. Azure "
            "DevOps accepts this (HTTP 200) but it breaks PATH addressing for every "
            "query and folder underneath it: a query path must start with 'Shared "
            "Queries' or 'My Queries', so after the rename nothing below this root "
            "can be reached by path and only GUIDs still work. That includes renaming "
            "it to the OTHER root's name — that is not a repair, it puts two roots on "
            "one path with no clean way back. This root is intact and needs no repair. "
            "Rename a folder INSIDE the root instead."
        )
    if target.lower() not in _QUERY_ROOTS:
        return (
            f"Refusing to rename the ROOT folder '{current}' to '{target}'. This is a "
            "ROOT of the query hierarchy that has ALREADY been renamed away from "
            "'Shared Queries'/'My Queries', which is why nothing beneath it can be "
            "reached by path any more — renaming it to another arbitrary name does not "
            "fix that. The only rename allowed on a root is the repair, back to a root "
            "name: devops_update_query(query=<root guid>, name='Shared Queries') (or "
            "'My Queries'), whichever of the two is not currently in use."
        )
    return None


def _queries_url(organization: str, project: str, ref: str | None = None) -> str:
    """Build the wit/queries URL, preserving '/' inside a query path.

    ``build_url`` encodes its ``path`` argument with ``quote(path, safe="/")``,
    which is precisely the contract a query path needs: spaces become ``%20``,
    the separators stay literal.
    """
    path = "wit/queries" if ref is None else f"wit/queries/{ref}"
    return build_url(organization, project, path)


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------

_ITEM_PASSTHROUGH = (
    ("hasChildren", "has_children"),
    ("queryType", "query_type"),
    ("wiql", "wiql"),
    ("isPublic", "is_public"),
    ("isDeleted", "is_deleted"),
    ("isInvalidSyntax", "is_invalid_syntax"),
    ("createdDate", "created_date"),
    ("lastModifiedDate", "last_modified_date"),
    ("lastExecutedDate", "last_executed_date"),
)

_CLAUSE_KEYS = ("clauses", "sourceClauses", "targetClauses", "linkClauses")


def _project_query_item(item: dict, *, include_clauses: bool = False) -> dict:
    """Reduce a QueryHierarchyItem to the fields a model can act on.

    An unprojected item is mostly identity boilerplate (imageUrl, descriptor,
    avatar links, self/parent/wiql links); a few nested folders of them bury the
    useful content.  Absent keys stay absent rather than becoming nulls, because
    which keys arrive depends on ``$expand`` in a way that is NOT monotonic.
    Measured live off one query, same api-version:

    * ``none``    -> ``_links, createdBy, createdDate, id, isPublic,
      lastModifiedBy, lastModifiedDate, name, path, queryType, url``
    * ``minimal`` -> ``id, isPublic, name, path, queryType, wiql``
    * ``wiql``    -> everything ``none`` returns **plus** ``columns`` and ``wiql``
    * ``all``     -> everything ``wiql`` returns **plus** ``clauses``

    So ``minimal`` is not a subset of ``none`` (it alone carries ``wiql``) and
    ``none`` is not a subset of ``minimal`` (it alone carries the dates, the
    author and the links).  Two easy misreadings, both wrong: ``columns`` does
    NOT arrive at ``none`` — only at ``wiql``/``all`` — and ``minimal`` does NOT
    drop ``queryType``, which is present at every level.  No key here may be
    treated as always-present.  ``isDeleted`` is another conditional one: it only
    appears when ``$includeDeleted=true`` (and, live, only for a GUID-addressed
    item).
    """
    if not isinstance(item, dict) or not item:
        # An empty body (a validate-only create, a bodiless 204) projects to
        # nothing rather than to a row of nulls.
        return {}

    projected: dict = {
        "id": item.get("id"),
        "name": item.get("name"),
        "path": item.get("path"),
        "is_folder": bool(item.get("isFolder", False)),
    }
    for source_key, dest_key in _ITEM_PASSTHROUGH:
        if item.get(source_key) is not None:
            projected[dest_key] = item[source_key]

    columns = item.get("columns")
    if isinstance(columns, list) and columns:
        projected["columns"] = [
            c.get("referenceName") for c in columns if isinstance(c, dict) and c.get("referenceName")
        ]

    sort_columns = item.get("sortColumns")
    if isinstance(sort_columns, list) and sort_columns:
        projected["sort_columns"] = [
            {
                "field": (s.get("field") or {}).get("referenceName"),
                "descending": bool(s.get("descending", False)),
            }
            for s in sort_columns
            if isinstance(s, dict)
        ]

    created_by = item.get("createdBy")
    if isinstance(created_by, dict) and created_by.get("displayName"):
        projected["created_by"] = created_by["displayName"]

    html_href = ((item.get("_links") or {}).get("html") or {}).get("href")
    if html_href:
        projected["html_url"] = html_href

    if include_clauses:
        for key in _CLAUSE_KEYS:
            if item.get(key) is not None:
                projected[key] = item[key]

    children = item.get("children")
    if isinstance(children, list):
        projected["children"] = [
            _project_query_item(child, include_clauses=include_clauses) for child in children
        ]

    return projected


def _extract_items(payload) -> list[dict]:
    """Normalise the list/search response into a list of items.

    The Search op answers a ``{count, value, hasMore}`` object.  The List op is
    documented as a bare array; some revisions wrap it in the usual
    ``{count, value}`` envelope.  Both shapes are accepted here rather than
    branching on the response type, so a change on either op cannot turn a
    populated hierarchy into a silent empty list.
    """
    if isinstance(payload, list):
        return [i for i in payload if isinstance(i, dict)]
    if isinstance(payload, dict):
        value = payload.get("value")
        if isinstance(value, list):
            return [i for i in value if isinstance(i, dict)]
    return []


# ---------------------------------------------------------------------------
# Run-result normalisation
# ---------------------------------------------------------------------------


def _extract_run_ids(result: dict) -> tuple[list[int], list[dict]]:
    """Return (de-duplicated ordered work item ids, projected relations).

    GOTCHA this exists for: a ``flat`` query answers ``workItems``, while
    ``tree`` and ``oneHop`` answer ``workItemRelations`` and omit ``workItems``
    ENTIRELY.  ``result.get("workItems", [])`` therefore reports zero results for
    a query that returned hundreds — a confident wrong answer, which is worse
    than a crash.  Relation rows also repeat ids (once per link) and the *root*
    rows carry neither ``rel`` nor ``source``, so ids are de-duplicated before
    they are fed to the 200-per-call batch endpoint.
    """
    ids: list[int] = []
    seen: set[int] = set()

    def _add(work_item_id) -> None:
        if isinstance(work_item_id, int) and work_item_id not in seen:
            seen.add(work_item_id)
            ids.append(work_item_id)

    relations_raw = result.get("workItemRelations")
    if isinstance(relations_raw, list):
        relations: list[dict] = []
        for row in relations_raw:
            if not isinstance(row, dict):
                continue
            source = row.get("source") or {}
            target = row.get("target") or {}
            source_id = source.get("id") if isinstance(source, dict) else None
            target_id = target.get("id") if isinstance(target, dict) else None
            # A row with no rel/source_id is a root row; the keys are omitted
            # rather than set to null so that shape is visible to the caller.
            projected: dict = {}
            if row.get("rel") is not None:
                projected["rel"] = row["rel"]
            if source_id is not None:
                projected["source_id"] = source_id
            projected["target_id"] = target_id
            relations.append(projected)
            _add(source_id)
            _add(target_id)
        return ids, relations

    for row in result.get("workItems") or []:
        if isinstance(row, dict):
            _add(row.get("id"))
    return ids, []


def _run_row_count(result: dict) -> int:
    """Raw row count of a run result, whichever shape it came back in."""
    relations = result.get("workItemRelations")
    if isinstance(relations, list):
        return len(relations)
    work_items = result.get("workItems")
    return len(work_items) if isinstance(work_items, list) else 0


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def _query_error_message(
    status: int, msg: str, *, subject: str, project: str, team: str | None = None
) -> str:
    """Turn an Azure DevOps queries error into something a model can act on."""
    if "TeamNotFoundException" in msg:
        # Verified live: an unknown team on the wiql route answers HTTP *500*,
        # not 404 — a caller input error dressed up as a server fault.  Left out
        # of the retry policy deliberately (500 is not retried here), so this
        # only has to be renamed, not rate-limited.
        named = f" '{team}'" if team else ""
        return (
            f"The team{named} was not found in project '{project}'. Azure DevOps answers "
            "HTTP 500 (not 404) for an unknown team on this route, so this is an input "
            "error, not an outage. List the project's teams with devops_list_teams, or "
            "drop 'team' to run against the project's default team. Azure DevOps said: "
            f"{msg}"
        )
    if "TF237018" in msg:
        # Verified live: this arrives as HTTP *400*, never 409 — match on the
        # code, not the status.
        return (
            f"{subject} could not be saved in project '{project}': a query or query "
            "folder with that name already exists in the destination folder. Choose a "
            "different name, or update the existing item instead. "
            f"Azure DevOps said: {msg}"
        )
    if "TF237023" in msg:
        # Verified live: deleting a ROOT folder is refused server-side with this
        # code at HTTP *400* (not 403), and its typeKey is the generic
        # LegacyQueryItemException — the same one TF237018 rides.  So match the
        # code, never the status or the exception type.  No client-side root
        # guard is needed for delete because the service enforces it; contrast
        # the root *rename*, which the service allows and devops_update_query
        # has to refuse itself.
        return (
            f"{subject} is a ROOT query folder ('Shared Queries' / 'My Queries'), and "
            "Azure DevOps refuses to delete a root — the two roots are fixed, and every "
            "query path has to start at one of them. Nothing was deleted. Delete the "
            "folders and queries INSIDE the root individually instead: list them with "
            "devops_list_queries (or devops_get_query with depth=1) on the root, then "
            f"delete each child by its path or GUID. Azure DevOps said: {msg}"
        )
    if "Querying folders is not supported" in msg:
        # A path-addressed folder is caught on the resolve hop before any run
        # request goes out; a GUID-addressed one can only be caught here, since
        # running by GUID costs no resolve call to inspect isFolder.
        return (
            f"{subject} is a query FOLDER, and a folder cannot be run. List what is "
            "inside it with devops_list_queries (or devops_get_query with depth=1) and "
            f"run one of the queries it contains. Azure DevOps said: {msg}"
        )
    if "VS402337" in msg:
        return (
            f"{subject} returned more than the Azure DevOps limit of "
            f"{_QUERY_RESULT_LIMIT:,} work items. Narrow the query (add a state, area "
            "path, or changed-date filter) and run it again — 'top' does not help, the "
            f"cap applies to the whole result set. Azure DevOps said: {msg}"
        )
    if status == 404:
        return (
            f"{subject} was not found in project '{project}'. Check the GUID, or the "
            "path (query paths start with 'Shared Queries' or 'My Queries' and are "
            "case-sensitive on some orgs). Query delete is soft, but a soft-deleted "
            "item is only addressable by its GUID — include_deleted does NOT make its "
            "path resolve, that stays a 404. To recover one: call devops_list_queries "
            "(or devops_get_query with depth=1) on the PARENT folder with "
            "include_deleted=true, read the GUID off the deleted child, then restore it "
            "with devops_update_query(query=<guid>, undelete=True). Azure DevOps said: "
            f"{msg}"
        )
    if status in (401, 403):
        return (
            f"Not authorized (HTTP {status}) for {subject} in project '{project}'. "
            "Saving under 'Shared Queries' needs Contribute on the target folder AND at "
            "least Basic access (Basic alone is not enough); renaming or moving needs "
            "Delete on the source folder plus Contribute on the destination. The token "
            f"must also carry 'vso.work_write'. Azure DevOps said: {msg}"
        )
    return f"Azure DevOps returned HTTP {status}: {msg}"


def _parse_optional_json(response: httpx.Response) -> dict:
    """Read a JSON object body when there is one, else {}.

    Delete documents a ``200 OK`` but samples a bodiless ``204``, and a
    validate-only create may answer without a body — never let a missing body
    downgrade a completed operation into a reported failure.
    """
    if response.status_code == 204 or not response.content:
        return {}
    try:
        parsed = response.json()
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


# ---------------------------------------------------------------------------
# Tools — read
# ---------------------------------------------------------------------------


@mcp.tool(
    name="devops_list_queries",
    annotations={
        "title": "List Queries",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def devops_list_queries(params: ListQueriesInput, ctx: Context) -> str:
    """Browse the saved-query hierarchy, or search saved queries by name.

    Two modes over one endpoint:

    - Hierarchy (default): returns the two root folders, 'My Queries' and
      'Shared Queries', with their children nested to 'depth'. Use this to
      discover what a team has curated.
    - Search: set 'name_filter' to match QUERY names anywhere in the project.
      Results are flat, capped at 'top' (max 200), and there is NO continuation
      token — if 'has_more' is true, narrow the filter rather than paging.

    Search matches query names ONLY — folder names are not searched (verified
    live: a filter matching two folders exactly returned zero results). To find a
    folder, browse the hierarchy instead (omit 'name_filter' and use 'depth').

    expand is not a simple "more is more" (key sets measured live):
    expand='none' (the default here) returns query_type, created_by and the
    created/modified dates but NO wiql and NO columns; expand='minimal' returns
    wiql and query_type but drops created_by and the dates; expand='wiql' is
    'none' plus wiql AND columns; 'all' adds the clause tree. Columns arrive only
    at 'wiql'/'all' — ask for one of those when you need the text and the
    metadata together. 'My Queries' is the personal folder of the identity this
    server authenticates as, which may not be your own.

    include_deleted surfaces soft-deleted children here — and this is the ONLY
    way to recover one, because a soft-deleted item cannot be addressed by path;
    read its GUID off this listing and use that everywhere afterwards.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    project = params.project or ""
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        url = _queries_url(organization, project)

        search_mode = params.name_filter is not None
        query_params: dict = {
            "api-version": _QUERIES_API_VERSION,
            "$expand": params.expand,
        }
        if search_mode:
            # $filter is what switches this route from hierarchy to search.
            query_params["$filter"] = params.name_filter
            query_params["$top"] = params.top
        else:
            query_params["$depth"] = params.depth
        if params.include_deleted:
            query_params["$includeDeleted"] = "true"

        response = await request_with_retry(
            app_ctx.http_client,
            "GET",
            url,
            headers=await build_headers(app_ctx),
            params=query_params,
        )
        response.raise_for_status()
        payload = response.json()

        include_clauses = params.expand in ("clauses", "all")
        items = [
            _project_query_item(item, include_clauses=include_clauses)
            for item in _extract_items(payload)
        ]

        result: dict = {
            "mode": "search" if search_mode else "hierarchy",
            "count": len(items),
            "queries": items,
        }
        if search_mode:
            has_more = bool(payload.get("hasMore")) if isinstance(payload, dict) else False
            result["has_more"] = has_more
            if has_more:
                result["note"] = (
                    "More matches exist than were returned and this endpoint has no "
                    "continuation token — narrow 'name_filter' to see the rest."
                )
        return finalize_response(result)

    except ValueError as e:
        return finalize_response({"error": True, "message": str(e)})
    except httpx.HTTPStatusError as e:
        msg = extract_error_message(e.response)
        logger.error("Azure DevOps HTTP %d: %s", e.response.status_code, msg)
        return finalize_response({
            "error": True,
            "message": _query_error_message(
                e.response.status_code, msg, subject="The query list", project=project
            ),
        })
    except Exception as e:
        logger.exception("Unexpected error in devops_list_queries")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


@mcp.tool(
    name="devops_get_query",
    annotations={
        "title": "Get Query",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def devops_get_query(params: GetQueryInput, ctx: Context) -> str:
    """Read one saved query or folder — including its WIQL — by GUID or by path.

    A path looks like 'Shared Queries/Website team/All Bugs'; the only two roots
    are 'Shared Queries' and 'My Queries'. The default expand='minimal' includes
    the WIQL text, which is usually the reason to read a query at all — but it
    DROPS created_by and the created/modified dates, which expand='none' returns
    (query_type comes back at every level). Neither of those two levels returns
    columns: measured live, columns arrive only at expand='wiql' or 'all', which
    also return the WIQL. Pass 'wiql' when you want the text plus the metadata.

    If 'is_invalid_syntax' comes back true the query is saved but broken (bad
    WIQL, or a deleted area/iteration path): it reads fine and fails only when
    run.

    Query delete is soft, so a 404 may mean "recently deleted" — but
    include_deleted only helps when 'query' is a GUID. A soft-deleted item's PATH
    stays a 404 forever (verified live). To find one, call devops_list_queries
    with include_deleted=True, or call this tool on the PARENT folder with
    depth=1 and include_deleted=True, then address the deleted child by its GUID.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    project = params.project or ""
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        ref = _validate_query_ref(params.query, "query")
        url = _queries_url(organization, project, ref)

        query_params: dict = {
            "api-version": _QUERIES_API_VERSION,
            "$expand": params.expand,
        }
        if params.depth:
            query_params["$depth"] = params.depth
        if params.include_deleted:
            query_params["$includeDeleted"] = "true"

        response = await request_with_retry(
            app_ctx.http_client,
            "GET",
            url,
            headers=await build_headers(app_ctx),
            params=query_params,
        )
        response.raise_for_status()

        item = _project_query_item(
            response.json(), include_clauses=params.expand in ("clauses", "all")
        )
        if item.get("is_invalid_syntax"):
            item["warning"] = (
                "This query is saved but its WIQL is invalid (bad syntax, or it "
                "references a deleted area/iteration path). Running it will fail until "
                "it is fixed with devops_update_query(wiql=...)."
            )
        return finalize_response(item)

    except ValueError as e:
        return finalize_response({"error": True, "message": str(e)})
    except httpx.HTTPStatusError as e:
        msg = extract_error_message(e.response)
        logger.error("Azure DevOps HTTP %d: %s", e.response.status_code, msg)
        return finalize_response({
            "error": True,
            "message": _query_error_message(
                e.response.status_code, msg, subject=f"Query '{params.query}'", project=project
            ),
        })
    except Exception as e:
        logger.exception("Unexpected error in devops_get_query")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


async def _resolve_query_id(
    app_ctx: AppContext,
    organization: str,
    project: str,
    ref: str,
    *,
    must_be_runnable: bool = False,
) -> tuple[str, dict]:
    """Resolve a query reference to (GUID, projected item).

    A GUID reference costs no HTTP call — the saved-query run route is typed
    ``uuid`` and has no path-addressed form, so only a path needs the extra hop.

    Two guards live here because the value flows into a URL afterwards:

    * ``must_be_runnable`` rejects a **folder** client-side.  Folders have ids
      too, so "did it come back with an id?" never catches one; the resolved
      item's ``isFolder`` is the real test.  Without it a run falls through to
      the wiql route and the caller gets a generic ``400 QueryException:
      Querying folders is not supported`` after a wasted round trip.  It is
      opt-in because *moving* a folder is perfectly legal and resolves an id the
      same way.
    * The id is **GUID-rechecked**.  It is server JSON heading for a hand-built
      URL (the team-segment run form), and the run route is typed ``uuid``
      anyway, so anything that is not a GUID is a client-side error rather than
      a request.
    """
    if _is_guid(ref):
        return str(uuid.UUID(ref)), {}

    response = await request_with_retry(
        app_ctx.http_client,
        "GET",
        _queries_url(organization, project, ref),
        headers=await build_headers(app_ctx),
        params={"api-version": _QUERIES_API_VERSION},
    )
    response.raise_for_status()
    payload = response.json()
    item: dict = payload if isinstance(payload, dict) else {}
    if must_be_runnable and item.get("isFolder"):
        raise ValueError(
            f"'{ref}' is a query FOLDER, not a query — a folder cannot be run "
            "(Azure DevOps answers 'Querying folders is not supported'). List what is "
            "inside it with devops_list_queries, or devops_get_query(query=..., "
            "depth=1), then run one of the queries it contains."
        )
    query_id = item.get("id")
    if not query_id or not _is_guid(str(query_id)):
        raise ValueError(
            f"Azure DevOps returned no usable id for query '{ref}' (got {query_id!r}), "
            "so it cannot be used here — this route is typed as a GUID. Re-read the "
            "item with devops_get_query and use the 'id' it reports."
        )
    return str(uuid.UUID(str(query_id))), _project_query_item(item)


def _renamed_ref(ref: str, new_name: str | None) -> str:
    """The reference that addresses *ref* AFTER a rename to *new_name*.

    A rename replaces the path's leaf and nothing else, so the post-rename path
    is derivable without a round trip.  This exists so no code path ever looks an
    item up by a reference the tool itself has just invalidated — a stale path
    404s and the failure reads as "the query vanished".  A GUID never goes stale.

    The no-separator branch (a single-segment ref, i.e. a ROOT folder) is
    effectively unreachable: ``devops_update_query`` refuses every rename of a
    root except the repair of an already-damaged one, and a damaged root is
    addressable only by GUID (which returns early above). It is kept because the
    branch is cheap and a stale-path lookup is the failure it exists to prevent.
    """
    if new_name is None or _is_guid(ref):
        return ref
    head, sep, _leaf = ref.rpartition("/")
    return f"{head}{sep}{new_name}" if sep else new_name


async def _read_current_item(
    app_ctx: AppContext, organization: str, project: str, ref: str, *, required: bool
) -> dict | None:
    """Read the item as it stands BEFORE the update; ``None`` when unreadable.

    One GET serves two different pre-checks, so an update that needs both (a
    rename plus an undelete) still pays for only one round trip:

    * ``isDeleted`` — undelete only cascades at the *transition*: re-issuing
      ``isDeleted=false`` (even with ``$undeleteDescendants``) against an item
      that is ALREADY live answers 200 and does nothing at all, verified live,
      with the folder's descendants left deleted.  Reporting "restored" off the
      200 alone is a confidently wrong answer, so the state is read first.
    * ``isFolder``/``path`` — the root-folder guard (see ``_root_guard_error``).
      A GUID hides what it points at, so a GUID-addressed rename or move has to
      look before it leaps.

    ``required`` is what separates the two.  The undelete pre-check is
    best-effort: a failure there must not fail the update the caller actually
    asked for, so it degrades to ``None`` ("unknown") and the report hedges.  The
    root guard is not optional — an unreadable item is one this tool must not
    rename blind, and the same error would have met the PATCH anyway, so the
    HTTPStatusError is left to propagate into the standard error mapping.

    ``$includeDeleted=true`` is sent because a soft-deleted item must still be
    readable here; a 200 whose body simply omits ``isDeleted`` means live.

    A 200 whose body is not an OBJECT (``[]``, ``null``, a string) is treated the
    same as an unreadable one: with ``required`` set, the root guard would
    otherwise be handed ``{}``, decide "not a root", and wave the rename through.
    A guard that fails open on a surprising body is not a guard.
    """
    try:
        response = await request_with_retry(
            app_ctx.http_client,
            "GET",
            _queries_url(organization, project, ref),
            headers=await build_headers(app_ctx),
            params={"api-version": _QUERIES_API_VERSION, "$includeDeleted": "true"},
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:
        if required:
            raise
        logger.warning("Could not read the pre-update state of query %r", ref, exc_info=True)
        return None
    if isinstance(payload, dict):
        return payload
    if required:
        raise ValueError(
            f"Azure DevOps answered HTTP 200 for query '{ref}' with a "
            f"{type(payload).__name__} instead of a query item, so what this "
            "reference points at could not be established. The request was refused "
            "rather than issued blind (renaming or moving a ROOT folder breaks path "
            "addressing for everything beneath it). Re-read the item with "
            "devops_get_query and try again."
        )
    logger.warning("Pre-update read of query %r returned a non-object body", ref)
    return None


async def _root_repair_conflict(
    app_ctx: AppContext,
    organization: str,
    project: str,
    item: dict,
    new_name: str,
) -> str | None:
    """Refuse a root repair whose target name is already held by the OTHER root.

    ``_root_guard_error`` has already established that *item* is a damaged root
    and that *new_name* is a root name, so the only remaining question is whether
    that name is free.  It usually is — the damaged root is the one that vacated
    it — but a caller can just as easily aim the repair at the root that is still
    healthy, which would put two roots on one path.

    Only this case pays for the extra call, and only ever on the GUID branch: a
    healthy root is refused before any HTTP, and a non-root item never reaches
    here.  The bare ``wit/queries`` list is the cheapest answer available — it
    returns both roots (and nothing below them) in one hop — and the roots are
    compared by ID so the item being repaired can never be mistaken for its own
    collision.

    Fails CLOSED: if the roots cannot be listed, the repair is refused rather
    than issued on an unverified assumption.
    """
    target = new_name.strip().lower()
    own_id = str(item.get("id") or "").strip().lower()
    try:
        response = await request_with_retry(
            app_ctx.http_client,
            "GET",
            _queries_url(organization, project),
            headers=await build_headers(app_ctx),
            params={"api-version": _QUERIES_API_VERSION},
        )
        response.raise_for_status()
        roots = _extract_items(response.json())
    except Exception:
        logger.warning("Could not list the query roots to check a root repair", exc_info=True)
        return (
            f"Refusing to rename this ROOT folder to '{new_name}': the query roots "
            "could not be listed, so whether that name is already taken by the other "
            "root is unknown, and renaming a root onto a name that is in use puts two "
            "roots on one path. Check with devops_list_queries and try again."
        )
    for root in roots:
        if not _is_root_folder(root):
            continue
        root_id = str(root.get("id") or "").strip().lower()
        if own_id and root_id == own_id:
            continue
        if _root_label(root).lower() == target:
            return (
                f"Refusing to rename this ROOT folder to '{new_name}': that name is "
                "already in use by the other root of the query hierarchy, and two roots "
                "sharing one path is worse than the damage being repaired — neither "
                "would cleanly own it. A renamed root can only be restored to the root "
                "name that is currently VACANT. Check which one that is with "
                "devops_list_queries, then repair this root to that name."
            )
    return None


_PERMISSIONS_NOT_RESTORED = (
    "Permission changes applied to this item (and to its descendants) before it was "
    "deleted are NOT restored — that loss is permanent."
)

_RESTORE_DESCENDANTS_HINT = (
    "A soft-deleted item is only addressable by GUID; find the GUIDs with "
    "devops_list_queries(include_deleted=True) and restore each one with "
    "devops_update_query(query=<guid>, undelete=True)."
)


def _undelete_report(was_deleted: bool | None, descendants_requested: bool) -> dict:
    """Say what the undelete actually did, not what was asked for.

    The service answers 200 either way, so the honest signal is the state read
    *before* the PATCH.  Three outcomes, and the middle one is the whole point:
    an undelete aimed at an already-live item restores nothing and — critically —
    does not cascade, leaving the descendants the caller believes they just
    recovered still deleted.

    ``undelete_state`` is the authoritative field; ``undeleted`` is deliberately
    the coarser one.  It is True for ``restored`` and — the decision worth
    stating — for ``unverified`` too, where it means "the PATCH was issued and
    accepted", matching the ``renamed``/``wiql_updated``/``moved`` flags beside
    it, which also report what was *done*, not what changed.  ``already_live`` is
    the one case that reports False, because there the service told us plainly
    that nothing was restored.  A caller that must not guess reads
    ``undelete_state``.
    """
    if was_deleted is True:
        note = f"Restored from soft delete. {_PERMISSIONS_NOT_RESTORED}"
        if descendants_requested:
            note += (
                " Its descendants were restored too: the cascade ran because the item "
                "itself transitioned from deleted to live."
            )
        else:
            note += (
                " If this is a folder, the queries inside it are STILL deleted, and "
                "re-running with undelete_descendants=True will not fix that: the "
                "cascade only fires on the call that restores the folder itself, which "
                "has now already happened. " + _RESTORE_DESCENDANTS_HINT
            )
        return {"undeleted": True, "undelete_state": "restored", "note": note}

    if was_deleted is False:
        note = (
            "Nothing was restored: this item was not deleted. Azure DevOps accepts the "
            "request anyway and answers 200."
        )
        if descendants_requested:
            note += (
                " undelete_descendants had NO effect either — it only cascades at the "
                "moment the item itself goes from deleted to live, so any descendants "
                "that are still deleted were left deleted. " + _RESTORE_DESCENDANTS_HINT
            )
        return {"undeleted": False, "undelete_state": "already_live", "note": note}

    return {
        "undeleted": True,
        "undelete_state": "unverified",
        "note": (
            "The undelete was accepted, but this item's state could not be read "
            "beforehand, so whether it was actually deleted is unknown. If it was "
            "already live nothing changed, and undelete_descendants did not cascade. "
            f"Confirm with devops_get_query. {_PERMISSIONS_NOT_RESTORED}"
        ),
    }


async def _hydrate_work_items(
    app_ctx: AppContext,
    organization: str,
    project: str,
    ids: list[int],
    fields: list[str],
) -> list[dict]:
    """Fetch field values for *ids* via workitemsbatch, chunked at the 200 cap.

    ``errorPolicy: omit`` drops ids the caller cannot read (a shared query can
    legitimately return items in an area the caller has no access to) instead of
    failing the whole batch.
    """
    url = build_url(organization, project, "wit/workitemsbatch")
    headers = await build_headers(app_ctx, include_content_type=True)
    collected: list[dict] = []
    for start in range(0, len(ids), _BATCH_CHUNK_SIZE):
        chunk = ids[start : start + _BATCH_CHUNK_SIZE]
        response = await request_with_retry(
            app_ctx.http_client,
            "POST",
            url,
            headers=headers,
            params={"api-version": _WORKITEMS_BATCH_API_VERSION},
            json={"ids": chunk, "fields": fields, "errorPolicy": "omit"},
        )
        response.raise_for_status()
        collected.extend(response.json().get("value", []))
    return collected


# idempotentHint is True to match every other readOnlyHint tool in this repo.
# The one caveat, recorded here rather than in the annotation: running a saved
# query does touch server state — it advances the query's lastExecutedDate — so
# two identical runs are not byte-identical no-ops. That is bookkeeping, not a
# mutation of anything a caller addresses, and the MCP spec treats
# idempotentHint as meaningful only when readOnlyHint is false.
@mcp.tool(
    name="devops_run_query",
    annotations={
        "title": "Run Query",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def devops_run_query(params: RunQueryInput, ctx: Context) -> str:
    """Execute a saved Azure DevOps query and fetch the resulting work items.

    Accepts a query GUID or a path ('Shared Queries/Team/All Bugs'); a path costs
    one extra call to resolve the id. A folder cannot be run: addressed by path
    it is caught on that resolve call, so no run request is ever issued;
    addressed by GUID there is no resolve call to catch it on, so the service's
    'Querying folders is not supported' 400 is mapped instead. Either way you
    never get a generic failure.

    WIQL returns only id stubs, so field values are fetched in a second batched
    step (200 per call) unless fetch_details=False. By default the fields fetched
    are the query's own selected columns — what the query author declared as
    mattering.

    Result shape depends on the query type the server derived from the WIQL:
    - 'flat' (FROM WorkItems) -> 'work_items' only.
    - any link query (FROM WorkItemLinks) -> 'tree' or 'oneHop', which also
      return 'relations', where each entry has a target_id and (except for root
      rows) a rel and source_id. Which of the two you get depends on the MODE
      clause, not on you — MODE (MustContain) comes back as 'oneHop' — so treat
      both identically. The same work item can appear in several relations;
      'work_items' is de-duplicated.

    Pass 'team' for queries using @CurrentIteration or @TeamAreas — without it
    the project's default team is used, which can silently mean the wrong sprint.
    A team name that does not exist comes back as an HTTP 500, not a 404.
    'possibly_truncated' true means the result hit the 20,000-item cap.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    project = params.project or ""
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        ref = _validate_query_ref(params.query, "query")

        query_id, resolved_item = await _resolve_query_id(
            app_ctx, organization, project, ref, must_be_runnable=True
        )

        # build_url() encodes the project as a single segment, so a team segment
        # cannot be smuggled through it — the URL is assembled here instead, with
        # each segment encoded independently and the team omitted entirely (no
        # empty '//' segment) when it is not supplied.
        if params.team:
            wiql_url = (
                f"https://dev.azure.com/{quote(organization, safe='')}"
                f"/{quote(project, safe='')}/{quote(params.team, safe='')}"
                f"/_apis/wit/wiql/{query_id}"
            )
        else:
            wiql_url = build_url(organization, project, f"wit/wiql/{query_id}")

        run_params: dict = {"api-version": _WIQL_API_VERSION}
        if params.top is not None:
            run_params["$top"] = params.top
        if params.time_precision:
            # No '$' on this one, unlike every other parameter in this module.
            run_params["timePrecision"] = "true"

        run_response = await request_with_retry(
            app_ctx.http_client,
            "GET",
            wiql_url,
            headers=await build_headers(app_ctx),
            params=run_params,
        )
        run_response.raise_for_status()
        run_result = run_response.json()

        ids, relations = _extract_run_ids(run_result)
        columns = [
            c.get("referenceName")
            for c in run_result.get("columns") or []
            if isinstance(c, dict) and c.get("referenceName")
        ]

        result: dict = {
            "query_id": query_id,
            "query_type": run_result.get("queryType"),
            "result_type": run_result.get("queryResultType"),
            "as_of": run_result.get("asOf"),
            "count": len(ids),
            "possibly_truncated": _run_row_count(run_result) >= _QUERY_RESULT_LIMIT,
            "columns": columns,
        }
        if resolved_item.get("path"):
            result["query_path"] = resolved_item["path"]
        if relations:
            result["relations"] = relations

        if not ids:
            result["work_items"] = []
            return finalize_response(result)

        if not params.fetch_details:
            result["work_item_ids"] = ids
            return finalize_response(result)

        fields = params.fields or columns or _DEFAULT_HYDRATION_FIELDS
        result["work_items"] = await _hydrate_work_items(
            app_ctx, organization, project, ids, fields
        )
        return finalize_response(result)

    except ValueError as e:
        return finalize_response({"error": True, "message": str(e)})
    except httpx.HTTPStatusError as e:
        msg = extract_error_message(e.response)
        logger.error("Azure DevOps HTTP %d: %s", e.response.status_code, msg)
        return finalize_response({
            "error": True,
            "message": _query_error_message(
                e.response.status_code,
                msg,
                subject=f"Query '{params.query}'",
                project=project,
                team=params.team,
            ),
        })
    except Exception as e:
        logger.exception("Unexpected error in devops_run_query")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


# ---------------------------------------------------------------------------
# Tools — write
# ---------------------------------------------------------------------------


@write_tool(
    name="devops_create_query",
    annotations={
        "title": "Create Query",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def devops_create_query(params: CreateQueryInput, ctx: Context) -> str:
    """Save a new WIQL query under a folder in the query hierarchy.

    Only 'name' and 'wiql' are sent — queryType, columns and sortColumns are
    derived server-side from the SELECT/ORDER BY, and isPublic is derived from
    the parent folder ('Shared Queries' => public). The default parent is
    'Shared Queries' so the query is visible to the team; 'My Queries' is the
    private folder of whatever identity this server authenticates as.

    Set validate_only=True to parse and validate the WIQL without creating
    anything — a cheap way to check a query before persisting it.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    project = params.project or ""
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        parent = _validate_query_ref(params.parent, "parent")

        create_params: dict = {"api-version": _QUERIES_API_VERSION}
        if params.validate_only:
            # Note: no '$' prefix on this one — with a '$' it is silently ignored
            # and the query gets created for real.
            create_params["validateWiqlOnly"] = "true"

        response = await request_with_retry(
            app_ctx.http_client,
            "POST",
            _queries_url(organization, project, parent),
            headers=await build_headers(app_ctx, include_content_type=True),
            params=create_params,
            json={"name": params.name, "wiql": params.wiql},
        )
        response.raise_for_status()

        result = _project_query_item(_parse_optional_json(response))
        result["validated_only"] = params.validate_only
        if params.validate_only:
            result["message"] = (
                f"The WIQL is valid. Nothing was created — re-run without "
                f"validate_only to save '{params.name}' under '{params.parent}'."
            )
        return finalize_response(result)

    except ValueError as e:
        return finalize_response({"error": True, "message": str(e)})
    except httpx.HTTPStatusError as e:
        msg = extract_error_message(e.response)
        logger.error("Azure DevOps HTTP %d: %s", e.response.status_code, msg)
        return finalize_response({
            "error": True,
            "message": _query_error_message(
                e.response.status_code,
                msg,
                subject=f"Query '{params.name}' under '{params.parent}'",
                project=project,
            ),
        })
    except Exception as e:
        logger.exception("Unexpected error in devops_create_query")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


@write_tool(
    name="devops_create_query_folder",
    annotations={
        "title": "Create Query Folder",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def devops_create_query_folder(params: CreateQueryFolderInput, ctx: Context) -> str:
    """Create a folder in the saved-query hierarchy.

    A folder and a query are created through the same endpoint but have disjoint
    bodies — a folder must not carry WIQL — which is why this is a separate tool
    from devops_create_query. The default parent is 'Shared Queries'.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    project = params.project or ""
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        parent = _validate_query_ref(params.parent, "parent")

        response = await request_with_retry(
            app_ctx.http_client,
            "POST",
            _queries_url(organization, project, parent),
            headers=await build_headers(app_ctx, include_content_type=True),
            params={"api-version": _QUERIES_API_VERSION},
            json={"name": params.name, "isFolder": True},
        )
        response.raise_for_status()
        return finalize_response(_project_query_item(_parse_optional_json(response)))

    except ValueError as e:
        return finalize_response({"error": True, "message": str(e)})
    except httpx.HTTPStatusError as e:
        msg = extract_error_message(e.response)
        logger.error("Azure DevOps HTTP %d: %s", e.response.status_code, msg)
        return finalize_response({
            "error": True,
            "message": _query_error_message(
                e.response.status_code,
                msg,
                subject=f"Folder '{params.name}' under '{params.parent}'",
                project=project,
            ),
        })
    except Exception as e:
        logger.exception("Unexpected error in devops_create_query_folder")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


@write_tool(
    name="devops_update_query",
    annotations={
        "title": "Update Query",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def devops_update_query(params: UpdateQueryInput, ctx: Context) -> str:
    """Rename, re-WIQL, move, or undelete a saved query or folder.

    Only the keys you supply are sent. There is NO optimistic concurrency on this
    resource (no revision, no ETag) — last writer wins, so a concurrent edit by
    someone else is silently overwritten.

    Moving is a different operation from the rest: it posts the item into the
    destination folder. When combined with a rename or a WIQL edit, the edit is
    applied first so the returned 'path' reflects the final location.

    The two ROOT folders cannot be renamed or moved here. Azure DevOps allows a
    root rename (HTTP 200, no warning) and it breaks PATH addressing for every
    query beneath it — paths must start with 'Shared Queries' or 'My Queries', so
    afterwards only GUIDs resolve. An INTACT root is refused every rename,
    including a rename to the other root's name (that is a swap, not a repair,
    and leaves two roots on one path). The one allowed rename is the repair of a
    root that has ALREADY been renamed: addressed by its GUID, back to whichever
    of 'Shared Queries' / 'My Queries' is currently vacant. A GUID says nothing
    about what it points at, so a rename or move addressed by GUID costs one
    extra read (a repair costs a second, to confirm the target name is free); a
    path-addressed one costs nothing.

    Undelete restores a soft-deleted item's content but NOT the permission
    changes that were on it. It only works when 'query' is a GUID — a
    soft-deleted item's path does not resolve; get the GUID from
    devops_list_queries(include_deleted=True).

    undelete_descendants only takes effect on the deleted -> live TRANSITION.
    Re-running it against an item that is already live is accepted and does
    nothing, so this tool reads the item's state first and reports
    'undelete_state' as 'restored', 'already_live' or 'unverified' rather than
    claiming a restore that did not happen.

    Editing WIQL re-derives query_type server-side, which can change what
    devops_run_query returns.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    project = params.project or ""
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        ref = _validate_query_ref(params.query, "query")
        destination = _validate_query_ref(params.move_to, "move_to") if params.move_to else None

        # Renaming or moving is what changes an item's ADDRESS, and doing either
        # to a root folder strands everything beneath it.  A path ref settles it
        # for free (only a root has a single-segment path); a GUID hides what it
        # points at, so that case has to be looked up.
        #
        # A single-segment PATH ref is provably a HEALTHY root and needs no read
        # at all: _validate_query_ref has already required the first segment to
        # be 'Shared Queries'/'My Queries', and a DAMAGED root answers to neither
        # of those paths (that is what being damaged means), so this ref either
        # addresses an intact root or 404s.  No rename or move of it is ever
        # permitted, so the healthy path never pays for a lookup it cannot need.
        changes_address = params.name is not None or destination is not None
        if changes_address and _is_root_path(ref):
            refusal = _root_guard_error(
                {"isFolder": True, "path": ref},
                new_name=params.name,
                moving=destination is not None,
            )
            if refusal:
                raise ValueError(refusal)

        # One pre-PATCH read serves both pre-checks: the root guard above (for a
        # GUID ref) and the undelete state below, which is what lets the report
        # distinguish a real restore from a no-op against an already-live item.
        needs_root_check = changes_address and _is_guid(ref)
        current: dict | None = None
        if needs_root_check or params.undelete:
            current = await _read_current_item(
                app_ctx, organization, project, ref, required=needs_root_check
            )
        if needs_root_check:
            refusal = _root_guard_error(
                current or {}, new_name=params.name, moving=destination is not None
            )
            if refusal:
                raise ValueError(refusal)
            # The guard permits exactly one root rename — repairing a DAMAGED
            # root back to a root name — and only that case buys a second read,
            # to confirm the name it is being restored to is not still held by
            # the other root.
            if params.name is not None and _is_damaged_root(current or {}):
                conflict = await _root_repair_conflict(
                    app_ctx, organization, project, current or {}, params.name
                )
                if conflict:
                    raise ValueError(conflict)

        item: dict = {}
        patch_body: dict = {}
        if params.name is not None:
            patch_body["name"] = params.name
        if params.wiql is not None:
            patch_body["wiql"] = params.wiql
        if params.undelete:
            patch_body["isDeleted"] = False

        was_deleted = (
            bool(current.get("isDeleted", False))
            if params.undelete and current is not None
            else None
        )

        if patch_body:
            patch_params: dict = {"api-version": _QUERIES_API_VERSION}
            if params.undelete_descendants:
                patch_params["$undeleteDescendants"] = "true"

            # Merge-patch: ordinary application/json with a partial
            # QueryHierarchyItem. NOT application/json-patch+json — that is the
            # wit/workitems contract and it is rejected here.
            patch_response = await request_with_retry(
                app_ctx.http_client,
                "PATCH",
                _queries_url(organization, project, ref),
                headers=await build_headers(app_ctx, include_content_type=True),
                params=patch_params,
                json=patch_body,
            )
            patch_response.raise_for_status()
            item = _parse_optional_json(patch_response)

        if destination:
            # Move is a POST to Create against the DESTINATION parent, with a body
            # of just the item's id — there is no documented way to move by
            # PATCHing 'path'. Create answers 201, this answers 200; neither
            # status is branched on.
            move_id = item.get("id")
            if not move_id:
                # DEAD CODE against Azure DevOps Services as of 2026-08: probed
                # live, a rename PATCH always answers 200 with the full item,
                # including 'id' and the post-rename 'path', so this never fires.
                # Kept because the id is mandatory two lines down and a bodiless
                # PATCH is permitted by the contract (delete already answers a
                # bodiless 204 where the reference documents a body) — but it is
                # unreachable on the real service and therefore unverified there.
                # Looking the item up by the ORIGINAL 'ref' would 404 when the
                # PATCH just renamed it, so resolve by the post-rename reference
                # (a rename only replaces the path's leaf; a GUID is unaffected).
                move_id = (
                    await _resolve_query_id(
                        app_ctx, organization, project, _renamed_ref(ref, params.name)
                    )
                )[0]
            if not _is_guid(str(move_id)):
                raise ValueError(
                    f"Cannot move '{params.query}': Azure DevOps reported its id as "
                    f"{move_id!r}, which is not a GUID. Re-read the item with "
                    "devops_get_query and move it by GUID."
                )
            move_response = await request_with_retry(
                app_ctx.http_client,
                "POST",
                _queries_url(organization, project, destination),
                headers=await build_headers(app_ctx, include_content_type=True),
                params={"api-version": _QUERIES_API_VERSION},
                json={"id": move_id},
            )
            move_response.raise_for_status()
            moved_item = _parse_optional_json(move_response)
            if moved_item:
                item = moved_item

        result = _project_query_item(item)
        result["renamed"] = params.name is not None
        result["wiql_updated"] = params.wiql is not None
        result["moved"] = destination is not None
        if params.undelete:
            result.update(_undelete_report(was_deleted, params.undelete_descendants))
        else:
            result["undeleted"] = False
        return finalize_response(result)

    except ValueError as e:
        return finalize_response({"error": True, "message": str(e)})
    except httpx.HTTPStatusError as e:
        msg = extract_error_message(e.response)
        logger.error("Azure DevOps HTTP %d: %s", e.response.status_code, msg)
        return finalize_response({
            "error": True,
            "message": _query_error_message(
                e.response.status_code, msg, subject=f"Query '{params.query}'", project=project
            ),
        })
    except Exception as e:
        logger.exception("Unexpected error in devops_update_query")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})


@delete_tool(
    name="devops_delete_query",
    annotations={
        "title": "Delete Query",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def devops_delete_query(params: DeleteQueryInput, ctx: Context) -> str:
    """Delete a saved query or folder. Deleting a FOLDER cascades to its contents.

    The delete is SOFT and there is no 'destroy' option: the item stays
    discoverable with include_deleted=True and is restorable with
    devops_update_query(undelete=True) (add undelete_descendants=True for a
    folder). But deleting it costs you its PATH as an address — a soft-deleted
    item resolves only by GUID, so this tool returns 'query_id' when the service
    gives one, and otherwise the GUID has to be read back from
    devops_list_queries(include_deleted=True) on the parent folder.

    What is NOT restorable is the item's permissions — any permission change
    applied to the query or folder, and to every descendant of a deleted folder,
    is lost permanently even after a successful undelete.

    Deleting a folder deletes every query inside it. Check with devops_get_query
    (depth=1) first if you are not sure what a folder holds.

    A ROOT folder ('Shared Queries', 'My Queries') cannot be deleted at all —
    verified live, Azure DevOps refuses it with 'TF237023: You cannot delete root
    folders.' at HTTP 400. That is why this tool carries no client-side root
    guard: the service enforces it, and the error is mapped to an actionable
    message. Note the asymmetry with RENAME, which is NOT refused — renaming a
    root answers 200 and bricks path addressing for the whole hierarchy beneath
    it, so devops_update_query has to guard that itself. To clear out a root,
    delete the folders and queries INSIDE it individually.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    project = params.project or ""
    try:
        organization = resolve_org(app_ctx, params.organization)
        project = resolve_project(app_ctx, params.project)
        ref = _validate_query_ref(params.query, "query")

        response = await request_with_retry(
            app_ctx.http_client,
            "DELETE",
            _queries_url(organization, project, ref),
            headers=await build_headers(app_ctx),
            params={"api-version": _QUERIES_API_VERSION},
        )
        response.raise_for_status()

        # Documented as 200 with a body; answers a bodiless 204 in practice
        # (verified live) — treat any 2xx as success and only read JSON when
        # there is content.
        body = _parse_optional_json(response)

        result: dict = {
            "query": params.query,
            "project": project,
            "deleted": True,
            "recoverable": True,
            "note": (
                "This was a soft delete. Restore it with "
                "devops_update_query(query=<guid>, undelete=True) — add "
                "undelete_descendants=True on that same call for a folder, because the "
                "cascade only runs on the deleted -> live transition. Restore BY GUID: "
                "the path no longer resolves now that the item is deleted, and "
                "include_deleted does not change that. Permission changes on the item "
                "and its descendants are NOT restored. If this was a folder, every "
                "query inside it was deleted too."
            ),
        }
        # The GUID is the only remaining handle on a soft-deleted item, so echo
        # it whenever it is known — the 204 carries no body at all.
        if body.get("id"):
            result["query_id"] = body["id"]
        elif _is_guid(ref):
            result["query_id"] = str(uuid.UUID(ref))
        if body.get("path"):
            result["path"] = body["path"]
        return finalize_response(result)

    except ValueError as e:
        return finalize_response({"error": True, "message": str(e)})
    except httpx.HTTPStatusError as e:
        msg = extract_error_message(e.response)
        logger.error("Azure DevOps HTTP %d: %s", e.response.status_code, msg)
        return finalize_response({
            "error": True,
            "message": _query_error_message(
                e.response.status_code, msg, subject=f"Query '{params.query}'", project=project
            ),
        })
    except Exception as e:
        logger.exception("Unexpected error in devops_delete_query")
        return finalize_response({"error": True, "message": f"Unexpected error: {type(e).__name__}: {e}"})

"""HTML routes.

Server-rendered with Jinja2, progressively enhanced with htmx: the calls table
is a normal GET that also works without JavaScript, and htmx swaps just the
table body when filters change.  There is no build step and no client-side
state to keep in sync with the server.

Filters live in the query string so a filtered view is a URL an operator can
bookmark or paste to a colleague -- which is most of what a CDR search is for.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote_plus, urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from c2w.api.deps import (
    BRAND_COOKIE,
    SESSION_COOKIE,
    CurrentUser,
    ScopedSession,
    active_brand_id,
    client_ip,
    current_user,
    effective_role,
    get_session,
    optional_user,
    selectable_brands,
)
from c2w.api.v1.cdrs import (
    CdrQuery,
    SortField,
    count_cdrs,
    dashboard_stats,
    get_call,
    get_recording,
    search_cdrs,
)
from c2w.auth.local import AuthError, authenticate, create_session, revoke_session
from c2w.auth.rbac import Permission, permissions_for
from c2w.crypto import CryptoError
from c2w.db.models.auth import AuthSource, User
from c2w.db.models.core import Brand, CommPeakConnection, StorageDestination, Tenant
from c2w.logging import get_logger
from c2w.settings import SettingsError, settings_service
from c2w.settings_spec import SETTINGS, SettingType, specs_by_category
from c2w.storage.commpeak import COMMPEAK_ENDPOINT
from c2w.storage.errors import ErrorClass, TransferError
from c2w.sync import queue
from c2w.web.accounts import (
    AccountError,
    add_connection,
    add_destination,
    delete_connection,
    delete_destination,
    test_connection,
    test_destination,
    update_connection,
    update_destination,
    wasabi_region_choices,
)
from c2w.web.filters import register as register_filters

log = get_logger(__name__)

#: The result of the last Test, handed to the page after the redirect.
#:
#: In memory and per process, which is right for what it is: a probe result is
#: interesting for one page view and worthless afterwards. Putting it in the
#: database would mean writing a row on every button press and cleaning them up
#: later; putting it in the session cookie would mean sending a report of
#: someone's credentials back through a browser.
_PROBE_RESULTS: dict[tuple[str, int | None], Any] = {}

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
register_filters(templates.env)

router = APIRouter(include_in_schema=False)


#: Falls back to the registry default rather than UTC, so the clock and the
#: schedules agree with each other before anyone has chosen a zone.
async def _shell(
    request: Request, session: AsyncSession, user: User, nav: str, **extra: Any
) -> dict[str, Any]:
    """Context every page needs.

    Permissions are for the organisation being looked at, not the one on the
    account: the same person can be an admin for one of these companies and an
    operator for the other.
    """
    brands = await selectable_brands(session, user)
    brand_cookie = request.cookies.get(BRAND_COOKIE)
    active: Brand | None = None
    if brand_cookie and brand_cookie.isdigit():
        active = next((b for b in brands if b.id == int(brand_cookie)), None)
    if active is None:
        active = next((b for b in brands if b.id == user.brand_id), None) or (
            brands[0] if brands else None
        )

    role_here = await effective_role(session, user, active.id if active else None)
    timezone = await settings_service.get_str(
        session, "org.timezone", brand_id=active.id if active else None
    )
    return {
        "request": request,
        "user": user,
        "brands": brands,
        "active_brand": active,
        "nav": nav,
        "role_here": role_here,
        "org_timezone": timezone,
        "prefs": user.preferences or {},
        "permissions": {str(p) for p in permissions_for(role_here)},
        **extra,
    }


# ------------------------------------------------------------------ auth pages


@router.get("/login", response_class=HTMLResponse)
async def login_form(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User | None, Depends(optional_user)],
) -> Response:
    if user is not None:
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    no_users = (await session.execute(select(func.count()).select_from(User))).scalar_one() == 0
    sso = await settings_service.get_bool(session, "auth.oidc_entra_enabled")
    return templates.TemplateResponse(
        request, "login.html", {"request": request, "no_users": no_users, "sso_enabled": sso}
    )


@router.post("/login")
async def login_submit(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
) -> Response:
    try:
        user = await authenticate(session, email, password)
    except AuthError as exc:
        log.info("login.failed", email=email[:64], reason=str(exc))
        return templates.TemplateResponse(
            request,
            "login.html",
            {"request": request, "error": str(exc)},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    token, _ = await create_session(
        session, user, ip=client_ip(request), user_agent=request.headers.get("user-agent")
    )
    response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    # httponly so JavaScript cannot read it; samesite=lax so it survives a
    # normal navigation but not a cross-site POST.
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        max_age=await settings_service.get_int(session, "core.session_ttl_seconds"),
    )
    # A super admin has no brand of their own; start them on the first one so
    # the UI has a scope to render, and let them switch from the sidebar.
    initial_brand = user.brand_id
    if initial_brand is None:
        brands = await selectable_brands(session, user)
        initial_brand = brands[0].id if brands else None
    if initial_brand:
        response.set_cookie(BRAND_COOKIE, str(initial_brand), samesite="lax")
    log.info("login.ok", user_id=user.id, role=str(user.role))
    return response


@router.get("/logout")
async def logout(
    request: Request, session: Annotated[AsyncSession, Depends(get_session)]
) -> Response:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        await revoke_session(session, token)
    response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(SESSION_COOKIE)
    return response


@router.post("/switch-brand")
async def switch_brand(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: CurrentUser,
    brand_id: Annotated[int, Form()],
) -> Response:
    """Change the active organisation.

    Validated against the brands this user may see, so the cookie cannot be
    edited to reach another company's data.
    """
    allowed = {b.id for b in await selectable_brands(session, user)}
    if brand_id not in allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not your organisation")
    response = RedirectResponse(
        request.headers.get("referer") or "/", status_code=status.HTTP_303_SEE_OTHER
    )
    response.set_cookie(BRAND_COOKIE, str(brand_id), samesite="lax")
    return response


# ------------------------------------------------------------------- dashboard


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    user: CurrentUser,
    session: ScopedSession,
) -> Response:
    stats = await dashboard_stats(session)
    destinations = (await session.execute(select(StorageDestination))).scalars().all()
    transfers_enabled = await settings_service.get_bool(session, "transfer.enabled")
    order = [
        "AVAILABLE",
        "VERIFIED",
        "UPLOADED",
        "TRANSFERRING",
        "QUEUED",
        "DISCOVERED",
        "FAILED",
        "MISSING_SOURCE",
        "SOURCE_DELETED",
    ]
    known = stats["recording_states"]
    state_rows = [(name, known[name]) for name in order if name in known]
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        await _shell(
            request,
            session,
            user,
            "dashboard",
            stats=stats,
            state_rows=state_rows,
            has_destination=bool(destinations),
            transfers_enabled=transfers_enabled,
        ),
    )


# ----------------------------------------------------------------------- calls


def _parse_query(
    number: str | None,
    date_from: str | None,
    date_to: str | None,
    direction: str | None,
    agent: str | None,
    media: str | None,
    connection_id: int | None,
    status_filter: str | None,
    min_duration: int | None,
    sort: str,
    desc: str,
    limit: int,
    offset: int,
) -> CdrQuery:
    def _dt(value: str | None) -> datetime | None:
        if not value:
            return None
        for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(value, fmt).replace(tzinfo=UTC)
            except ValueError:
                continue
        return None

    try:
        sort_field = SortField(sort)
    except ValueError:
        sort_field = SortField.STARTED

    return CdrQuery(
        date_from=_dt(date_from),
        date_to=_dt(date_to),
        number=number or None,
        direction=direction or None,
        agent=agent or None,
        media=media or None,
        connection_id=connection_id,
        status=status_filter or None,
        min_duration=min_duration or None,
        sort=sort_field,
        descending=desc in ("1", "true", ""),
        limit=limit,
        offset=offset,
    )


_QueryParams = dict[str, Any]


async def _calls_context(
    request: Request, session: AsyncSession, query: CdrQuery
) -> _QueryParams:
    page = await search_cdrs(session, query)
    total = await count_cdrs(session, query)
    any_cdrs = (await session.execute(text("SELECT EXISTS (SELECT 1 FROM cdrs)"))).scalar_one()
    connections = (
        (await session.execute(select(CommPeakConnection).order_by(CommPeakConnection.name)))
        .scalars()
        .all()
    )
    count_label = f"{total:,}+ calls" if total >= 10_000 else f"{total:,} calls"
    # The chip row: which slice of the list you are looking at. Server-rendered
    # links rather than client-side filtering, because at these row counts the
    # server has to do the paging anyway.
    media_chips = (
        ("", "All"),
        ("available", "Playable"),
        ("pending", "Copying"),
        ("failed", "Failed"),
        ("none", "No recording"),
    )
    return {
        "page": page,
        "q": query,
        "connections": connections,
        "any_cdrs": any_cdrs,
        "count_label": count_label,
        "media_chips": media_chips,
        "query_string": urlencode(
            {k: v for k, v in request.query_params.items() if v}, doseq=True
        ),
    }


@router.get("/calls", response_class=HTMLResponse)
async def calls(
    request: Request,
    user: CurrentUser,
    session: ScopedSession,
    number: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    direction: str | None = None,
    agent: str | None = None,
    media: str | None = None,
    connection_id: int | None = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    min_duration: int | None = None,
    sort: str = "started",
    desc: str = "1",
    limit: int = 50,
    offset: int = 0,
) -> Response:
    query = _parse_query(
        number, date_from, date_to, direction, agent, media, connection_id,
        status_filter, min_duration, sort, desc, limit, offset,
    )
    context = await _calls_context(request, session, query)
    return templates.TemplateResponse(
        request, "calls.html", await _shell(request, session, user, "calls", **context)
    )


@router.get("/calls/rows", response_class=HTMLResponse)
async def calls_rows(
    request: Request,
    user: CurrentUser,
    session: ScopedSession,
    number: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    direction: str | None = None,
    agent: str | None = None,
    media: str | None = None,
    connection_id: int | None = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    min_duration: int | None = None,
    sort: str = "started",
    desc: str = "1",
    limit: int = 50,
    offset: int = 0,
) -> Response:
    """The table fragment htmx swaps in."""
    query = _parse_query(
        number, date_from, date_to, direction, agent, media, connection_id,
        status_filter, min_duration, sort, desc, limit, offset,
    )
    context = await _calls_context(request, session, query)
    return templates.TemplateResponse(
        request,
        "_calls_rows.html",
        {
            **context,
            "request": request,
            "permissions": {str(p) for p in permissions_for(user.role)},
        },
    )


@router.get("/calls/export.csv")
async def export_calls(
    request: Request,
    user: CurrentUser,
    session: ScopedSession,
    number: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    direction: str | None = None,
    agent: str | None = None,
    media: str | None = None,
    connection_id: int | None = None,
    min_duration: int | None = None,
) -> Response:
    """Stream a CSV of the current filter.

    Streamed rather than assembled in memory: an export can legitimately cover
    a month of calls, and buffering that would put an unbounded allocation in
    the request path.
    """
    if Permission.CDR_EXPORT not in permissions_for(user.role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "your role may not export CDRs")

    query = _parse_query(
        number, date_from, date_to, direction, agent, media, connection_id,
        None, min_duration, "started", "1", 200, 0,
    )

    async def rows():
        import csv
        import io

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            [
                "started_at", "ended_at", "direction", "from", "to", "agent",
                "duration_seconds", "status", "call_uuid", "recording_state",
                "recording_parts",
            ]
        )
        yield buffer.getvalue()
        buffer.seek(0), buffer.truncate(0)

        offset = 0
        while True:
            query.offset = offset
            page = await search_cdrs(session, query)
            if not page.rows:
                return
            for row in page.rows:
                writer.writerow(
                    [
                        row["start_at"].isoformat() if row["start_at"] else "",
                        row["end_at"].isoformat() if row["end_at"] else "",
                        row["direction"] or "",
                        row["src"] or "",
                        row["dst"] or "",
                        row["agent_name"] or row["agent_extension"] or "",
                        row["call_duration"] or "",
                        row["status"] or "",
                        row["call_uuid"] or "",
                        ",".join(row["recording_states"] or []),
                        row["recording_parts"] or 0,
                    ]
                )
            yield buffer.getvalue()
            buffer.seek(0), buffer.truncate(0)
            if not page.has_more:
                return
            offset += page.limit

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return StreamingResponse(
        rows(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="calls-{stamp}.csv"'},
    )


@router.get("/calls/{cdr_id}", response_class=HTMLResponse)
async def call_detail(
    request: Request, user: CurrentUser, session: ScopedSession, cdr_id: int
) -> Response:
    call = await get_call(session, cdr_id)
    if call is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "call not found")
    return templates.TemplateResponse(
        request, "call_detail.html", await _shell(request, session, user, "calls", call=call)
    )


# ------------------------------------------------------------------------ sync


async def _sync_context(session: AsyncSession) -> _QueryParams:
    jobs = await queue.queue_depth(session)
    rows = (
        await session.execute(
            text(
                """
                SELECT k.id, k.name, k.last_inventory_at, k.inventory_cursor_hour,
                       count(r.id)                                          AS total,
                       count(r.id) FILTER (
                           WHERE r.state IN ('AVAILABLE','SOURCE_DELETED')
                       )                                                    AS archived,
                       count(r.id) FILTER (
                           WHERE r.state IN ('DISCOVERED','QUEUED','TRANSFERRING','UPLOADED')
                       )                                                    AS pending,
                       count(r.id) FILTER (
                           WHERE r.state IN ('FAILED','MISSING_SOURCE')
                       )                                                    AS failed
                FROM commpeak_connections k
                LEFT JOIN recordings r ON r.connection_id = k.id AND r.brand_id = k.brand_id
                GROUP BY k.id, k.name, k.last_inventory_at, k.inventory_cursor_hour
                ORDER BY k.name
                """
            )
        )
    ).mappings().all()

    causes = (
        await session.execute(
            text(
                "SELECT error_class, count(*) FROM transfer_jobs "
                "WHERE state = 'FAILED' AND error_class IS NOT NULL "
                "GROUP BY error_class ORDER BY 2 DESC"
            )
        )
    ).all()
    hints = []
    for cause, count in causes:
        try:
            hint = ErrorClass(cause).__class__ and _cause_hint(ErrorClass(cause))
        except ValueError:
            hint = ""
        hints.append((cause, count, hint))

    return {"jobs": jobs, "per_connection": [dict(r) for r in rows], "error_classes": hints}


def _cause_hint(cause: ErrorClass) -> str:
    """Operator-facing next step for a failure class."""
    return {
        ErrorClass.ACL_ERROR: "add this server's public IP to the CommPeak account ACL",
        ErrorClass.AUTH_ERROR: "the S3 token or secret is wrong; re-enter it",
        ErrorClass.CONFIG_ERROR: "check the endpoint, region and bucket; also check clock skew",
        ErrorClass.NOT_FOUND: "the object was removed at source before it could be copied",
        ErrorClass.RATE_LIMIT: "lower the per-connection concurrency",
        ErrorClass.NETWORK_ERROR: "transient; check link stability if it persists",
        ErrorClass.CHECKSUM_ERROR: (
            "source and archive bytes disagreed; investigate before requeueing"
        ),
        ErrorClass.STORAGE_ERROR: "the archive rejected the write; check quota and bucket policy",
        ErrorClass.PERMISSION_ERROR: "the credentials lack the required permission",
    }.get(cause, "")


@router.get("/sync", response_class=HTMLResponse)
async def sync_page(request: Request, user: CurrentUser, session: ScopedSession) -> Response:
    context = await _sync_context(session)
    return templates.TemplateResponse(
        request, "sync.html", await _shell(request, session, user, "sync", **context)
    )


@router.get("/sync/panel", response_class=HTMLResponse)
async def sync_panel(request: Request, user: CurrentUser, session: ScopedSession) -> Response:
    """The panel fragment, for a browser without JavaScript."""
    context = await _sync_context(session)
    return templates.TemplateResponse(
        request, "_sync_panel.html", {**context, "request": request}
    )


@router.get("/sync/status.json")
async def sync_status(user: CurrentUser, session: ScopedSession) -> Response:
    """Counters for the kit's poll.js.

    ``active`` is what stops the page polling a system that is doing nothing:
    with no work queued or running there is nothing to watch, and poll.js
    treats the transition to inactive as completion and reloads once -- which
    is the honest way to show a dozen rows that have all changed.
    """
    jobs = await queue.queue_depth(session)
    rows = (
        await session.execute(
            text(
                """
                SELECT k.id, count(r.id) AS total,
                       count(r.id) FILTER (
                           WHERE r.state IN ('AVAILABLE','SOURCE_DELETED')
                       ) AS archived,
                       count(r.id) FILTER (
                           WHERE r.state IN ('DISCOVERED','QUEUED','TRANSFERRING','UPLOADED')
                       ) AS pending,
                       count(r.id) FILTER (
                           WHERE r.state IN ('FAILED','MISSING_SOURCE')
                       ) AS failed
                FROM commpeak_connections k
                LEFT JOIN recordings r ON r.connection_id = k.id AND r.brand_id = k.brand_id
                GROUP BY k.id
                """
            )
        )
    ).mappings().all()

    in_flight = jobs.get("PENDING", 0) + jobs.get("RUNNING", 0)
    return JSONResponse(
        {
            "active": in_flight > 0,
            "jobs": jobs,
            "connections": [dict(r) for r in rows],
        }
    )


# ----------------------------------------------------------------------- admin


@router.get("/admin/connections", response_class=HTMLResponse)
async def admin_connections(
    request: Request,
    user: CurrentUser,
    session: ScopedSession,
    saved: str | None = None,
    error: str | None = None,
    tested: int | None = None,
) -> Response:
    connections = (
        (await session.execute(select(CommPeakConnection).order_by(CommPeakConnection.name)))
        .scalars()
        .all()
    )
    destinations = (
        (await session.execute(select(StorageDestination).order_by(StorageDestination.name)))
        .scalars()
        .all()
    )
    tenants = {
        t.id: t for t in (await session.execute(select(Tenant))).scalars().all()
    }
    counts_rows = (
        await session.execute(
            text(
                "SELECT connection_id, count(*) AS total, "
                "count(*) FILTER (WHERE state IN ('AVAILABLE','SOURCE_DELETED')) AS archived "
                "FROM recordings GROUP BY connection_id"
            )
        )
    ).mappings().all()

    probe = _PROBE_RESULTS.pop(("connection", tested), None) if tested else None

    return templates.TemplateResponse(
        request,
        "connections.html",
        await _shell(
            request, session, user, "connections",
            connections=connections,
            destinations={d.id: d for d in destinations},
            destination_list=destinations,
            tenants=tenants,
            counts={r["connection_id"]: dict(r) for r in counts_rows},
            commpeak_endpoints=(COMMPEAK_ENDPOINT,),
            auth_schemes=("bearer", "header", "basic", "query", "none"),
            probe=probe,
            probe_for=tested,
            saved=saved,
            error=error,
        ),
    )


@router.post("/admin/connections")
async def admin_connections_save(
    request: Request,
    user: CurrentUser,
    session: ScopedSession,
    brand_id: Annotated[int, Depends(active_brand_id)],
) -> Response:
    """Add or change a CommPeak account from the browser."""
    if Permission.STORAGE_MANAGE not in permissions_for(
        await effective_role(session, user, brand_id)
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "your role may not manage accounts")

    form = {k: str(v) for k, v in (await request.form()).items()}
    action = form.get("action") or "add"
    try:
        if action == "add":
            conn = await add_connection(session, brand_id, form, actor=user.email)
            target = f"/admin/connections?saved={quote_plus(conn.name)}"
        elif action == "update":
            conn = await update_connection(
                session, brand_id, int(form["connection_id"]), form, actor=user.email
            )
            target = f"/admin/connections?saved={quote_plus(conn.name)}"
        elif action == "test":
            connection_id = int(form["connection_id"])
            outcome = await test_connection(session, brand_id, connection_id)
            _PROBE_RESULTS[("connection", connection_id)] = outcome
            target = f"/admin/connections?tested={connection_id}"
        elif action == "remove":
            name = await delete_connection(
                session, brand_id, int(form["connection_id"]), actor=user.email
            )
            target = f"/admin/connections?saved={quote_plus(name + ' removed')}"
        else:
            target = "/admin/connections"
    except (AccountError, TransferError, CryptoError) as exc:
        target = f"/admin/connections?error={quote_plus(str(exc))}"
    return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)


@router.get("/admin/storage", response_class=HTMLResponse)
async def admin_storage(
    request: Request,
    user: CurrentUser,
    session: ScopedSession,
    saved: str | None = None,
    error: str | None = None,
    tested: int | None = None,
) -> Response:
    destinations = (
        (await session.execute(select(StorageDestination).order_by(StorageDestination.name)))
        .scalars()
        .all()
    )
    probe = _PROBE_RESULTS.pop(("destination", tested), None) if tested else None
    return templates.TemplateResponse(
        request,
        "storage.html",
        await _shell(
            request, session, user, "storage",
            destinations=destinations,
            providers=("wasabi", "s3", "minio", "backblaze", "other"),
            regions=wasabi_region_choices(),
            probe=probe,
            probe_for=tested,
            saved=saved,
            error=error,
        ),
    )


@router.post("/admin/storage")
async def admin_storage_save(
    request: Request,
    user: CurrentUser,
    session: ScopedSession,
    brand_id: Annotated[int, Depends(active_brand_id)],
) -> Response:
    """Add or change archive storage from the browser."""
    if Permission.STORAGE_MANAGE not in permissions_for(
        await effective_role(session, user, brand_id)
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "your role may not manage storage")

    form = {k: str(v) for k, v in (await request.form()).items()}
    action = form.get("action") or "add"
    try:
        if action == "add":
            dest = await add_destination(session, brand_id, form, actor=user.email)
            target = f"/admin/storage?saved={quote_plus(dest.name)}"
        elif action == "update":
            dest = await update_destination(
                session, brand_id, int(form["destination_id"]), form, actor=user.email
            )
            target = f"/admin/storage?saved={quote_plus(dest.name)}"
        elif action == "test":
            destination_id = int(form["destination_id"])
            outcome = await test_destination(session, brand_id, destination_id)
            _PROBE_RESULTS[("destination", destination_id)] = outcome
            target = f"/admin/storage?tested={destination_id}"
        elif action == "remove":
            name = await delete_destination(
                session, brand_id, int(form["destination_id"]), actor=user.email
            )
            target = f"/admin/storage?saved={quote_plus(name + ' stopped')}"
        else:
            target = "/admin/storage"
    except (AccountError, TransferError, CryptoError) as exc:
        target = f"/admin/storage?error={quote_plus(str(exc))}"
    return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)


#: A one-line orientation for each card, above its fields.
_CATEGORY_NOTES = {
    "Organisation": "Who this organisation is, and where its recordings may live.",
    "CommPeak (source)": "An organisation can have as many CommPeak accounts as "
    "it has PBXes and dialers -- each with its own bucket and its own "
    "credentials, added on the CommPeak page. What follows is shared by all of "
    "them.",
    "Wasabi (archive)": "An organisation can have as many Wasabi accounts and "
    "buckets as it needs; they are added on the Archive page, each with its own "
    "keys. What follows applies to all of them.",
    "Archiving": "How hard to work while copying, and what to do when a copy fails.",
    "Retention": "How long recordings stay where.",
    "Playback and downloads": "How a recording reaches a browser.",
    "Notifications": "Where alerts go. Leave the tokens blank to send none.",
    "Scheduling": "When unattended work happens.",
    "Microsoft 365": "Let staff sign in with their Microsoft work account "
    "instead of a password kept here.",
    "Google Workspace": "Let staff sign in with their Google work account.",
    "Active Directory": "Take the list of people, and who is an administrator, "
    "from a domain controller you run.",
    "Two-factor and passwords": "Applies to accounts kept here. Accounts from "
    "Microsoft, Google or your directory follow that system's rules.",
    "Cloudflare": "Reaching this console from outside, and keeping robots off "
    "the sign-in page.",
    "General": "This installation itself.",
    "Logging and metrics": "What the services write down.",
}

#: Fields whose value is long enough to want two columns.
_WIDE_FIELDS = frozenset(
    {
        "core.base_url",
        "org.data_region_note",
        "commpeak.s3_endpoint",
        "commpeak.cdr_api_base",
        "auth.entra_redirect_note",
        "auth.entra_allowed_domains",
        "auth.google_allowed_domains",
        "ldap.server_uri",
        "ldap.bind_dn",
        "ldap.base_dn",
        "media.ffmpeg_path",
        "transfer.retry_backoff_seconds",
        "tunnel.hostname",
        "turnstile.site_key",
    }
)

#: Switches the software does not let a setting override.
_LOCKED_SETTINGS = frozenset({"source.read_only", "retention.allow_source_deletion"})

#: Where a card's real work is done, when it is not on this page.
_MANAGE_LINKS = {
    "CommPeak (source)": ("/admin/connections", "Manage CommPeak accounts"),
    "Wasabi (archive)": ("/admin/storage", "Manage archive storage"),
    "Two-factor and passwords": ("/admin/users", "Manage people"),
}


async def _setup_steps(session: AsyncSession, brand: Brand | None) -> list[dict[str, Any]]:
    """The checklist between "installed" and "archiving".

    Each step reports what it found rather than just done/not-done, because
    "2 of 2 accounts reachable" answers the next question too.
    """
    users = (await session.execute(text("SELECT count(*) FROM users"))).scalar_one()
    conns = (
        await session.execute(
            text("SELECT count(*), count(*) FILTER (WHERE status = 'OK') FROM commpeak_connections")
        )
    ).one()
    dests = (
        await session.execute(
            text("SELECT count(*), count(*) FILTER (WHERE status = 'OK') FROM storage_destinations")
        )
    ).one()
    recordings = (await session.execute(text("SELECT count(*) FROM recordings"))).scalar_one()
    transfers_on = await settings_service.get_bool(session, "transfer.enabled")
    telegram = await settings_service.get_secret(session, "alerts.telegram_bot_token")
    slack = await settings_service.get_secret(session, "alerts.slack_webhook_url")
    cdr_base = await settings_service.get_str(
        session, "commpeak.cdr_api_base", brand_id=brand.id if brand else None
    )

    steps: list[dict[str, Any]] = [
        {
            "label": "Create this organisation",
            "detail": f"{brand.name}" if brand else "No organisation exists yet.",
            "done": brand is not None,
            "href": None,
            "action": "",
        },
        {
            "label": "Add a CommPeak account",
            "detail": (
                f"{conns[1]} of {conns[0]} reachable"
                if conns[0]
                else "No CommPeak account added yet."
            ),
            "done": conns[0] > 0 and conns[1] == conns[0],
            "href": "/admin/connections",
            "action": "Manage" if conns[0] else "Add",
        },
        {
            "label": "Point it at the call records",
            "detail": (
                "Fetching call details"
                if cdr_base
                else "Recordings will be archived without call details until this is set."
            ),
            "done": bool(cdr_base),
            "href": None,
            "action": "",
        },
        {
            "label": "Add archive storage",
            "detail": (
                f"{dests[1]} of {dests[0]} reachable"
                if dests[0]
                else "Nothing is copied anywhere until storage exists."
            ),
            "done": dests[0] > 0 and dests[1] == dests[0],
            "href": "/admin/storage",
            "action": "Manage" if dests[0] else "Add",
        },
        {
            "label": "Start copying",
            "detail": (
                f"On — {recordings:,} recording(s) known"
                if transfers_on
                else "Recordings are being found but not copied."
            ),
            "done": transfers_on,
            "href": None,
            "action": "",
        },
        {
            "label": "Send alerts somewhere",
            "detail": (
                "Configured"
                if (telegram or slack)
                else "Failures will be visible on the Sync page but nobody will be told."
            ),
            "done": bool(telegram or slack),
            "optional": True,
            "href": None,
            "action": "",
        },
        {
            "label": "Add the people who need access",
            "detail": f"{users} account(s)",
            "done": users > 1,
            "optional": True,
            "href": "/admin/users",
            "action": "Manage",
        },
    ]
    number = 0
    for step in steps:
        step.setdefault("optional", False)
        if not step["done"] and not step["optional"]:
            number += 1
            step["number"] = number
        else:
            step["number"] = ""
    return steps


@router.get("/admin/settings", response_class=HTMLResponse)
async def admin_settings(
    request: Request,
    user: CurrentUser,
    session: ScopedSession,
    brand_id: Annotated[int, Depends(active_brand_id)],
    saved: str | None = None,
    error: str | None = None,
) -> Response:
    if Permission.SETTINGS_VIEW not in permissions_for(user.role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "your role may not view settings")

    values = await settings_service.all_effective(session, brand_id=brand_id)
    brand = (
        await session.execute(select(Brand).where(Brand.id == brand_id))
    ).scalar_one_or_none()

    # The last few characters of a stored secret, so it can be checked against
    # the console it was copied from without being revealed.
    tails: dict[str, str] = {}
    for key, spec in SETTINGS.items():
        if not spec.sensitive:
            continue
        current = await settings_service.get_secret(session, key, brand_id=brand_id)
        if current:
            tails[key] = f"stored, ending …{current[-4:]}"

    counts = (
        await session.execute(
            text(
                "SELECT (SELECT count(*) FROM commpeak_connections) AS commpeak, "
                "       (SELECT count(*) FROM storage_destinations)  AS wasabi"
            )
        )
    ).mappings().one()

    return templates.TemplateResponse(
        request,
        "settings.html",
        await _shell(
            request,
            session,
            user,
            "settings",
            account_counts=dict(counts),
            categories=specs_by_category(),
            values=values,
            secret_tails=tails,
            category_notes=_CATEGORY_NOTES,
            wide_fields=_WIDE_FIELDS,
            locked_settings=_LOCKED_SETTINGS,
            manage_links={k: v[0] for k, v in _MANAGE_LINKS.items()},
            manage_link_labels={k: v[1] for k, v in _MANAGE_LINKS.items()},
            setup_steps=await _setup_steps(session, brand),
            saved=saved,
            error=error,
        ),
    )


@router.post("/admin/settings")
async def admin_settings_save(
    request: Request,
    user: CurrentUser,
    session: ScopedSession,
    brand_id: Annotated[int, Depends(active_brand_id)],
) -> Response:
    """Save one card at a time.

    The whole card posts together, so a switch that is off can be told apart
    from a field the form never mentioned: every setting in the named category
    is considered, and an absent checkbox means false rather than unchanged.
    """
    if Permission.SETTINGS_MANAGE not in permissions_for(user.role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "your role may not change settings")

    form = await request.form()
    category = str(form.get("category") or "")
    problems: list[str] = []

    for spec in specs_by_category().get(category, []):
        if spec.key in _LOCKED_SETTINGS:
            continue
        field = f"set:{spec.key}"
        scope = brand_id if spec.brand_overridable else None

        if spec.type == SettingType.BOOL:
            submitted: Any = field in form
        else:
            if field not in form:
                continue
            submitted = str(form.get(field) or "")
            # An empty secret box means "keep what is stored", not "clear it" --
            # the box is always empty on load, so treating blank as a clear
            # would wipe every secret on the card each time it is saved.
            if spec.sensitive and not submitted:
                continue

        try:
            await settings_service.set(
                session, spec.key, submitted, brand_id=scope, changed_by=user.email
            )
        except (SettingsError, KeyError) as exc:
            problems.append(str(exc))

    params = urlencode({"error": "; ".join(problems)} if problems else {"saved": category})
    return RedirectResponse(f"/admin/settings?{params}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/audit", response_class=HTMLResponse)
async def audit_page(
    request: Request,
    user: CurrentUser,
    session: ScopedSession,
    action: str | None = None,
    actor: str | None = None,
) -> Response:
    if Permission.AUDIT_VIEW not in permissions_for(user.role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "your role may not view the audit log")
    clauses, params = ["1=1"], {}
    if action:
        clauses.append("action = :action")
        params["action"] = action
    if actor:
        clauses.append("actor_label ILIKE :actor")
        params["actor"] = f"%{actor}%"
    events = (
        await session.execute(
            text(
                f"SELECT * FROM audit_events WHERE {' AND '.join(clauses)} "  # noqa: S608
                "ORDER BY at DESC LIMIT 300"
            ),
            params,
        )
    ).mappings().all()
    actions = (
        await session.execute(text("SELECT DISTINCT action FROM audit_events ORDER BY action"))
    ).scalars().all()
    return templates.TemplateResponse(
        request,
        "audit.html",
        await _shell(
            request, session, user, "audit",
            events=[dict(e) for e in events], actions=list(actions),
            selected_action=action, selected_actor=actor,
        ),
    )


@router.get("/admin/users", response_class=HTMLResponse)
async def admin_users(
    request: Request,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    _guard: Annotated[User, Depends(current_user)],
) -> Response:
    if Permission.USERS_MANAGE not in permissions_for(user.role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "your role may not manage users")
    stmt = select(User).order_by(User.email)
    if not user.is_super_admin:
        stmt = stmt.where(User.brand_id == user.brand_id)
    users = (await session.execute(stmt)).scalars().all()
    return templates.TemplateResponse(
        request, "users.html", await _shell(request, session, user, "users", users=users)
    )



# ------------------------------------------------------------------ my account


@router.get("/me", response_class=HTMLResponse)
async def my_account(
    request: Request,
    user: CurrentUser,
    session: ScopedSession,
    saved: str | None = None,
    error: str | None = None,
) -> Response:
    """Everything about the person signed in, in one place.

    Password and two-factor only appear for an account kept here: for one that
    signs in through Microsoft, Google or a directory, those live in that
    system, and offering them here would be offering something that cannot work.
    """
    return templates.TemplateResponse(
        request,
        "me.html",
        await _shell(
            request,
            session,
            user,
            "me",
            font_choices=(("90", "Smaller"), ("100", "Default"), ("110", "Larger"),
                          ("125", "Largest")),
            theme_choices=(("auto", "Match the system"), ("light", "Light"), ("dark", "Dark")),
            saved=saved,
            error=error,
        ),
    )


@router.post("/me/appearance")
async def save_appearance(
    request: Request,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    """Store what this person chose for themselves.

    On the account rather than in the browser, so it follows them to another
    machine. Values are clamped rather than trusted: these come from a form.
    """
    form = await request.form()
    prefs = dict(user.preferences or {})

    theme = str(form.get("theme") or "auto")
    prefs["theme"] = theme if theme in ("auto", "light", "dark") else "auto"

    try:
        scale = int(str(form.get("font_scale") or 100))
    except ValueError:
        scale = 100
    prefs["font_scale"] = min(max(scale, 80), 150)

    try:
        volume = int(str(form.get("volume") or 100))
    except ValueError:
        volume = 100
    prefs["volume"] = min(max(volume, 0), 100)

    row = (await session.execute(select(User).where(User.id == user.id))).scalar_one()
    row.preferences = prefs
    await session.flush()
    return RedirectResponse("/me?saved=appearance", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/me/password")
async def change_my_password(
    request: Request,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    """Change the password on an account kept here.

    The current one is required: a session left open on an unlocked machine
    should not be enough to lock its owner out. Every other session is ended,
    because a stolen cookie must not outlive the credential it was issued
    against.
    """
    from c2w.auth.local import (
        AuthError,
        hash_password,
        revoke_all_sessions,
        verify_password,
    )

    if user.auth_source != AuthSource.LOCAL:
        return RedirectResponse(
            "/me?error=This+account+signs+in+through+your+organisation",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    form = await request.form()
    current = str(form.get("current") or "")
    new = str(form.get("new") or "")
    again = str(form.get("again") or "")

    row = (await session.execute(select(User).where(User.id == user.id))).scalar_one()
    if not row.password_hash or not verify_password(row.password_hash, current):
        return RedirectResponse(
            "/me?error=Your+current+password+is+not+right",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    if new != again:
        return RedirectResponse(
            "/me?error=The+two+new+passwords+do+not+match",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    try:
        row.password_hash = hash_password(new)
    except AuthError as exc:
        return RedirectResponse(
            f"/me?error={quote_plus(str(exc))}", status_code=status.HTTP_303_SEE_OTHER
        )

    await revoke_all_sessions(session, row.id)
    await session.flush()
    response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(SESSION_COOKIE)
    return response


# ----------------------------------------------------------------------- media


@router.get("/api/v1/recordings/{recording_id}/stream")
async def stream_recording(
    request: Request, user: CurrentUser, session: ScopedSession, recording_id: int
) -> Response:
    return await _media_redirect(request, session, user, recording_id, download=False)


@router.get("/api/v1/recordings/{recording_id}/download")
async def download_recording(
    request: Request, user: CurrentUser, session: ScopedSession, recording_id: int
) -> Response:
    return await _media_redirect(request, session, user, recording_id, download=True)


async def _media_redirect(
    request: Request, session: AsyncSession, user: User, recording_id: int, *, download: bool
) -> Response:
    """Authorise, then redirect to a short-lived archive URL.

    A redirect rather than a proxy: the audio never passes through this process,
    which is what makes concurrent playback across a brand affordable. The URL
    expires in minutes, and every attempt -- allowed or refused -- is audited.
    """
    from c2w.db.models.core import Recording
    from c2w.media.sdr import MediaAction, MediaDenied, playback_url

    row = await get_recording(session, recording_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "recording not found")
    recording = (
        await session.execute(select(Recording).where(Recording.id == recording_id))
    ).scalar_one()

    try:
        access = await playback_url(
            session,
            user,
            recording,
            action=MediaAction.DOWNLOAD if download else MediaAction.PLAY,
            ip=client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
    except MediaDenied as denied:
        code = (
            status.HTTP_409_CONFLICT
            if denied.reason in ("still_syncing", "no_archive_copy")
            else status.HTTP_403_FORBIDDEN
        )
        raise HTTPException(code, denied.message) from denied

    return RedirectResponse(access.url or "", status_code=status.HTTP_307_TEMPORARY_REDIRECT)

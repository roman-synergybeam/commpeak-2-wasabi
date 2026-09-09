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
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
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
from c2w.db.models.auth import User
from c2w.db.models.core import Brand, CommPeakConnection, StorageDestination
from c2w.logging import get_logger
from c2w.settings import SettingsError, settings_service
from c2w.settings_spec import specs_by_category
from c2w.storage.errors import ErrorClass
from c2w.sync import queue
from c2w.web.filters import register as register_filters

log = get_logger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
register_filters(templates.env)

router = APIRouter(include_in_schema=False)


async def _shell(
    request: Request, session: AsyncSession, user: User, nav: str, **extra: Any
) -> dict[str, Any]:
    """Context every page needs: who is signed in, which brand, what they may do."""
    brands = await selectable_brands(session, user)
    brand_cookie = request.cookies.get(BRAND_COOKIE)
    active: Brand | None = None
    if brand_cookie and brand_cookie.isdigit():
        active = next((b for b in brands if b.id == int(brand_cookie)), None)
    if active is None:
        active = brands[0] if brands else None
    return {
        "request": request,
        "user": user,
        "brands": brands,
        "active_brand": active,
        "nav": nav,
        "permissions": {str(p) for p in permissions_for(user.role)},
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
    context = await _sync_context(session)
    return templates.TemplateResponse(
        request, "_sync_panel.html", {**context, "request": request}
    )


# ----------------------------------------------------------------------- admin


@router.get("/admin/connections", response_class=HTMLResponse)
async def admin_connections(
    request: Request, user: CurrentUser, session: ScopedSession
) -> Response:
    connections = (
        (await session.execute(select(CommPeakConnection).order_by(CommPeakConnection.name)))
        .scalars()
        .all()
    )
    destinations = {
        d.id: d for d in (await session.execute(select(StorageDestination))).scalars().all()
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
    counts = {r["connection_id"]: dict(r) for r in counts_rows}
    return templates.TemplateResponse(
        request,
        "connections.html",
        await _shell(
            request, session, user, "connections",
            connections=connections, destinations=destinations, counts=counts,
        ),
    )


@router.get("/admin/storage", response_class=HTMLResponse)
async def admin_storage(request: Request, user: CurrentUser, session: ScopedSession) -> Response:
    destinations = (
        (await session.execute(select(StorageDestination).order_by(StorageDestination.name)))
        .scalars()
        .all()
    )
    return templates.TemplateResponse(
        request,
        "storage.html",
        await _shell(request, session, user, "storage", destinations=destinations),
    )


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
    return templates.TemplateResponse(
        request,
        "settings.html",
        await _shell(
            request, session, user, "settings",
            categories=specs_by_category(), values=values, saved=saved, error=error,
        ),
    )


@router.post("/admin/settings")
async def admin_settings_save(
    request: Request,
    user: CurrentUser,
    session: ScopedSession,
    key: Annotated[str, Form()],
    value: Annotated[str, Form()] = "",
) -> Response:
    if Permission.SETTINGS_MANAGE not in permissions_for(user.role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "your role may not change settings")
    try:
        await settings_service.set(session, key, value, changed_by=user.email)
        params = urlencode({"saved": key})
    except (SettingsError, KeyError) as exc:
        params = urlencode({"error": str(exc)})
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

"""HTML routes.

Server-rendered with Jinja2, progressively enhanced with htmx: the calls table
is a normal GET that also works without JavaScript, and htmx swaps just the
table body when filters change.  There is no build step and no client-side
state to keep in sync with the server.

Filters live in the query string so a filtered view is a URL an operator can
bookmark or paste to a colleague -- which is most of what a CDR search is for.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Final
from urllib.parse import quote, quote_plus, urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from c2w.api.deps import (
    BRAND_COOKIE,
    MFA_COOKIE,
    SESSION_COOKIE,
    CurrentUser,
    ScopedSession,
    active_brand_id,
    client_ip,
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
    filter_options,
    get_call,
    get_recording,
    search_cdrs,
)
from c2w.api.v1.messages import (
    MessageQuery,
    MessageSort,
    count_messages,
    message_filter_options,
    message_stats,
    search_messages,
)
from c2w.audit import AdminAction, record_admin_event
from c2w.auth import directory, mfa, oidc, turnstile
from c2w.auth.local import (
    AuthError,
    authenticate,
    authenticate_directory,
    create_session,
    hash_password,
    link_federated_user,
    revoke_all_sessions,
    revoke_session,
)
from c2w.auth.rbac import Permission, permissions_for
from c2w.crypto import CryptoError, generate_data_key
from c2w.db.models.auth import AuthSource, Role, User, UserBrand
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
    purge_connection,
    purge_destination,
    reveal_credentials,
    test_connection,
    test_destination,
    update_connection,
    update_destination,
    wasabi_region_choices,
)
from c2w.web.filters import register as register_filters
from c2w.web.settings_tests import run_section_test, tests_for

log = get_logger(__name__)

#: The result of the last Test, handed to the page after the redirect.
#:
#: In memory and per process, which is right for what it is: a probe result is
#: interesting for one page view and worthless afterwards. Putting it in the
#: database would mean writing a row on every button press and cleaning them up
#: later; putting it in the session cookie would mean sending a report of
#: someone's credentials back through a browser.
_PROBE_RESULTS: dict[tuple[str, int | None], Any] = {}

#: Stored credentials waiting to be shown once, keyed by (user id, account id).
#:
#: In memory and popped by the render that displays them, never carried in the
#: query string: the redirect after the POST becomes browser history, the
#: referrer of the next request, and a line in anything that logs URLs, and an
#: S3 secret belongs in none of those. Keyed by user as well as account so one
#: administrator's page cannot collect a reveal another one asked for. Not
#: persisted, deliberately -- losing these on restart is the correct behaviour.
_REVEALED: dict[tuple[int, int], tuple[str, str]] = {}

TEMPLATES_DIR = Path(__file__).parent / "templates"
def _asset_version() -> str:
    """A cache-busting stamp for the stylesheets and scripts.

    Without one, a browser keeps serving the CSS it already has and a change
    lands for nobody until they hard-refresh -- which looked like a styling bug
    that could not be reproduced on the server. The newest mtime across the
    static tree, so any edit moves it and an unchanged deploy does not.
    """
    root = Path(__file__).parent / "static"
    newest = 0.0
    for path in root.rglob("*"):
        if path.suffix in {".css", ".js"}:
            newest = max(newest, path.stat().st_mtime)
    return str(int(newest))


templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
register_filters(templates.env)

router = APIRouter(include_in_schema=False)


#: Falls back to the registry default rather than UTC, so the clock and the
#: schedules agree with each other before anyone has chosen a zone.
#: Typefaces on offer. Every one is a stack of families already present on a
#: normal machine: this console runs on a private network with no outbound
#: access, so a downloaded font would silently fall back to something else and
#: the setting would appear to do nothing.
FONT_STACKS: Final[dict[str, tuple[str, str]]] = {
    "system": (
        "Match the system",
        'system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif',
    ),
    "humanist": (
        "Segoe UI / Helvetica",
        '"Segoe UI","Helvetica Neue",Helvetica,Arial,sans-serif',
    ),
    "grotesque": ("Roboto / Arial", 'Roboto,Arial,"Liberation Sans",Helvetica,sans-serif'),
    "serif": ("Georgia / Times", 'Georgia,"Times New Roman",Times,serif'),
    "mono": (
        "Monospace throughout",
        'ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,"Liberation Mono",monospace',
    ),
}

#: Sizes in pixels, so the label says what it does. 20 is the default: four
#: more than the browser's usual 16.
FONT_SIZES: Final[tuple[int, ...]] = (14, 16, 18, 20, 22, 24, 28)
DEFAULT_FONT_PX = 20


def _font_px(prefs: dict[str, Any]) -> int:
    """The chosen size, converting an older percentage preference if present.

    Sizes used to be stored as a percentage of the browser default. Anyone who
    had set one keeps the size they chose rather than being silently reset.
    """
    raw = prefs.get("font_px")
    if raw is None and prefs.get("font_scale"):
        try:
            raw = round(16 * float(prefs["font_scale"]) / 100)
        except (TypeError, ValueError):
            raw = None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_FONT_PX
    return min(max(value, FONT_SIZES[0]), FONT_SIZES[-1])


async def _brand_scope(
    request: Request, session: AsyncSession, user: User
) -> tuple[list[Brand], Brand | None]:
    """The organisations this person can reach, and the one they are looking at.

    Shared rather than repeated: any page that reads a per-brand setting has to
    resolve the same cookie the same way, and two copies of this would drift
    into a page rendering one organisation's data under another's settings.
    """
    brands = await selectable_brands(session, user)
    cookie = request.cookies.get(BRAND_COOKIE)
    active: Brand | None = None
    if cookie and cookie.isdigit():
        active = next((b for b in brands if b.id == int(cookie)), None)
    if active is None:
        active = next((b for b in brands if b.id == user.brand_id), None) or (
            brands[0] if brands else None
        )
    return list(brands), active


async def _shell(
    request: Request, session: AsyncSession, user: User, nav: str, **extra: Any
) -> dict[str, Any]:
    """Context every page needs.

    Permissions are for the organisation being looked at, not the one on the
    account: the same person can be an admin for one of these companies and an
    operator for the other.
    """
    brands, active = await _brand_scope(request, session, user)

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
        # The platform's own name, one setting rather than five hardcoded
        # strings that drifted apart -- the authenticator said "c2w" while the
        # header said something else.
        "asset_v": _asset_version(),
        "platform_name": await settings_service.get_str(
            session, "core.platform_name", brand_id=active.id if active else None
        ),
        # Resolved here rather than in the template: an unknown value must fall
        # back to a real stack, not to an empty `font-family:` declaration.
        "font_stack": FONT_STACKS.get(
            str((user.preferences or {}).get("font_family") or "system"),
            FONT_STACKS["system"],
        )[1],
        # Drives whether the Messages tab appears at all: an empty page behind
        # a menu item that will never have data is worse than no menu item.
        "sms_enabled": await settings_service.get_bool(
            session, "sms.enabled", brand_id=active.id if active else None
        ),
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
    # Only providers that could actually complete a sign-in. `load_provider`
    # raises when one is switched off or missing its client details, and a
    # button that always fails is worse than no button.
    providers = []
    for key in ("entra", "google"):
        try:
            await oidc.load_provider(session, key)
        except oidc.OidcError:
            continue
        providers.append({"key": key, "label": oidc.PROVIDERS[key].label})
    sso = bool(providers)
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "request": request,
            "no_users": no_users,
            "sso_enabled": sso,
            "sso_providers": providers,
            "platform_name": await settings_service.get_str(session, "core.platform_name"),
            "asset_v": _asset_version(),
            "turnstile": await turnstile.load_gate(session, client_ip=client_ip(request)),
        },
    )


async def _sign_in(
    request: Request,
    session: AsyncSession,
    user: User,
    response: Response,
) -> Response:
    """Attach a real session to ``response``.

    Split out because sign-in now finishes in three places -- straight from the
    password, after a code, and after a forced enrolment -- and a cookie that
    is set with different flags depending on which path you came in by is a
    security bug waiting to happen.
    """
    token, _ = await create_session(
        session, user, ip=client_ip(request), user_agent=request.headers.get("user-agent")
    )
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
    response.delete_cookie(MFA_COOKIE)
    log.info("login.ok", user_id=user.id, role=str(user.role))
    return response


def _set_ticket(response: Response, request: Request, user: User) -> Response:
    """Hand the browser a half-login while the second factor is collected.

    A ticket rather than a session: a session that exists before the code has
    been checked is a session that works, which would make the second step
    decorative.
    """
    response.set_cookie(
        MFA_COOKIE,
        mfa.issue_ticket(user),
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        max_age=mfa.TICKET_MAX_AGE_SECONDS,
    )
    return response


async def _ticket_user(request: Request, session: AsyncSession) -> User | None:
    """The half-logged-in user, or None if the ticket is missing or stale."""
    user_id = mfa.read_ticket(request.cookies.get(MFA_COOKIE) or "")
    if user_id is None:
        return None
    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None or not user.is_active:
        return None
    return user


@router.post("/login")
async def login_submit(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
) -> Response:
    try:
        # The directory first: an account that belongs to Active Directory has
        # no local password to check, and asking the local path first would
        # only produce a misleading "invalid email or password" for it.
        user = await authenticate_directory(session, email, password)
        if user is None:
            user = await authenticate(session, email, password)
    except AuthError as exc:
        log.info("login.failed", email=email[:64], reason=str(exc))
        return templates.TemplateResponse(
            request,
            "login.html",
            {"request": request, "error": str(exc)},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    # The password was right, which is not the same as being signed in.
    if await mfa.second_factor_required(session, user):
        where = "/login/code" if user.totp_active else "/login/enrol"
        log.info("login.second_factor", user_id=user.id, step=where)
        return _set_ticket(
            RedirectResponse(where, status_code=status.HTTP_303_SEE_OTHER), request, user
        )

    return await _sign_in(
        request, session, user, RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    )


@router.get("/auth/{provider}/start")
async def oidc_start(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    provider: str,
) -> Response:
    """Send the browser to Microsoft or Google to sign in."""
    try:
        config = await oidc.load_provider(session, provider)
        url, cookie = await oidc.authorize_url(config, _oidc_redirect_uri(request, provider))
    except oidc.OidcError as exc:
        return RedirectResponse(
            f"/login?error={quote_plus(str(exc))}", status_code=status.HTTP_303_SEE_OTHER
        )
    except Exception as exc:
        log.warning("oidc.start_failed", provider=provider, error=str(exc)[:200])
        return RedirectResponse(
            "/login?error=" + quote_plus("Could not reach that sign-in provider"),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    response = RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        oidc.COOKIE, cookie, **oidc.flow_cookie_kwargs(secure=request.url.scheme == "https")
    )
    return response


@router.get("/auth/{provider}/callback")
async def oidc_callback(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    provider: str,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> Response:
    """Finish the flow, and sign the person in if everything checks out."""

    def refuse(message: str) -> Response:
        response = RedirectResponse(
            f"/login?error={quote_plus(message)}", status_code=status.HTTP_303_SEE_OTHER
        )
        # The flow cookie is single-use whatever happens; leaving it would let
        # a stale state be replayed.
        response.delete_cookie(oidc.COOKIE, path="/")
        return response

    if error:
        # The provider itself refused -- consent declined, account blocked.
        log.info("oidc.provider_error", provider=provider, error=error)
        return refuse(error_description or f"{provider} refused the sign-in")
    if not code or not state:
        return refuse("That sign-in was incomplete; please start again")

    try:
        config = await oidc.load_provider(session, provider)
        identity = await oidc.complete(
            config,
            code=code,
            state=state,
            cookie=request.cookies.get(oidc.COOKIE) or "",
        )
    except oidc.OidcError as exc:
        return refuse(str(exc))
    except Exception as exc:
        log.warning("oidc.callback_failed", provider=provider, error=str(exc)[:200])
        return refuse("That sign-in could not be completed")

    try:
        user = await link_federated_user(
            session,
            identity_email=identity.email,
            subject=identity.subject,
            display_name=identity.name,
            groups=identity.groups,
            source=AuthSource.ENTRA if provider == "entra" else AuthSource.GOOGLE,
        )
    except AuthError as exc:
        return refuse(str(exc))

    log.info("oidc.signed_in", provider=provider, user_id=user.id)
    signed_in = await _sign_in(
        request, session, user, RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    )
    signed_in.delete_cookie(oidc.COOKIE, path="/")
    return signed_in


def _oidc_redirect_uri(request: Request, provider: str) -> str:
    """The callback address, which must match what is registered at the provider.

    Built from the request rather than from a setting, so it is right on the
    LAN address, through the tunnel and on localhost without three settings
    that can disagree. `core.base_url` overrides it when set, because behind a
    proxy the request's own host can be the internal one.
    """
    return f"{str(request.base_url).rstrip('/')}/auth/{provider}/callback"


@router.get("/login/code", response_class=HTMLResponse)
async def login_code_form(
    request: Request, session: Annotated[AsyncSession, Depends(get_session)]
) -> Response:
    user = await _ticket_user(request, session)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        request,
        "login_code.html",
        {
            "request": request,
            "email": user.email,
            "platform_name": await settings_service.get_str(session, "core.platform_name"),
            "asset_v": _asset_version(),
        },
    )


@router.post("/login/code")
async def login_code_submit(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    code: Annotated[str, Form()],
) -> Response:
    user = await _ticket_user(request, session)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    try:
        await mfa.check_second_factor(session, user, code)
    except AuthError as exc:
        log.info("login.code_failed", user_id=user.id, reason=str(exc))
        return templates.TemplateResponse(
            request,
            "login_code.html",
            {
                "request": request,
                "email": user.email,
                "error": str(exc),
                "platform_name": await settings_service.get_str(
                    session, "core.platform_name"
                ),
            },
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    return await _sign_in(
        request, session, user, RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    )


@router.get("/login/enrol", response_class=HTMLResponse)
async def login_enrol_form(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    error: str | None = None,
) -> Response:
    """Forced enrolment: an authenticator is required and this account has none.

    Reached only with a valid ticket, so the password has already been checked
    and no session exists yet -- the account cannot be used until the factor it
    is required to have actually works.
    """
    user = await _ticket_user(request, session)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    try:
        enrolment = await mfa.begin_enrolment(session, user)
    except AuthError as exc:
        return templates.TemplateResponse(
            request, "login.html", {"request": request, "error": str(exc)},
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return templates.TemplateResponse(
        request,
        "login_enrol.html",
        {
            "request": request,
            "email": user.email,
            "enrolment": enrolment,
            "error": error,
            "platform_name": await settings_service.get_str(session, "core.platform_name"),
            "asset_v": _asset_version(),
        },
    )


@router.post("/login/enrol")
async def login_enrol_submit(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    code: Annotated[str, Form()],
) -> Response:
    user = await _ticket_user(request, session)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    try:
        codes = await mfa.confirm_enrolment(session, user, code)
    except AuthError as exc:
        return RedirectResponse(
            f"/login/enrol?error={quote_plus(str(exc))}", status_code=status.HTTP_303_SEE_OTHER
        )
    # Signed in and shown the recovery codes on the same response: they exist
    # exactly once, and bouncing through a redirect would lose them.
    page = templates.TemplateResponse(
        request, "recovery_codes.html",
        {"request": request, "codes": codes, "next_url": "/", "standalone": True},
    )
    return await _sign_in(request, session, user, page)


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


#: How often the dashboard's figures refresh, in seconds.
#:
#: Ten is short enough that a running backfill visibly moves and long enough
#: that a page left open all day is not a load problem: the fragment is three
#: aggregate queries, and it replaces only the numbers rather than the page.
_DASHBOARD_REFRESH_SECONDS: Final[int] = 10


async def _dashboard_live_context(session: AsyncSession) -> dict[str, Any]:
    """The figures the dashboard refreshes, and nothing else."""
    stats = await dashboard_stats(session)
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
    return {
        "stats": stats,
        "state_rows": [(name, known[name]) for name in order if name in known],
        # Rendered rather than done in the browser, so it says when the server
        # produced these numbers -- which is the question a stale-looking
        # dashboard raises -- and not merely when the tab last drew.
        "live_at": f"{datetime.now(UTC):%H:%M:%S}Z",
        "live_every": _DASHBOARD_REFRESH_SECONDS,
    }


@router.get("/dashboard/live", response_class=HTMLResponse)
async def dashboard_live(
    request: Request,
    user: CurrentUser,
    session: ScopedSession,
) -> Response:
    """Just the figures, for the poll. No shell, no navigation.

    Brand-scoped like every other request, so a poll cannot see across
    organisations any more than the page it came from can.
    """
    return templates.TemplateResponse(
        request,
        "_dashboard_live.html",
        await _dashboard_live_context(session),
    )


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    user: CurrentUser,
    session: ScopedSession,
) -> Response:
    destinations = (await session.execute(select(StorageDestination))).scalars().all()
    transfers_enabled = await settings_service.get_bool(session, "transfer.enabled")
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        await _shell(
            request,
            session,
            user,
            "dashboard",
            has_destination=bool(destinations),
            transfers_enabled=transfers_enabled,
            **await _dashboard_live_context(session),
        ),
    )


# ----------------------------------------------------------------------- calls


def _parse_query(
    *,
    number: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    direction: str | None = None,
    agent: str | None = None,
    media: str | None = None,
    connection_id: int | None = None,
    status_filter: str | None = None,
    min_duration: int | None = None,
    country: str | None = None,
    queue: str | None = None,
    call_type: str | None = None,
    sort: str = "started",
    desc: str = "1",
    limit: int = 50,
    offset: int = 0,
) -> CdrQuery:
    """Turn query-string strings into a validated :class:`CdrQuery`.

    Keyword-only: this had thirteen positional parameters of which eight were
    ``str | None``, called from three places. Adding one in the middle would
    have silently shifted every argument after it, and every one of them would
    still have type-checked.
    """
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
        country=country or None,
        queue=queue or None,
        call_type=call_type or None,
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
        "options": await filter_options(session),
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
    country: str | None = None,
    queue: str | None = None,
    call_type: str | None = None,
    sort: str = "started",
    desc: str = "1",
    limit: int = 50,
    offset: int = 0,
) -> Response:
    query = _parse_query(
        number=number, date_from=date_from, date_to=date_to, direction=direction,
        agent=agent, media=media, connection_id=connection_id,
        status_filter=status_filter, min_duration=min_duration,
        country=country, queue=queue, call_type=call_type,
        sort=sort, desc=desc, limit=limit, offset=offset,
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
    country: str | None = None,
    queue: str | None = None,
    call_type: str | None = None,
    sort: str = "started",
    desc: str = "1",
    limit: int = 50,
    offset: int = 0,
) -> Response:
    """The table fragment htmx swaps in."""
    query = _parse_query(
        number=number, date_from=date_from, date_to=date_to, direction=direction,
        agent=agent, media=media, connection_id=connection_id,
        status_filter=status_filter, min_duration=min_duration,
        country=country, queue=queue, call_type=call_type,
        sort=sort, desc=desc, limit=limit, offset=offset,
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
    country: str | None = None,
    queue: str | None = None,
    call_type: str | None = None,
) -> Response:
    """Stream a CSV of the current filter.

    Streamed rather than assembled in memory: an export can legitimately cover
    a month of calls, and buffering that would put an unbounded allocation in
    the request path.
    """
    if Permission.CDR_EXPORT not in permissions_for(user.role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "your role may not export CDRs")

    query = _parse_query(
        number=number, date_from=date_from, date_to=date_to, direction=direction,
        agent=agent, media=media, connection_id=connection_id,
        min_duration=min_duration, country=country, queue=queue, call_type=call_type,
        limit=200,
    )

    async def rows():
        import csv
        import io

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            [
                "started_at", "ended_at", "direction", "from", "to",
                "country", "agent", "second_agent", "queue", "call_type",
                "commpeak_account", "duration_seconds", "billed_seconds", "cost",
                "status", "call_uuid", "recording_state", "recording_parts",
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
                        row["dst_country"] or "",
                        row["agent_name"] or row["agent_extension"] or "",
                        row["bridged_agent_name"] or row["bridged_agent_extension"] or "",
                        row["queue_name"] or "",
                        row["call_type"] or "",
                        row["connection_name"] or "",
                        row["call_duration"] or "",
                        row["bill_duration"] or "",
                        row["cost"] if row["cost"] is not None else "",
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
                SELECT k.id, k.name, k.last_inventory_at, k.inventory_cursor_day,
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
                GROUP BY k.id, k.name, k.last_inventory_at, k.inventory_cursor_day
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
            revealed=_REVEALED.pop((user.id, tested), None) if tested else None,
            revealed_for=tested,
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
            target = _account_target(form, "/admin/connections", saved=conn.name)
        elif action == "update":
            conn = await update_connection(
                session, brand_id, int(form["connection_id"]), form, actor=user.email
            )
            target = _account_target(form, "/admin/connections", saved=conn.name)
        elif action == "test":
            connection_id = int(form["connection_id"])
            outcome = await test_connection(session, brand_id, connection_id)
            _PROBE_RESULTS[("connection", connection_id)] = outcome
            target = _account_target(
                form, "/admin/connections", tested=connection_id
            )
        elif action == "reveal":
            connection_id = int(form["connection_id"])
            name, token, secret = await reveal_credentials(
                session, brand_id, connection_id
            )
            _REVEALED[(user.id, connection_id)] = (token, secret)
            await record_admin_event(
                session,
                actor=user,
                action=AdminAction.CREDENTIALS_REVEALED,
                brand_id=brand_id,
                ip=client_ip(request),
                user_agent=request.headers.get("user-agent"),
                # The account, never the values. An audit row that copied the
                # secret in would have defeated the sealed column it came out
                # of; what matters is that somebody looked, and at which one.
                detail={"account": name, "connection_id": connection_id},
            )
            target = _account_target(
                form, "/admin/connections", tested=connection_id
            )
        elif action == "stop":
            name = await delete_connection(
                session, brand_id, int(form["connection_id"]), actor=user.email
            )
            target = _account_target(
                form, "/admin/connections", saved=f"{name} stopped"
            )
        elif action == "delete":
            name = await purge_connection(
                session, brand_id, int(form["connection_id"]), actor=user.email
            )
            target = _account_target(
                form, "/admin/connections", saved=f"{name} deleted"
            )
        else:
            target = "/admin/connections"
    except (AccountError, TransferError, CryptoError) as exc:
        target = _account_target(form, "/admin/connections", error=str(exc))
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
            target = _account_target(form, "/admin/storage", saved=dest.name)
        elif action == "update":
            dest = await update_destination(
                session, brand_id, int(form["destination_id"]), form, actor=user.email
            )
            target = _account_target(form, "/admin/storage", saved=dest.name)
        elif action == "test":
            destination_id = int(form["destination_id"])
            outcome = await test_destination(session, brand_id, destination_id)
            _PROBE_RESULTS[("destination", destination_id)] = outcome
            target = _account_target(form, "/admin/storage", tested=destination_id)
        elif action == "stop":
            name = await delete_destination(
                session, brand_id, int(form["destination_id"]), actor=user.email
            )
            target = _account_target(
                form, "/admin/storage", saved=f"{name} stopped"
            )
        elif action == "delete":
            name = await purge_destination(
                session, brand_id, int(form["destination_id"]), actor=user.email
            )
            target = _account_target(
                form, "/admin/storage", saved=f"{name} deleted"
            )
        else:
            target = "/admin/storage"
    except (AccountError, TransferError, CryptoError) as exc:
        target = _account_target(form, "/admin/storage", error=str(exc))
    return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)


#: A one-line orientation for each card, above its fields.
_CATEGORY_NOTES = {
    "Your company": "Who this organisation is, and where its recordings may live.",
    "CommPeak calls": "An organisation can have as many CommPeak accounts as "
    "it has PBXes and dialers -- each with its own bucket and its own "
    "credentials, added on the CommPeak page. What follows is shared by all of "
    "them.",
    "CommPeak messages": "Text messages sent and received through CommPeak TextPeak, "
    "listed beside the calls. This is a separate API and a separate key from "
    "call records -- one does not imply the other.",
    "Wasabi storage": "An organisation can have as many Wasabi accounts and "
    "buckets as it needs; they are added on the Archive page, each with its own "
    "keys. What follows applies to all of them.",
    "Copying to the archive": "How hard to work while copying, and what to do when a copy fails.",
    "Retention": "How long recordings stay where.",
    "Playback and downloads": "How a recording reaches a browser.",
    "Alerts": "Where alerts go. Leave the tokens blank to send none.",
    "Scheduling": "When unattended work happens.",
    "Microsoft 365": "Let staff sign in with their Microsoft work account "
    "instead of a password kept here.",
    "Google Workspace": "Let staff sign in with their Google work account.",
    "Active Directory": "Take the list of users, and who is an administrator, "
    "from a domain controller you run.",
    "Two-factor and passwords": "Applies to accounts kept here. Accounts from "
    "Microsoft, Google or your directory follow that system's rules.",
    "Transcription and voice analysis": "Turning recorded speech into searchable "
    "text. Nothing here runs yet -- the settings and the storage are in place, "
    "the recogniser is not.",
    "Cloudflare": "Reaching this console from outside, and keeping robots off "
    "the sign-in page.",
    "Web address and sessions": "How users reach this console, and how long a sign-in lasts.",
    "Logs and monitoring": "What the services write down.",
}

#: Fields whose value is long enough to want two columns.
_WIDE_FIELDS = frozenset(
    {
        "core.base_url",
        "org.data_region_note",
        "commpeak.s3_endpoint",
        "commpeak.cdr_api_base",
        "sms.api_base",
        "sms.api_path",
        "sms.incoming_path",
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
    "CommPeak calls": ("/admin/connections", "Manage CommPeak accounts"),
    "Wasabi storage": ("/admin/storage", "Manage archive storage"),
    # A section of this same page now, so the link goes straight there
    # instead of out to the old address and back through its redirect.
    "Two-factor and passwords": ("/admin/settings?section=users", "Manage users"),
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
            "label": "Add the users who need access",
            "detail": f"{users} account(s)",
            "done": users > 1,
            "optional": True,
            "href": "/admin/settings?section=users",
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


#: The left rail. One hundred and nine settings in seventeen cards was a single
#: page you scrolled to find anything on, so each card is now its own view and
#: these are the groups the rail is divided into. Order matters: it is roughly
#: the order somebody sets the system up in, not alphabetical.
#:
#: `SETUP_SECTION` is not a settings category -- it is the checklist that used
#: to sit above everything else, kept as the landing view because "what still
#: needs doing" is the question a half-configured install raises.
SETUP_SECTION = "Setup"

#: Two more sections that are not settings categories either: they were pages
#: of their own in the main menu, and both are configuration by any reading --
#: an organisation is the thing every other setting hangs off, and who may
#: sign in belongs beside the sign-in sources it depends on. Each one is
#: guarded by its own permission rather than by `settings.view`, so the rail
#: shows an organisation admin the users section and not the organisations one.
ORGANISATIONS_SECTION = "Organisations"
USERS_SECTION = "Users"

#: What a section needs beyond `settings.view` to appear in the rail at all.
_SECTION_PERMISSION: Final[dict[str, Permission]] = {
    ORGANISATIONS_SECTION: Permission.BRANDS_MANAGE,
    USERS_SECTION: Permission.USERS_MANAGE,
}

_SETTING_GROUPS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    # "Your company" belongs here rather than off in a technical group: the
    # name, the time zone and where recordings may live are the first things
    # anyone sets, and the checklist points at them.
    ("Getting started", (SETUP_SECTION, "Your company", ORGANISATIONS_SECTION)),
    (
        "Calls and messages",
        (
            "CommPeak calls",
            "CommPeak messages",
            "Playback and downloads",
            "Transcription and voice analysis",
        ),
    ),
    ("Archive", ("Wasabi storage", "Copying to the archive", "Retention")),
    (
        "Users and sign-in",
        (
            "Two-factor and passwords",
            "Web address and sessions",
            "Active Directory",
            "Microsoft 365",
            "Google Workspace",
            USERS_SECTION,
        ),
    ),
    ("Alerts and schedules", ("Alerts", "Scheduling")),
    ("Server and access", ("Cloudflare", "Logs and monitoring")),
)


def _section_slug(name: str) -> str:
    """A URL-safe id for a section name.

    The section lives in the query string rather than in a fragment so that a
    link to one is a link somebody can paste, and so the save handler can send
    you back to the card you were editing instead of to the top of the page.
    """
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _settings_sections(
    categories: dict[str, Any], permissions: frozenset[Permission]
) -> list[dict[str, Any]]:
    """The rail: groups, each with its sections and their setting counts.

    Built from ``_SETTING_GROUPS`` but reconciled against the registry, so a
    category added to `settings_spec` without being placed in a group still
    appears -- at the end, under "Other". A new setting silently missing from
    the UI is worse than one filed untidily.

    Sections in ``_SECTION_PERMISSION`` are dropped for anyone without that
    permission. They are the two that were pages of their own, and each is
    still guarded by the permission its old page checked -- moving a page into
    this rail must not widen who can reach it.
    """
    placed = {name for _, names in _SETTING_GROUPS for name in names}
    groups: list[dict[str, Any]] = []
    for title, names in _SETTING_GROUPS:
        # Named "sections", never "items": in a Jinja attribute lookup
        # `group.items` finds dict.items -- the bound method -- rather than the
        # key, and iterating it raises. Renaming the key is a better fix than
        # remembering to write `group["items"]` in every template.
        sections = []
        for name in names:
            needed = _SECTION_PERMISSION.get(name)
            if needed is not None:
                if needed not in permissions:
                    continue
            elif name != SETUP_SECTION and name not in categories:
                continue
            sections.append(
                {
                    "name": name,
                    "slug": _section_slug(name),
                    "count": len(categories.get(name, [])),
                }
            )
        if sections:
            groups.append({"title": title, "sections": sections})

    unplaced = [name for name in categories if name not in placed]
    if unplaced:
        groups.append(
            {
                "title": "Other",
                "sections": [
                    {"name": n, "slug": _section_slug(n), "count": len(categories[n])}
                    for n in unplaced
                ],
            }
        )
    return groups


def _search_settings(
    query: str | None, permissions: frozenset[Permission]
) -> list[dict[str, Any]]:
    """Find settings by label, key, description or category.

    112 settings across 17 sections is more than anyone can hold in their head,
    and the rail only helps if you already know which section a thing lives in.
    Somebody looking for "telegram" should not have to guess that it is filed
    under Alerts.

    Matches on the **key** as well as the prose, because a support conversation
    or a log line names the key (`alerts.telegram_chat_id`) and pasting that in
    should land you on it.

    Ranked so the useful answer is first: a label match beats a key match beats
    a mention somewhere in the description. Within a rank, alphabetical -- a
    stable order matters when somebody is comparing two searches.
    """
    term = (query or "").strip().lower()
    if len(term) < 2:
        # One character matches most of the registry, which is not a search
        # result, it is the whole list with extra steps.
        return []

    matches: list[tuple[int, str, dict[str, Any]]] = []
    for spec in SETTINGS.values():
        needed = _SECTION_PERMISSION.get(spec.category)
        if needed is not None and needed not in permissions:
            continue
        label = spec.label.lower()
        if term in label:
            rank = 0
        elif term in spec.key.lower():
            rank = 1
        elif term in spec.category.lower():
            rank = 2
        elif term in spec.description.lower():
            rank = 3
        else:
            continue
        matches.append(
            (
                rank,
                spec.label,
                {
                    "key": spec.key,
                    "label": spec.label,
                    "category": spec.category,
                    "slug": _section_slug(spec.category),
                    "description": spec.description,
                    "sensitive": spec.sensitive,
                    "unit": spec.unit,
                },
            )
        )
    matches.sort(key=lambda row: (row[0], row[1]))
    return [row[2] for row in matches]


@router.get("/admin/settings", response_class=HTMLResponse)
async def admin_settings(
    request: Request,
    user: CurrentUser,
    session: ScopedSession,
    brand_id: Annotated[int, Depends(active_brand_id)],
    section: str | None = None,
    saved: str | None = None,
    error: str | None = None,
    tested: str | None = None,
    probe_id: int | None = None,
    q: str | None = None,
) -> Response:
    """One section at a time, chosen from the rail on the left.

    Everything used to render on one page: seventeen cards, a hundred and nine
    settings, and no way to get to the one you wanted except scrolling. Only
    the requested section is built now, so the page is also a great deal
    smaller.
    """
    permissions = permissions_for(user.role)
    if Permission.SETTINGS_VIEW not in permissions:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "your role may not view settings")

    all_categories = specs_by_category()
    groups = _settings_sections(all_categories, permissions)
    by_slug = {
        s["slug"]: s["name"] for group in groups for s in group["sections"]
    }
    # An unknown or absent section lands on the checklist rather than 404ing:
    # a stale bookmark should show you something useful. A section the role may
    # not see is absent from `by_slug` and so lands there too, which is the
    # right answer for a link passed on by somebody with more access.
    active_section = by_slug.get(str(section or ""), SETUP_SECTION)

    values = await settings_service.all_effective(session, brand_id=brand_id)
    brand = (
        await session.execute(select(Brand).where(Brand.id == brand_id))
    ).scalar_one_or_none()

    # The last few characters of a stored secret, so it can be checked against
    # the console it was copied from without being revealed.
    # Only for the section on screen. Every one of these is an unseal, and
    # doing all of them on every page load was work thrown away for the
    # sixteen cards that were not being looked at.
    tails: dict[str, str] = {}
    for spec in all_categories.get(active_section, []):
        if not spec.sensitive:
            continue
        current = await settings_service.get_secret(session, spec.key, brand_id=brand_id)
        if current:
            tails[spec.key] = f"stored, ending …{current[-4:]}"

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
            categories=all_categories,
            section_groups=groups,
            active_section=active_section,
            active_slug=_section_slug(active_section),
            setup_section=SETUP_SECTION,
            values=values,
            secret_tails=tails,
            category_notes=_CATEGORY_NOTES,
            wide_fields=_WIDE_FIELDS,
            locked_settings=_LOCKED_SETTINGS,
            manage_links={k: v[0] for k, v in _MANAGE_LINKS.items()},
            manage_link_labels={k: v[1] for k, v in _MANAGE_LINKS.items()},
            setup_steps=await _setup_steps(session, brand),
            # The account managers are rendered inline for these two sections,
            # so the page needs everything their own pages needed. Loaded only
            # for the section on screen.
            **(
                await _commpeak_section_context(
                    session, tested_id=probe_id, user_id=user.id
                )
                if active_section == "CommPeak calls"
                else await _archive_section_context(session, tested_id=probe_id)
                if active_section == "Wasabi storage"
                # These two were pages of their own. Their context builders are
                # unchanged and still used by the pages that replaced them, so
                # what the pane renders cannot drift from what they served.
                else await _organisations_pane(session)
                if active_section == ORGANISATIONS_SECTION
                else await _users_pane(request, session, user, error=error, saved=saved)
                if active_section == USERS_SECTION
                else {}
            ),
            organisations_section=ORGANISATIONS_SECTION,
            users_section=USERS_SECTION,
            settings_query=(q or "").strip(),
            settings_matches=_search_settings(q, permissions),
            section_tests=tests_for(active_section),
            test_result=_SECTION_TESTS.pop(f"{user.id}:{tested}", None) if tested else None,
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

    # Back to the same section: being bounced to the checklist after saving
    # something would mean re-navigating for every change.
    params = {"section": _section_slug(category)}
    if problems:
        params["error"] = "; ".join(problems)
    else:
        params["saved"] = category
    return RedirectResponse(
        f"/admin/settings?{urlencode(params)}", status_code=status.HTTP_303_SEE_OTHER
    )


#: Results of a section test, held between the POST and the redirect that
#: follows it. In memory and per process, like the connection probes: a test
#: result is worth showing once and is not worth a table.
_SECTION_TESTS: dict[str, Any] = {}


#: The only places an account form is rendered, and therefore the only places
#: it may send you back to. An allowlist rather than validation, because
#: "does this look like one of our URLs" is how open redirects get written.
_ACCOUNT_RETURNS: Final[frozenset[str]] = frozenset(
    {
        "/admin/connections",
        "/admin/storage",
        "/admin/settings?section=commpeak-calls",
        "/admin/settings?section=wasabi-storage",
    }
)


def _account_target(
    form: dict[str, str],
    default: str,
    *,
    saved: str | None = None,
    error: str | None = None,
    tested: int | None = None,
) -> str:
    """Where a saved account form should land, with its message attached.

    Submitting from the settings page used to bounce you to the standalone
    page, which loses your place for no reason. The requested page is checked
    against an allowlist rather than validated by shape, because "does this
    look like one of our URLs" is how open redirects get written.

    The probe result is keyed `probe_id` on the settings page and `tested` on
    the standalone ones, because `tested` already means something else there.
    """
    wanted = str(form.get("return_to") or "").strip()
    base = wanted if wanted in _ACCOUNT_RETURNS else default
    joiner = "&" if "?" in base else "?"
    if saved is not None:
        return f"{base}{joiner}saved={quote_plus(saved)}"
    if error is not None:
        return f"{base}{joiner}error={quote_plus(error)}"
    if tested is not None:
        key = "probe_id" if base.startswith("/admin/settings") else "tested"
        return f"{base}{joiner}{key}={tested}"
    return base


async def _commpeak_section_context(
    session: AsyncSession, *, tested_id: int | None, user_id: int
) -> dict[str, Any]:
    """What `_commpeak_accounts.html` needs, wherever it is rendered.

    ``user_id`` only to collect a reveal this person asked for: keying it by
    account alone would let one administrator's page display the credentials
    another one had just looked up.
    """
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
        t.id: t
        for t in (await session.execute(select(Tenant))).scalars().all()
    }
    # Both columns, and the same shape the standalone page uses. This used to
    # be `{id: <int>}` while the shared template reads
    # `counts.get(c.id, {}).get('total')`, which raises on an int -- and it
    # raised nowhere, because `recordings` was empty for every organisation, so
    # the `{}` default answered every lookup. The moment one account had a
    # single recording this section would have failed to render, which is to
    # say the day the system started working.
    counts = (
        await session.execute(
            text(
                "SELECT connection_id, count(*) AS total, "
                "count(*) FILTER (WHERE state IN ('AVAILABLE','SOURCE_DELETED')) AS archived "
                "FROM recordings GROUP BY connection_id"
            )
        )
    ).mappings().all()
    return {
        "connections": connections,
        # A dict keyed by id, because the shared template calls
        # `destinations.get(c.destination_id)`. A list survived here only
        # because that call sits behind `c.destination_id and ...` and no
        # account has a destination yet -- the same latent shape mismatch as
        # `counts` below, waiting for the first account to be pointed at a
        # bucket. `destination_list` is what the picker iterates.
        "destinations": {d.id: d for d in destinations},
        "destination_list": destinations,
        "tenants": tenants,
        "counts": {row["connection_id"]: dict(row) for row in counts},
        "probe": _PROBE_RESULTS.pop(("connection", tested_id), None) if tested_id else None,
        "probe_for": tested_id,
        # Popped, so a refresh does not show it again.
        "revealed": _REVEALED.pop((user_id, tested_id), None) if tested_id else None,
        "revealed_for": tested_id,
    }


async def _archive_section_context(
    session: AsyncSession, *, tested_id: int | None
) -> dict[str, Any]:
    """What `_archive_accounts.html` needs, wherever it is rendered."""
    destinations = (
        (await session.execute(select(StorageDestination).order_by(StorageDestination.name)))
        .scalars()
        .all()
    )
    return {
        "destinations": destinations,
        "providers": ("wasabi", "s3", "minio", "backblaze", "other"),
        "regions": wasabi_region_choices(),
        "probe": _PROBE_RESULTS.pop(("destination", tested_id), None) if tested_id else None,
        "probe_for": tested_id,
    }


@router.post("/admin/settings/test")
async def test_settings_section(
    request: Request,
    user: CurrentUser,
    session: ScopedSession,
    brand_id: Annotated[int, Depends(active_brand_id)],
) -> Response:
    """Prove one settings card actually reaches what it is configured for.

    Every one of these cards is values copied out of somebody else's console,
    and the failure mode is always the same: it looks configured and does
    nothing. So the card talks to the thing now and says what came back.
    """
    if Permission.SETTINGS_MANAGE not in permissions_for(user.role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "your role may not test settings")

    form = await request.form()
    category = str(form.get("category") or "")
    check = str(form.get("check") or "")
    outcome = await run_section_test(
        session, category, brand_id=brand_id, actor=user.email, check=check
    )
    _SECTION_TESTS[f"{user.id}:{category}"] = outcome
    log.info(
        "settings.tested",
        category=category,
        check=check or "(default)",
        ok=outcome.ok,
        actor=user.email,
    )
    return RedirectResponse(
        f"/admin/settings?section={_section_slug(category)}&tested={quote_plus(category)}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/audit", response_class=HTMLResponse)
async def audit_page(
    request: Request,
    user: CurrentUser,
    session: ScopedSession,
    action: str | None = None,
    actor: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> Response:
    """The change log: one line per thing that happened, newest first.

    Two sources, one timeline. `audit_events` says who did something --
    listened to a call, created an account, cleared a second factor.
    `setting_history` says what a setting was changed *from* and *to*, which
    until now was recorded faithfully and shown nowhere at all.

    A UNION rather than two tables on one page, because "what changed here
    last week" does not care which of our tables the answer is in.

    It was a nine-column table before. Nine columns of mostly-empty cells
    collapse, on the kit's own narrow rules, into a stack of one-cell rows --
    which is what made the page unreadable. A log is a list of lines, so this
    renders lines.
    """
    if Permission.AUDIT_VIEW not in permissions_for(user.role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "your role may not view the change log")

    limit = max(20, min(limit, 500))
    offset = max(0, offset)

    where_events = ["1=1"]
    where_settings = ["1=1"]
    params: dict[str, Any] = {"limit": limit + 1, "offset": offset}
    if action:
        where_events.append("action = :action")
        # A settings change has one action name of its own, so filtering by
        # any other action must exclude the settings side entirely rather
        # than silently ignoring the filter.
        where_settings.append(":action = 'SETTING_CHANGED'")
        params["action"] = action
    if actor:
        where_events.append("actor_label ILIKE :actor")
        where_settings.append("changed_by ILIKE :actor")
        params["actor"] = f"%{actor}%"

    sql = f"""
        SELECT at, actor_label, action, result, ip, detail,
               recording_id, call_uuid, NULL::text AS key,
               NULL::text AS old_value, NULL::text AS new_value, NULL::text AS note
        FROM audit_events
        WHERE {" AND ".join(where_events)}
        UNION ALL
        SELECT at, changed_by AS actor_label, 'SETTING_CHANGED' AS action,
               'SUCCESS' AS result, NULL AS ip, '{{}}'::jsonb AS detail,
               NULL::bigint AS recording_id, NULL::text AS call_uuid,
               key, old_value #>> '{{}}' AS old_value, new_value #>> '{{}}' AS new_value, note
        FROM setting_history
        WHERE {" AND ".join(where_settings)}
        ORDER BY at DESC
        LIMIT :limit OFFSET :offset
    """  # noqa: S608 - clauses are fixed literals; every value is bound
    rows = (await session.execute(text(sql), params)).mappings().all()
    has_more = len(rows) > limit

    actions = (
        await session.execute(
            text(
                "SELECT DISTINCT action FROM audit_events "
                "UNION SELECT 'SETTING_CHANGED' ORDER BY 1"
            )
        )
    ).scalars().all()

    return templates.TemplateResponse(
        request,
        "audit.html",
        await _shell(
            request,
            session,
            user,
            "audit",
            entries=[dict(r) for r in rows[:limit]],
            actions=list(actions),
            selected_action=action,
            selected_actor=actor,
            page_limit=limit,
            page_offset=offset,
            has_more=has_more,
            query_string=urlencode(
                {k: v for k, v in request.query_params.items() if v and k != "offset"},
                doseq=True,
            ),
        ),
    )


@router.get("/admin/users")
async def admin_users(
    user: CurrentUser,
    error: str | None = None,
    saved: str | None = None,
) -> Response:
    """Moved into the settings rail; kept as a redirect.

    Who may sign in is configuration, so it sits with the sign-in sources it
    depends on rather than beside them in the main menu. This stays because
    every one of the dozen POST handlers below redirects here afterwards, and
    because the address has been handed round -- one place to forward from is
    better than a dozen places to edit and one to forget.
    """
    if Permission.USERS_MANAGE not in permissions_for(user.role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "your role may not manage users")
    return RedirectResponse(
        _moved_to(USERS_SECTION, saved=saved, error=error),
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _moved_to(section: str, *, saved: str | None, error: str | None) -> str:
    """The settings address for a section that used to be a page."""
    params = {"section": _section_slug(section)}
    if saved:
        params["saved"] = saved
    if error:
        params["error"] = error
    return f"/admin/settings?{urlencode(params)}"


async def _users_pane(
    request: Request,
    session: AsyncSession,
    user: User,
    *,
    error: str | None = None,
    saved: str | None = None,
) -> dict[str, Any]:
    """The users pane: the list, plus everything the add form has to offer.

    Returns the pane's own context and not a rendered shell, because it is now
    one section of the settings page rather than a page of its own.
    """
    stmt = select(User).order_by(User.email)
    if not user.is_super_admin:
        stmt = stmt.where(User.brand_id == user.brand_id)
    users = list((await session.execute(stmt)).scalars().all())

    # Which organisations this administrator may place someone into. A platform
    # admin sees all of them; an organisation admin can only ever add people to
    # their own, which is the isolation rule showing up in the UI.
    brands = await selectable_brands(session, user)

    # Roles this administrator may hand out. Only a platform admin can create
    # another platform admin -- otherwise an organisation admin could promote
    # themselves out of their own organisation, and the boundary would be
    # advisory.
    assignable = [Role.ADMIN, Role.OPERATOR]
    if user.is_super_admin:
        assignable = [Role.SUPER_ADMIN, *assignable]

    # Per-brand: every one of these is brand-overridable, and reading them
    # globally meant a brand that had turned Active Directory on saw no picker,
    # because the global row was still false.
    _, active = await _brand_scope(request, session, user)
    directory_brand = active.id if active else None
    directory = {
        "entra": await settings_service.get_bool(
            session, "auth.oidc_entra_enabled", brand_id=directory_brand
        ),
        "google": await settings_service.get_bool(
            session, "auth.oidc_google_enabled", brand_id=directory_brand
        ),
        "ldap": await settings_service.get_bool(
            session, "ldap.enabled", brand_id=directory_brand
        ),
    }
    directory["any"] = any(directory.values())

    memberships: dict[int, list[Any]] = {}
    if users:
        rows = (
            await session.execute(
                select(UserBrand.user_id, UserBrand.role, Brand.name)
                .join(Brand, Brand.id == UserBrand.brand_id)
                .where(UserBrand.user_id.in_([u.id for u in users]))
                .order_by(Brand.name)
            )
        ).all()
        for uid, role, brand_name in rows:
            memberships.setdefault(uid, []).append({"brand": brand_name, "role": Role(role)})

    return {
        "users": users,
        "user_brands": brands,
        "assignable_roles": assignable,
        "memberships": memberships,
        "directory": directory,
        "min_password_length": await settings_service.get_int(
            session, "auth.password_min_length"
        ),
        "users_error": error,
        "users_saved": saved,
    }


async def _audit_scope(request: Request, session: AsyncSession, user: User) -> int | None:
    """The brand an audit row must be written under.

    Not the target's brand: ``audit_events`` carries the same forced RLS as
    everything else, and its WITH CHECK rejects a row for any brand other than
    the one the session is scoped to. A platform administrator acting from a
    different organisation would otherwise fail to write the very row that
    records what they did.
    """
    _, active = await _brand_scope(request, session, user)
    return active.id if active else None


def _assert_may_manage(actor: User) -> None:
    if Permission.USERS_MANAGE not in permissions_for(actor.role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "your role may not manage users")


def _assert_may_assign(actor: User, role: Role, brand_id: int | None) -> None:
    """Guard the two escalations this form could otherwise allow.

    Both are the same mistake -- trusting a value that arrived in a POST body
    because the page that renders the form only offered safe ones. The form is
    not the control.
    """
    if role == Role.SUPER_ADMIN and not actor.is_super_admin:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "only a platform administrator can create another platform administrator",
        )
    if not actor.is_super_admin and brand_id != actor.brand_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "you can only add users to your own organisation"
        )


async def _refuse(
    request: Request,
    session: AsyncSession,
    actor: User,
    action: AdminAction,
    reason: str,
    *,
    where: str,
    target: User | None = None,
    detail: dict[str, Any] | None = None,
) -> Response:
    """Record a refused administrative action, then redirect with the message.

    An attempt that was blocked is exactly what an audit trail is for: "who
    tried to create an account for this address" is a question the successes
    alone cannot answer. Recorded with ``result='DENIED'``, which is the
    vocabulary media refusals already use, so the change log's own filter
    treats them the same way.
    """
    body = dict(detail or {})
    body["reason"] = reason
    await record_admin_event(
        session,
        actor=actor,
        action=action,
        brand_id=await _audit_scope(request, session, actor),
        target=target,
        result="DENIED",
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
        detail=body,
    )
    joiner = "&" if "?" in where else "?"
    return RedirectResponse(
        f"{where}{joiner}error={quote_plus(reason)}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


async def _directory_source_enabled(
    session: AsyncSession, source: AuthSource, actor: User, request: Request
) -> bool:
    """Whether the directory an account is being created against is configured.

    Checked server-side as well as in the form: the menu disables the options
    that are not set up, but a disabled option is a suggestion, not a control,
    and an account nobody can ever sign in to is a support call.
    """
    _, active = await _brand_scope(request, session, actor)
    brand_id = active.id if active else None
    key = {
        AuthSource.LDAP: "ldap.enabled",
        AuthSource.ENTRA: "auth.oidc_entra_enabled",
        AuthSource.GOOGLE: "auth.oidc_google_enabled",
    }.get(source)
    if key is None:
        return False
    return await settings_service.get_bool(session, key, brand_id=brand_id)


@router.post("/admin/users")
async def add_user(
    request: Request,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    """Create an account.

    There was no way to do this from the console at all -- the page listed
    people and offered nothing else, so the only route in was the CLI.
    """
    _assert_may_manage(user)
    form = await request.form()

    email = str(form.get("email") or "").strip().lower()
    display_name = str(form.get("display_name") or "").strip() or None
    role_raw = str(form.get("role") or "")
    source_raw = str(form.get("auth_source") or "LOCAL")
    # Several, not one: the same person routinely handles calls for more than
    # one of these companies.
    brand_raws = [str(v) for v in form.getlist("brand_ids") if str(v).strip()]
    password = str(form.get("password") or "")
    again = str(form.get("password_again") or "")
    directory_dn = str(form.get("directory_dn") or "").strip()

    async def back(message: str) -> Response:
        return await _refuse(
            request, session, user, AdminAction.USER_CREATED, message,
            where="/admin/users", detail={"attempted_email": email},
        )

    if "@" not in email or len(email) < 3:
        return await back("That does not look like an email address")
    try:
        role = Role(role_raw)
    except ValueError:
        return await back("Choose a role")
    try:
        source = AuthSource(source_raw)
    except ValueError:
        return await back("Choose how this person signs in")

    # A platform admin spans every organisation and so belongs to none; any
    # other role must land in at least one.
    brand_ids: list[int] = []
    brand_id: int | None = None
    if role != Role.SUPER_ADMIN:
        if not brand_raws:
            return await back("Tick at least one organisation")
        allowed = {b.id for b in await selectable_brands(session, user)}
        try:
            brand_ids = [int(v) for v in brand_raws]
        except ValueError:
            return await back("That is not an organisation you can add users to")
        if not set(brand_ids) <= allowed:
            return await back("That is not an organisation you can add users to")
        # The first ticked is where they land after signing in; the rest are
        # theirs to switch to.
        brand_id = brand_ids[0]

    _assert_may_assign(user, role, brand_id)

    existing = (
        await session.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    if existing is not None:
        return await back(f"{email} already has an account")

    password_hash = None
    if source == AuthSource.LOCAL:
        if password != again:
            return await back("The two passwords do not match")
        try:
            password_hash = hash_password(password)
        except AuthError as exc:
            return await back(str(exc))
    elif not await _directory_source_enabled(session, source, user, request):
        names = {
            AuthSource.LDAP: "Active Directory / LDAP",
            AuthSource.ENTRA: "Microsoft Entra ID",
            AuthSource.GOOGLE: "Google Workspace",
        }
        return await back(
            f"{names.get(source, str(source))} sign-in is not configured, so an "
            "account cannot be created against it yet"
        )

    row = User(
        brand_id=brand_id,
        email=email,
        display_name=display_name or email,
        role=role,
        auth_source=source,
        password_hash=password_hash,
        # A password an administrator typed is a password an administrator
        # knows, so it is a one-time value and must be replaced on first use.
        must_change_password=source == AuthSource.LOCAL,
        # The directory's own identifier, when the account was picked from it.
        # Matching on that rather than on an email address survives somebody
        # changing their name. Empty until their first sign-in otherwise.
        oidc_subject=directory_dn or None,
        is_active=True,
    )
    session.add(row)
    await session.flush()
    # One membership per organisation ticked, all with the role chosen above.
    # The role lives on the pairing, so it can be changed per organisation
    # afterwards on the person's own page.
    for member_brand in brand_ids:
        session.add(UserBrand(user_id=row.id, brand_id=member_brand, role=role))
    if brand_ids:
        await session.flush()

    log.info(
        "users.created", actor_id=user.id, user_id=row.id, role=str(role), source=str(source)
    )
    await record_admin_event(
        session,
        actor=user,
        action=AdminAction.USER_CREATED,
        brand_id=await _audit_scope(request, session, user),
        target=row,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
        detail={
            "organisation_ids": brand_ids,
            "home_organisation_id": brand_id,
            "signs_in_with": str(source),
        },
    )
    return RedirectResponse(
        f"/admin/users?saved={quote_plus(email)}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/admin/users/directory", response_class=HTMLResponse)
async def browse_directory(
    request: Request,
    user: CurrentUser,
    session: ScopedSession,
    kind: str = "user",
    q: str = "",
) -> Response:
    """Search Active Directory for people, groups or OUs.

    A fragment for htmx rather than JSON: the results are a list with a button
    on each row, which is markup, and rendering it here keeps the escaping and
    the empty state in one place instead of in a script.

    Returns 200 with a message on failure rather than an error status, because
    "the domain controller refused the reading account" is information the
    administrator needs to see in the page, not a stack trace in a console.
    """
    _assert_may_manage(user)
    try:
        entry_kind = directory.EntryKind(kind)
    except ValueError:
        entry_kind = directory.EntryKind.USER

    _, active = await _brand_scope(request, session, user)
    brand_id = active.id if active else None
    config = await directory.load_config(session, brand_id=brand_id)
    ldap_on = await settings_service.get_bool(session, "ldap.enabled", brand_id=brand_id)
    entra_on = await settings_service.get_bool(
        session, "auth.oidc_entra_enabled", brand_id=brand_id
    )
    google_on = await settings_service.get_bool(
        session, "auth.oidc_google_enabled", brand_id=brand_id
    )

    if ldap_on:
        result = await directory.search(config, entry_kind, q)
    elif entra_on or google_on:
        # Being honest about the gap rather than returning an empty list that
        # looks like "nobody matches". Entra and Google are wired for *sign-in*;
        # reading their user lists needs Microsoft Graph or the Google Admin
        # SDK, which is a separate integration and is not built.
        which = " and ".join(
            n for n, on in (("Microsoft Entra ID", entra_on), ("Google Workspace", google_on)) if on
        )
        result = directory.DirectoryResult(
            error=f"{which} is set up for signing in, but its directory cannot be "
                  "searched from here yet -- that needs Microsoft Graph or the Google "
                  "Admin API, which is not built. Type the address in by hand; it is "
                  "matched at their first sign-in."
        )
    else:
        result = directory.DirectoryResult(
            error="No directory is connected. Turn on Active Directory under "
                  "Settings to search it, or type the address in by hand."
        )

    return templates.TemplateResponse(
        request,
        "_directory_results.html",
        {"request": request, "result": result, "kind": entry_kind.value, "term": q},
    )


@router.post("/admin/users/{ident}/active")
async def set_user_active(
    request: Request,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    ident: str,
) -> Response:
    """Enable or disable an account.

    Disabling revokes every session, so it takes effect on the next request
    rather than whenever a cookie would have expired -- the reason sessions are
    rows here in the first place.
    """
    _assert_may_manage(user)
    form = await request.form()
    active = str(form.get("active") or "") == "1"

    row = await _managed_target(session, user, ident)
    if row.id == user.id:
        return RedirectResponse(
            "/admin/users?error=You+cannot+disable+your+own+account",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    # The last platform admin standing must not be switched off: there would be
    # nobody left who can switch anyone back on.
    if row.is_super_admin and not active:
        remaining = (
            await session.execute(
                select(func.count())
                .select_from(User)
                .where(
                    User.role == Role.SUPER_ADMIN,
                    User.is_active.is_(True),
                    User.id != row.id,
                )
            )
        ).scalar_one()
        if remaining == 0:
            return RedirectResponse(
                "/admin/users?error=" + quote_plus(
                    "That is the only active platform administrator -- add another first"
                ),
                status_code=status.HTTP_303_SEE_OTHER,
            )

    row.is_active = active
    if not active:
        await revoke_all_sessions(session, row.id)
    await session.flush()
    log.info("users.active", actor_id=user.id, user_id=row.id, active=active)
    await record_admin_event(
        session,
        actor=user,
        action=AdminAction.USER_ENABLED if active else AdminAction.USER_DISABLED,
        brand_id=await _audit_scope(request, session, user),
        target=row,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
        detail={"sessions_revoked": not active},
    )
    return RedirectResponse("/admin/users", status_code=status.HTTP_303_SEE_OTHER)


async def _managed_target(
    session: AsyncSession, actor: User, ident: str
) -> User:
    """The account being administered, or a refusal.

    ``ident`` is an email address -- ``/admin/users/someone@example.com`` --
    because a URL that says who it is about is one you can read, paste into a
    ticket and recognise later; ``/admin/users/3`` is only meaningful to the
    database. A numeric segment is still accepted so older links keep working.

    One place, because every one of the routes below has to make the same two
    checks and getting either wrong is a privilege escalation: an organisation
    admin may only touch people in their own organisation, and only a platform
    admin may touch another platform admin.
    """
    ident = str(ident or "").strip()
    if ident.isdigit():
        clause = User.id == int(ident)
    else:
        clause = User.email == ident.lower()
    row = (await session.execute(select(User).where(clause))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such person")
    if not actor.is_super_admin and row.brand_id != actor.brand_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "not someone in your organisation"
        )
    if row.is_super_admin and not actor.is_super_admin:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "only a platform administrator can administer another one",
        )
    return row


async def _last_active_platform_admin(session: AsyncSession, excluding: int) -> bool:
    """Whether removing this account would leave nobody able to administer.

    Checked before demoting, disabling or deleting a platform admin: there
    would be no one left who could put it back.
    """
    remaining = (
        await session.execute(
            select(func.count())
            .select_from(User)
            .where(
                User.role == Role.SUPER_ADMIN,
                User.is_active.is_(True),
                User.id != excluding,
            )
        )
    ).scalar_one()
    return remaining == 0


@router.get("/admin/users/{ident}", response_class=HTMLResponse)
async def admin_user_detail(
    request: Request,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    ident: str,
    error: str | None = None,
    saved: str | None = None,
) -> Response:
    """One person: their role, and which organisations they work in.

    The list page could create an account and nothing else, so somebody who
    needed access to a second organisation, or the wrong role fixing, could
    only be edited in the database.
    """
    _assert_may_manage(user)
    target = await _managed_target(session, user, ident)

    memberships = (
        await session.execute(
            select(UserBrand.brand_id, UserBrand.role, Brand.name)
            .join(Brand, Brand.id == UserBrand.brand_id)
            .where(UserBrand.user_id == target.id)
            .order_by(Brand.name)
        )
    ).all()
    held = {row[0] for row in memberships}

    reachable = await selectable_brands(session, user)
    assignable = [Role.ADMIN, Role.OPERATOR]
    if user.is_super_admin:
        assignable = [Role.SUPER_ADMIN, *assignable]

    return templates.TemplateResponse(
        request,
        "user_detail.html",
        await _shell(
            request,
            session,
            user,
            "users",
            target=target,
            memberships=[
                {"brand_id": b, "role": Role(r), "brand": n} for b, r, n in memberships
            ],
            addable=[b for b in reachable if b.id not in held],
            assignable_roles=assignable,
            membership_roles=[Role.ADMIN, Role.OPERATOR],
            is_self=target.id == user.id,
            min_password_length=await settings_service.get_int(
                session, "auth.password_min_length"
            ),
            user_error=error,
            user_saved=saved,
        ),
    )


@router.post("/admin/users/{ident}/details")
async def change_user_details(
    request: Request,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    ident: str,
) -> Response:
    """Correct a name, an address, or where to message somebody.

    The email address is the account's identity: it is what a directory match
    is made on and what appears against everything the person did. Changing it
    is legitimate -- people mistype them, and people get married -- but it is
    checked for collisions and recorded, and the audit trail keeps the old one
    because entries written under it are still theirs.
    """
    _assert_may_manage(user)
    target = await _managed_target(session, user, ident)
    form = await request.form()

    def back(message: str) -> Response:
        return RedirectResponse(
            f"/admin/users/{quote(ident)}?error={quote_plus(message)}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    email = str(form.get("email") or "").strip().lower()
    display_name = str(form.get("display_name") or "").strip() or None
    telegram = str(form.get("telegram_chat_id") or "").strip() or None
    slack = str(form.get("slack_user_id") or "").strip() or None

    if "@" not in email or len(email) < 3:
        return back("That does not look like an email address")
    if email != target.email:
        clash = (
            await session.execute(select(User).where(User.email == email))
        ).scalar_one_or_none()
        if clash is not None:
            return back(f"{email} already belongs to another account")

    changes: dict[str, Any] = {}
    for field, value in (
        ("email", email),
        ("display_name", display_name),
        ("telegram_chat_id", telegram),
        ("slack_user_id", slack),
    ):
        if getattr(target, field) != value:
            changes[field] = {"from": getattr(target, field), "to": value}
            setattr(target, field, value)
    if not changes:
        return RedirectResponse(
            f"/admin/users/{quote(ident)}", status_code=status.HTTP_303_SEE_OTHER
        )
    await session.flush()

    await record_admin_event(
        session,
        actor=user,
        action=AdminAction.USER_DETAILS_CHANGED,
        brand_id=await _audit_scope(request, session, user),
        target=target,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
        detail={"changed": changes},
    )
    # Redirected to the address it is now, not the one it was, or the next page
    # load would 404 on an account that was just renamed.
    return RedirectResponse(
        f"/admin/users/{quote(target.email)}?saved=details",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/admin/users/{ident}/password")
async def reset_user_password(
    request: Request,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    ident: str,
) -> Response:
    """Set a new password for somebody who has lost theirs.

    The current one is not asked for -- the point is that nobody has it. What
    that costs is that an administrator now knows the password, so it is a
    one-time value: ``must_change_password`` is set and every session ends, so
    the person has to replace it before they can do anything.
    """
    _assert_may_manage(user)
    target = await _managed_target(session, user, ident)
    form = await request.form()

    def back(message: str) -> Response:
        return RedirectResponse(
            f"/admin/users/{quote(ident)}?error={quote_plus(message)}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    if target.auth_source != AuthSource.LOCAL:
        return back(
            "This account signs in through a directory, so its password is not kept here"
        )
    new = str(form.get("password") or "")
    again = str(form.get("password_again") or "")
    if new != again:
        return back("The two passwords do not match")
    try:
        target.password_hash = hash_password(new)
    except AuthError as exc:
        return back(str(exc))

    target.must_change_password = True
    target.failed_logins = 0
    target.locked_until = None
    await revoke_all_sessions(session, target.id)
    await session.flush()

    await record_admin_event(
        session,
        actor=user,
        action=AdminAction.PASSWORD_RESET_BY_ADMIN,
        brand_id=await _audit_scope(request, session, user),
        target=target,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
        detail={"must_change_at_next_sign_in": True, "sessions_revoked": True},
    )
    log.info("users.password_reset", actor_id=user.id, user_id=target.id)
    return RedirectResponse(
        f"/admin/users/{quote(ident)}?saved=password",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/admin/users/{ident}/role")
async def change_user_role(
    request: Request,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    ident: str,
) -> Response:
    """Change what someone is, platform-wide."""
    _assert_may_manage(user)
    target = await _managed_target(session, user, ident)
    form = await request.form()

    def back(message: str) -> Response:
        return RedirectResponse(
            f"/admin/users/{quote(ident)}?error={quote_plus(message)}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    try:
        role = Role(str(form.get("role") or ""))
    except ValueError:
        return back("Choose a role")
    if role == target.role:
        return RedirectResponse(
            f"/admin/users/{quote(ident)}", status_code=status.HTTP_303_SEE_OTHER
        )

    _assert_may_assign(user, role, target.brand_id)
    if target.id == user.id:
        return back("You cannot change your own role")
    if (
        target.is_super_admin
        and role != Role.SUPER_ADMIN
        and await _last_active_platform_admin(session, target.id)
    ):
        return back(
            "That is the only active platform administrator -- promote somebody else first"
        )

    was, target.role = target.role, role
    # A platform admin spans every organisation and belongs to none; anyone
    # else has to land somewhere, so keep the home organisation consistent
    # with the role rather than leaving a contradiction.
    if role == Role.SUPER_ADMIN:
        target.brand_id = None
    elif target.brand_id is None:
        brands = await selectable_brands(session, user)
        if not brands:
            return back("There is no organisation to put them in")
        target.brand_id = brands[0].id
    await session.flush()

    await record_admin_event(
        session,
        actor=user,
        action=AdminAction.USER_ROLE_CHANGED,
        brand_id=await _audit_scope(request, session, user),
        target=target,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
        detail={"from": str(was), "to": str(role)},
    )
    return RedirectResponse(
        f"/admin/users/{quote(ident)}?saved=role", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/admin/users/{ident}/brands")
async def add_user_brand(
    request: Request,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    ident: str,
) -> Response:
    """Give someone access to another organisation, with its own role.

    The role sits on the pairing, not the person: the same operator can be an
    admin for one of these companies and an operator for the other, and a
    single `users.role` cannot say that.
    """
    _assert_may_manage(user)
    target = await _managed_target(session, user, ident)
    form = await request.form()

    def back(message: str) -> Response:
        return RedirectResponse(
            f"/admin/users/{quote(ident)}?error={quote_plus(message)}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    try:
        brand_id = int(str(form.get("brand_id") or ""))
        role = Role(str(form.get("role") or ""))
    except ValueError:
        return back("Choose an organisation and a role")
    if role == Role.SUPER_ADMIN:
        return back("A platform administrator already reaches every organisation")

    allowed = {b.id for b in await selectable_brands(session, user)}
    if brand_id not in allowed:
        return back("That is not an organisation you can grant access to")

    already = (
        await session.execute(
            select(UserBrand).where(
                UserBrand.user_id == target.id, UserBrand.brand_id == brand_id
            )
        )
    ).scalar_one_or_none()
    if already is not None:
        already.role = role
    else:
        session.add(UserBrand(user_id=target.id, brand_id=brand_id, role=role))
    await session.flush()

    await record_admin_event(
        session,
        actor=user,
        action=AdminAction.USER_BRAND_ADDED,
        brand_id=await _audit_scope(request, session, user),
        target=target,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
        detail={"organisation_id": brand_id, "role": str(role)},
    )
    return RedirectResponse(
        f"/admin/users/{quote(ident)}?saved=access", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/admin/users/{ident}/brands/{brand_id}/remove")
async def remove_user_brand(
    request: Request,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    ident: str,
    brand_id: int,
) -> Response:
    _assert_may_manage(user)
    target = await _managed_target(session, user, ident)

    allowed = {b.id for b in await selectable_brands(session, user)}
    if brand_id not in allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not your organisation to revoke")

    await session.execute(
        delete(UserBrand).where(
            UserBrand.user_id == target.id, UserBrand.brand_id == brand_id
        )
    )
    # Losing access has to take effect now, not when a cookie expires.
    await revoke_all_sessions(session, target.id)
    await session.flush()

    await record_admin_event(
        session,
        actor=user,
        action=AdminAction.USER_BRAND_REMOVED,
        brand_id=await _audit_scope(request, session, user),
        target=target,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
        detail={"organisation_id": brand_id, "sessions_revoked": True},
    )
    return RedirectResponse(
        f"/admin/users/{quote(ident)}?saved=access", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/admin/users/{ident}/delete")
async def delete_user(
    request: Request,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    ident: str,
) -> Response:
    """Remove an account for good.

    Their sessions and organisation access go with them, by cascade. Their
    audit trail does not: `audit_events` holds no foreign key to `users` and
    keeps the email as text, so what somebody did survives their account being
    deleted. That is the point of an audit trail.

    Disabling is almost always the better answer, and the page says so -- which
    is why this asks for the address to be typed rather than offering a button
    next to "Disable".
    """
    _assert_may_manage(user)
    target = await _managed_target(session, user, ident)
    form = await request.form()
    typed = str(form.get("confirm_email") or "").strip().lower()

    def back(message: str) -> Response:
        return RedirectResponse(
            f"/admin/users/{quote(ident)}?error={quote_plus(message)}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    if target.id == user.id:
        return back("You cannot delete your own account")
    if typed != target.email.lower():
        return back("Type the address exactly to confirm the deletion")
    if target.is_super_admin and await _last_active_platform_admin(session, target.id):
        return back(
            "That is the only active platform administrator -- there would be nobody "
            "left who could undo this"
        )

    email, role = target.email, str(target.role)
    # Recorded before the row goes, so the trail is written whatever happens
    # next; the actor is what matters here and the actor is not being deleted.
    await record_admin_event(
        session,
        actor=user,
        action=AdminAction.USER_DELETED,
        brand_id=await _audit_scope(request, session, user),
        target=target,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
        detail={"role": role},
    )
    await revoke_all_sessions(session, target.id)
    await session.execute(delete(User).where(User.id == target.id))
    await session.flush()

    log.info("users.deleted", actor_id=user.id, deleted_email=email, role=role)
    return RedirectResponse(
        f"/admin/users?saved={quote_plus(email + ' deleted')}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/admin/users/{ident}/2fa/reset")
async def reset_user_2fa(
    request: Request,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    ident: str,
) -> Response:
    """Clear someone's second factor after a lost phone.

    Platform administrators only, and never on yourself -- clearing your own
    from a live session would make the factor optional for the one account that
    most needs it.
    """
    _assert_may_manage(user)
    row = await _managed_target(session, user, ident)
    if not mfa.can_reset_for(user, row):
        return RedirectResponse(
            "/admin/users?error=" + quote_plus(
                "Only a platform administrator can clear someone else's second factor, "
                "and not their own"
            ),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    row.totp_secret_sealed = None
    row.totp_enrolled_at = None
    row.totp_last_counter = None
    row.totp_recovery_hashes = []
    await revoke_all_sessions(session, row.id)
    await session.flush()
    log.info("mfa.reset_by_admin", actor_id=user.id, user_id=row.id)
    # The most abusable action here: it removes a factor from an account the
    # actor does not own, so it is the one most worth keeping for ever.
    await record_admin_event(
        session,
        actor=user,
        action=AdminAction.MFA_RESET_BY_ADMIN,
        brand_id=await _audit_scope(request, session, user),
        target=row,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return RedirectResponse(
        "/admin/users?saved=" + quote_plus(f"second factor cleared for {row.email}"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


# --------------------------------------------------------------- organisations


def _slugify(value: str) -> str:
    """A short, URL-safe handle derived from the name.

    Derived rather than typed: the slug is only ever used in URLs and CLI
    arguments, and asking somebody to invent one is asking them to get it
    wrong. It stays editable for the case where two companies would collide.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return slug[:60] or "org"


@router.get("/admin/organisations")
async def admin_organisations(
    user: CurrentUser,
    error: str | None = None,
    saved: str | None = None,
) -> Response:
    """Moved into the settings rail; kept as a redirect.

    An organisation is the top-level thing every other setting hangs off, so
    it belongs with the settings rather than beside them in the main menu.
    The address stays because the create/rename/tenant handlers all redirect
    here afterwards.
    """
    if Permission.BRANDS_MANAGE not in permissions_for(user.role):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "only a platform administrator can manage organisations",
        )
    return RedirectResponse(
        _moved_to(ORGANISATIONS_SECTION, saved=saved, error=error),
        status_code=status.HTTP_303_SEE_OTHER,
    )


async def _organisations_pane(session: AsyncSession) -> dict[str, Any]:
    """The organisations pane: each company, its PBXes and what it holds.

    Creating one used to be `c2w-admin brand add` and nothing else, so the one
    thing you cannot do without -- an organisation to put anything in -- was
    the one thing the console could not do.

    **Every organisation's real figures, whatever brand the profile has
    selected.** This page runs on the ordinary request session, which carries
    forced RLS scoped to the *active* brand -- so it used to show the selected
    organisation correctly and every other one as zeros with no PBXes, which
    reads as "InterMagnum has nothing in it" rather than "you are looking at
    Go4Rex". A platform administrator asking to see the organisations is asking
    across all of them; that is what the section is.

    The scope is therefore moved per organisation and restored, rather than
    reaching for the BYPASSRLS role: this is a user request, RLS stays the
    control, and one extra round trip per organisation is nothing next to
    quietly wrong numbers. `record_admin_event` does the same thing for the
    same reason. `set_config(..., true)` is transaction-local, so the restore
    matters only within this transaction -- but it matters, because the caller
    goes on to read settings for the brand the operator actually chose.
    """
    brands = (await session.execute(select(Brand).order_by(Brand.name))).scalars().all()

    was = (
        await session.execute(text("SELECT current_setting('c2w.brand_id', true)"))
    ).scalar_one() or ""

    counts: dict[int, dict[str, Any]] = {}
    by_brand: dict[int, list[Any]] = {}
    try:
        for brand in brands:
            await session.execute(
                text("SELECT set_config('c2w.brand_id', :b, true)"), {"b": str(brand.id)}
            )
            row = (
                await session.execute(
                    text(
                        """
                        SELECT (SELECT count(*) FROM tenants)              AS tenants,
                               (SELECT count(*) FROM commpeak_connections) AS accounts,
                               (SELECT count(*) FROM storage_destinations) AS archives,
                               -- Both routes into an organisation: the home
                               -- brand on the account, and a membership in
                               -- user_brands for somebody who works across
                               -- several. Counting only the column missed
                               -- every multi-organisation user.
                               (SELECT count(*) FROM (
                                    SELECT id FROM users WHERE brand_id = :b
                                    UNION
                                    SELECT user_id FROM user_brands WHERE brand_id = :b
                               ) AS m)                                     AS people
                        """
                    ),
                    {"b": brand.id},
                )
            ).mappings().one()
            counts[brand.id] = {"id": brand.id, **dict(row)}

            for tenant in (
                await session.execute(
                    text(
                        "SELECT id, brand_id, name, slug, commpeak_domain FROM tenants "
                        "ORDER BY name"
                    )
                )
            ).mappings().all():
                by_brand.setdefault(tenant["brand_id"], []).append(dict(tenant))
    finally:
        await session.execute(
            text("SELECT set_config('c2w.brand_id', :b, true)"), {"b": was}
        )

    return {
        "organisations": brands,
        "counts": counts,
        "tenants": by_brand,
        "timezone_choices": SETTINGS["org.timezone"].choices,
    }


@router.post("/admin/organisations")
async def add_organisation(
    request: Request,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    """Create an organisation, its encryption key and its table partitions.

    All in one transaction: an organisation without partitions has nowhere to
    put a call, and finding that out later means a failed insert inside a
    worker rather than a clear message here.
    """
    if Permission.BRANDS_MANAGE not in permissions_for(user.role):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "only a platform administrator can create an organisation",
        )

    form = await request.form()
    name = str(form.get("name") or "").strip()
    slug = _slugify(str(form.get("slug") or "") or name)
    timezone = str(form.get("timezone") or "").strip()

    def back(message: str) -> Response:
        return RedirectResponse(
            f"/admin/organisations?error={quote_plus(message)}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    if len(name) < 2:
        return back("Give the company a name")
    existing = (
        await session.execute(select(Brand).where(Brand.slug == slug))
    ).scalar_one_or_none()
    if existing is not None:
        return back(f"The handle {slug!r} is already used by {existing.name}")

    key_id, wrapped = generate_data_key()
    brand = Brand(
        name=name, slug=slug, encryption_key_id=key_id, encryption_key_wrapped=wrapped
    )
    session.add(brand)
    await session.flush()

    # Everything below inserts brand-scoped rows -- the retention policy, the
    # settings, the audit entry -- and this session is unscoped because the
    # page works across organisations. Without the scope, RLS rejects each one
    # in turn.
    await session.execute(
        text("SELECT set_config('c2w.brand_id', :b, true)"), {"b": str(brand.id)}
    )

    # Every brand-partitioned table. Miss one and the first insert into it
    # fails with "no partition of relation found" -- in a worker, at 3am.
    for table in ("cdrs", "recordings", "sms_messages"):
        await session.execute(
            text(
                f"CREATE TABLE IF NOT EXISTS {table}_brand_{brand.id} "
                f"PARTITION OF {table} FOR VALUES IN ({brand.id})"
            )
        )
    await session.execute(
        text(
            "INSERT INTO retention_policies (brand_id) VALUES (:b) "
            "ON CONFLICT (brand_id) DO NOTHING"
        ),
        {"b": brand.id},
    )
    if timezone:
        await settings_service.set(
            session, "org.timezone", timezone, brand_id=brand.id, changed_by=user.email
        )
    await settings_service.set(
        session, "org.display_name", name, brand_id=brand.id, changed_by=user.email
    )
    await session.flush()

    log.info("brands.created", actor_id=user.id, brand_id=brand.id, slug=slug)
    await record_admin_event(
        session,
        actor=user,
        action=AdminAction.ORGANISATION_CREATED,
        brand_id=brand.id,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
        detail={"name": name, "handle": slug},
    )
    return RedirectResponse(
        f"/admin/organisations?saved={quote_plus(name)}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/admin/organisations/{brand_id}/rename")
async def rename_organisation(
    request: Request,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    brand_id: int,
) -> Response:
    """Change the display name.

    The handle is left alone deliberately: it is in URLs, in systemd
    invocations and in the archive's object keys, so renaming it would orphan
    things that already point at it.
    """
    if Permission.BRANDS_MANAGE not in permissions_for(user.role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not your call to make")

    form = await request.form()
    name = str(form.get("name") or "").strip()
    brand = (
        await session.execute(select(Brand).where(Brand.id == brand_id))
    ).scalar_one_or_none()
    if brand is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such organisation")
    if len(name) < 2:
        return RedirectResponse(
            "/admin/organisations?error=Give+the+company+a+name",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    was, brand.name = brand.name, name
    await settings_service.set(
        session, "org.display_name", name, brand_id=brand.id, changed_by=user.email
    )
    await session.flush()
    await record_admin_event(
        session,
        actor=user,
        action=AdminAction.ORGANISATION_RENAMED,
        brand_id=brand.id,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
        detail={"from": was, "to": name},
    )
    return RedirectResponse(
        f"/admin/organisations?saved={quote_plus(name)}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/admin/organisations/{brand_id}/tenants")
async def add_tenant(
    request: Request,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    brand_id: int,
) -> Response:
    """Add a PBX or dialer domain under an organisation.

    A tenant is one CommPeak domain. An organisation has as many as it has
    PBXes, and a CommPeak account is registered against one of them.
    """
    if Permission.BRANDS_MANAGE not in permissions_for(user.role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not your call to make")

    form = await request.form()
    name = str(form.get("name") or "").strip()
    domain = str(form.get("commpeak_domain") or "").strip() or None
    slug = _slugify(str(form.get("slug") or "") or name)

    brand = (
        await session.execute(select(Brand).where(Brand.id == brand_id))
    ).scalar_one_or_none()
    if brand is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such organisation")
    if len(name) < 2:
        return RedirectResponse(
            "/admin/organisations?error=Give+the+PBX+a+name",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    # tenants is brand-scoped, so the insert needs the scope set or RLS
    # rejects it.
    await session.execute(
        text("SELECT set_config('c2w.brand_id', :b, true)"), {"b": str(brand_id)}
    )
    session.add(Tenant(brand_id=brand_id, name=name, slug=slug, commpeak_domain=domain))
    await session.flush()
    await record_admin_event(
        session,
        actor=user,
        action=AdminAction.TENANT_CREATED,
        brand_id=brand_id,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
        detail={"name": name, "handle": slug, "commpeak_domain": domain},
    )
    return RedirectResponse(
        f"/admin/organisations?saved={quote_plus(name)}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# -------------------------------------------------------------------- messages


def _parse_message_query(
    *,
    number: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    direction: str | None = None,
    status: str | None = None,
    country: str | None = None,
    stream: str | None = None,
    campaign: str | None = None,
    body: str | None = None,
    sort: str = "occurred",
    desc: str = "1",
    limit: int = 50,
    offset: int = 0,
) -> MessageQuery:
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
        sort_field = MessageSort(sort)
    except ValueError:
        sort_field = MessageSort.OCCURRED

    return MessageQuery(
        date_from=_dt(date_from),
        date_to=_dt(date_to),
        number=number or None,
        direction=direction or None,
        status=status or None,
        country=country or None,
        stream=stream or None,
        campaign=campaign or None,
        body=body or None,
        sort=sort_field,
        descending=desc in ("1", "true", ""),
        limit=limit,
        offset=offset,
    )


async def _messages_context(
    request: Request, session: AsyncSession, query: MessageQuery
) -> dict[str, Any]:
    page = await search_messages(session, query)
    total = await count_messages(session, query)
    any_messages = (
        await session.execute(text("SELECT EXISTS (SELECT 1 FROM sms_messages)"))
    ).scalar_one()
    count_label = f"{total:,}+ messages" if total >= 10_000 else f"{total:,} messages"
    return {
        "page": page,
        "q": query,
        "stats": await message_stats(session),
        "options": await message_filter_options(session),
        "any_messages": any_messages,
        "count_label": count_label,
        "direction_chips": (
            ("", "All"),
            ("out", "Sent"),
            ("in", "Received"),
        ),
        "query_string": urlencode(
            {k: v for k, v in request.query_params.items() if v}, doseq=True
        ),
    }


@router.get("/messages", response_class=HTMLResponse)
async def messages(
    request: Request,
    user: CurrentUser,
    session: ScopedSession,
    number: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    direction: str | None = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    country: str | None = None,
    stream: str | None = None,
    campaign: str | None = None,
    body: str | None = None,
    sort: str = "occurred",
    desc: str = "1",
    limit: int = 50,
    offset: int = 0,
) -> Response:
    """Text messages, both directions, with delivery status and timestamps."""
    query = _parse_message_query(
        number=number, date_from=date_from, date_to=date_to, direction=direction,
        status=status_filter, country=country, stream=stream, campaign=campaign,
        body=body, sort=sort, desc=desc, limit=limit, offset=offset,
    )
    context = await _messages_context(request, session, query)
    return templates.TemplateResponse(
        request, "messages.html", await _shell(request, session, user, "messages", **context)
    )


@router.get("/messages/rows", response_class=HTMLResponse)
async def messages_rows(
    request: Request,
    user: CurrentUser,
    session: ScopedSession,
    number: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    direction: str | None = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    country: str | None = None,
    stream: str | None = None,
    campaign: str | None = None,
    body: str | None = None,
    sort: str = "occurred",
    desc: str = "1",
    limit: int = 50,
    offset: int = 0,
) -> Response:
    """The table only, for htmx to swap in."""
    query = _parse_message_query(
        number=number, date_from=date_from, date_to=date_to, direction=direction,
        status=status_filter, country=country, stream=stream, campaign=campaign,
        body=body, sort=sort, desc=desc, limit=limit, offset=offset,
    )
    context = await _messages_context(request, session, query)
    return templates.TemplateResponse(request, "_messages_rows.html", context)


@router.get("/messages/export.csv")
async def export_messages_csv(
    request: Request,
    user: CurrentUser,
    session: ScopedSession,
    number: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    direction: str | None = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    country: str | None = None,
    stream: str | None = None,
    campaign: str | None = None,
    body: str | None = None,
) -> Response:
    """Stream a CSV of the current filter.

    Same permission as exporting calls: taking a copy of customer conversations
    off the platform is the same act whether it is audio or text.
    """
    if Permission.CDR_EXPORT not in permissions_for(user.role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "your role may not export messages")

    query = _parse_message_query(
        number=number, date_from=date_from, date_to=date_to, direction=direction,
        status=status_filter, country=country, stream=stream, campaign=campaign,
        body=body, limit=200,
    )

    async def rows():
        import csv
        import io

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            [
                "occurred_at", "direction", "status", "sent_at", "delivered_at",
                "received_at", "from", "to", "country", "stream", "campaign",
                "characters", "parts", "cost", "message", "message_uuid",
            ]
        )
        yield buffer.getvalue()
        buffer.seek(0), buffer.truncate(0)

        offset = 0
        while True:
            query.offset = offset
            page = await search_messages(session, query)
            if not page.rows:
                return
            for row in page.rows:
                writer.writerow(
                    [
                        row["occurred_at"].isoformat() if row["occurred_at"] else "",
                        row["direction"] or "",
                        row["status"] or "",
                        row["sent_at"].isoformat() if row["sent_at"] else "",
                        row["delivered_at"].isoformat() if row["delivered_at"] else "",
                        row["received_at"].isoformat() if row["received_at"] else "",
                        row["source_number"] or row["source_name"] or "",
                        row["destination_number"] or "",
                        row["country_name"] or "",
                        row["stream"] or "",
                        row["campaign"] or "",
                        row["message_length"] or "",
                        row["segments"] or "",
                        row["cost"] if row["cost"] is not None else "",
                        row["body"] or "",
                        row["message_uuid"] or "",
                    ]
                )
            yield buffer.getvalue()
            buffer.seek(0), buffer.truncate(0)
            if not page.has_more:
                return
            offset += page.limit

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
    return StreamingResponse(
        rows(),
        media_type="text/csv",
        headers={"content-disposition": f'attachment; filename="messages-{stamp}.csv"'},
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
            font_sizes=FONT_SIZES,
            font_px=_font_px(user.preferences or {}),
            font_families=[(k, v[0]) for k, v in FONT_STACKS.items()],
            font_family=str((user.preferences or {}).get("font_family") or "system"),
            theme_choices=(("auto", "Match the system"), ("light", "Light"), ("dark", "Dark")),
            recovery_left=len(user.totp_recovery_hashes or []),
            mfa_required=await settings_service.get_bool(session, "mfa.require_totp"),
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

    # Size in pixels now. The old percentage is dropped rather than kept in
    # step, because two fields meaning the same thing is how they end up
    # disagreeing.
    try:
        size = int(str(form.get("font_px") or DEFAULT_FONT_PX))
    except ValueError:
        size = DEFAULT_FONT_PX
    prefs["font_px"] = min(max(size, FONT_SIZES[0]), FONT_SIZES[-1])
    prefs.pop("font_scale", None)

    family = str(form.get("font_family") or "system")
    prefs["font_family"] = family if family in FONT_STACKS else "system"

    try:
        volume = int(str(form.get("volume") or 100))
    except ValueError:
        volume = 100
    # 200 rather than 100: above the recording's own level player.js amplifies
    # through a gain node, which is what quiet call audio needs.
    prefs["volume"] = min(max(volume, 0), 200)

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
    from c2w.auth.local import verify_password

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
    await record_admin_event(
        session,
        actor=row,
        action=AdminAction.PASSWORD_CHANGED,
        brand_id=await _audit_scope(request, session, row),
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
        detail={"sessions_revoked": True},
    )
    response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(SESSION_COOKIE)
    return response


# ------------------------------------------------------- my second factor


@router.post("/me/2fa/start")
async def start_my_2fa(
    request: Request,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    row = (await session.execute(select(User).where(User.id == user.id))).scalar_one()
    try:
        await mfa.begin_enrolment(session, row)
    except AuthError as exc:
        return RedirectResponse(
            f"/me?error={quote_plus(str(exc))}", status_code=status.HTTP_303_SEE_OTHER
        )
    return RedirectResponse("/me/2fa", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/me/2fa", response_class=HTMLResponse)
async def my_2fa_setup(
    request: Request,
    user: CurrentUser,
    session: ScopedSession,
    error: str | None = None,
) -> Response:
    """Show the QR code for a pending enrolment.

    The secret is read back out of the row rather than kept in memory between
    requests, so reloading this page shows the same code instead of quietly
    invalidating the one already scanned.
    """
    row = (await session.execute(select(User).where(User.id == user.id))).scalar_one()
    if row.totp_active:
        return RedirectResponse("/me#twofactor", status_code=status.HTTP_303_SEE_OTHER)
    if not row.totp_secret_sealed:
        return RedirectResponse("/me#twofactor", status_code=status.HTTP_303_SEE_OTHER)

    from c2w.auth import totp as _totp
    from c2w.crypto import open_global

    secret = open_global(row.totp_secret_sealed, aad=f"totp:{row.id}")
    issuer = str(await settings_service.get(session, "mfa.issuer_name") or "c2w")
    uri = _totp.provisioning_uri(secret, account=row.email, issuer=issuer)
    enrolment = mfa.Enrolment(
        secret=secret,
        typed_secret=_totp.format_secret(secret),
        uri=uri,
        qr_svg=_totp.qr_svg(uri),
    )
    return templates.TemplateResponse(
        request,
        "me_2fa.html",
        await _shell(request, session, user, "me", enrolment=enrolment, error=error),
    )


@router.post("/me/2fa/confirm")
async def confirm_my_2fa(
    request: Request,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    code: Annotated[str, Form()],
) -> Response:
    row = (await session.execute(select(User).where(User.id == user.id))).scalar_one()
    try:
        codes = await mfa.confirm_enrolment(session, row, code)
    except AuthError as exc:
        return RedirectResponse(
            f"/me/2fa?error={quote_plus(str(exc))}", status_code=status.HTTP_303_SEE_OTHER
        )
    await record_admin_event(
        session,
        actor=row,
        action=AdminAction.MFA_ENABLED,
        brand_id=await _audit_scope(request, session, row),
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    # Rendered rather than redirected: these exist once and a redirect drops them.
    return templates.TemplateResponse(
        request, "recovery_codes.html",
        {"request": request, "codes": codes, "next_url": "/me#twofactor", "standalone": True},
    )


@router.post("/me/2fa/cancel")
async def cancel_my_2fa(
    user: CurrentUser, session: Annotated[AsyncSession, Depends(get_session)]
) -> Response:
    row = (await session.execute(select(User).where(User.id == user.id))).scalar_one()
    try:
        await mfa.cancel_enrolment(session, row)
    except AuthError as exc:
        return RedirectResponse(
            f"/me?error={quote_plus(str(exc))}", status_code=status.HTTP_303_SEE_OTHER
        )
    return RedirectResponse("/me#twofactor", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/me/2fa/disable")
async def disable_my_2fa(
    request: Request,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    password: Annotated[str, Form()],
) -> Response:
    row = (await session.execute(select(User).where(User.id == user.id))).scalar_one()
    try:
        await mfa.disable(session, row, password)
    except AuthError as exc:
        return RedirectResponse(
            f"/me?error={quote_plus(str(exc))}", status_code=status.HTTP_303_SEE_OTHER
        )
    log.info("mfa.disabled", user_id=row.id)
    await record_admin_event(
        session,
        actor=row,
        action=AdminAction.MFA_DISABLED,
        brand_id=await _audit_scope(request, session, row),
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return RedirectResponse("/me?saved=2fa-off#twofactor", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/me/2fa/recovery")
async def regenerate_my_recovery(
    request: Request,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    """Replace the recovery codes.

    Only ever replaces the whole set: the old list cannot be shown again
    (only hashes are kept), so "how many are left" is the one question this
    page can answer, and "issue me a fresh set" the one action.
    """
    from c2w.auth import totp as _totp

    row = (await session.execute(select(User).where(User.id == user.id))).scalar_one()
    if not row.totp_active:
        return RedirectResponse("/me#twofactor", status_code=status.HTTP_303_SEE_OTHER)
    codes = _totp.new_recovery_codes()
    row.totp_recovery_hashes = [_totp.hash_recovery_code(c) for c in codes]
    await session.flush()
    await record_admin_event(
        session,
        actor=row,
        action=AdminAction.RECOVERY_CODES_REISSUED,
        brand_id=await _audit_scope(request, session, row),
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
        detail={"count": len(codes)},
    )
    return templates.TemplateResponse(
        request, "recovery_codes.html",
        {"request": request, "codes": codes, "next_url": "/me#twofactor", "standalone": True},
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

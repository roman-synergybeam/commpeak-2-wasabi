"""Reading people, groups and OUs out of Active Directory.

This exists so an administrator adding somebody can pick them from the
directory instead of retyping an address that has to match exactly. Getting a
character wrong there is not a validation error -- the account is created, and
then simply never matches at sign-in.

Three things shape the design, all of them about a directory being a *remote*
system on the far side of a web request:

* **Every call is bounded.** ldap3 is synchronous, so it runs in a worker
  thread with a connect timeout, a receive timeout and a result size limit. A
  domain controller that accepts a TCP connection and then says nothing would
  otherwise hang the request until the browser gives up, and the operator would
  see a spinner rather than a reason.
* **Failure is a message, never an exception through the page.** Everything
  returns a :class:`DirectoryResult` carrying either entries or a sentence an
  administrator can act on. "Which of these six things is broken" is the whole
  job when a directory will not answer.
* **Nothing is written, ever.** This module binds read-only and searches. Group
  membership decides a role at sign-in; it is not edited from here.

The bind password is a sealed setting, unsealed at the point of use like every
other credential -- see :mod:`c2w.settings`.
"""

from __future__ import annotations

import asyncio
import enum
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from c2w.logging import get_logger
from c2w.settings import settings_service

__all__ = [
    "DirectoryConfig",
    "DirectoryEntry",
    "DirectoryError",
    "DirectoryResult",
    "EntryKind",
    "authenticate",
    "group_memberships",
    "load_config",
    "probe",
    "search",
]

log = get_logger(__name__)

#: Bounded so a silent domain controller cannot hold a web request open.
CONNECT_TIMEOUT_SECONDS = 5
RECEIVE_TIMEOUT_SECONDS = 8
#: A picker is for choosing, not for browsing 40,000 people. Anyone past this
#: should narrow the search instead.
MAX_RESULTS = 50


class EntryKind(enum.StrEnum):
    USER = "user"
    GROUP = "group"
    OU = "ou"


class DirectoryError(Exception):
    """A directory lookup failed, with a message meant for an administrator."""


@dataclass(frozen=True, slots=True)
class DirectoryEntry:
    """One directory object, reduced to what the picker shows."""

    dn: str
    kind: EntryKind
    name: str
    email: str = ""
    login: str = ""
    #: Group and OU entries carry no address; users usually do.
    description: str = ""

    @property
    def label(self) -> str:
        if self.email:
            return f"{self.name} <{self.email}>"
        return self.name


@dataclass(frozen=True, slots=True)
class DirectoryConfig:
    server_uri: str
    bind_dn: str
    bind_password: str
    base_dn: str
    admin_group: str = ""
    user_group: str = ""

    @property
    def configured(self) -> bool:
        """Enough to attempt a connection.

        An anonymous bind is legitimate on some directories, so an empty
        ``bind_dn`` is not disqualifying -- but without a server and a base
        there is nothing to try.
        """
        return bool(self.server_uri and self.base_dn)

    @property
    def uses_tls(self) -> bool:
        return self.server_uri.lower().startswith("ldaps://")


@dataclass(slots=True)
class DirectoryResult:
    """Entries, or the reason there are none."""

    entries: list[DirectoryEntry] = field(default_factory=list)
    error: str = ""
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return not self.error


async def load_config(session: AsyncSession, *, brand_id: int | None = None) -> DirectoryConfig:
    """Read the directory settings, unsealing the bind password."""
    return DirectoryConfig(
        server_uri=(await settings_service.get_str(session, "ldap.server_uri", brand_id=brand_id)
                    or "").strip(),
        bind_dn=(await settings_service.get_str(session, "ldap.bind_dn", brand_id=brand_id)
                 or "").strip(),
        bind_password=await settings_service.get_secret(
            session, "ldap.bind_password", brand_id=brand_id
        ) or "",
        base_dn=(await settings_service.get_str(session, "ldap.base_dn", brand_id=brand_id)
                 or "").strip(),
        admin_group=(await settings_service.get_str(session, "ldap.admin_group",
                                                    brand_id=brand_id) or "").strip(),
        user_group=(await settings_service.get_str(session, "ldap.user_group",
                                                   brand_id=brand_id) or "").strip(),
    )


#: LDAP filters are not SQL, but the same rule applies: never interpolate
#: unescaped input. RFC 4515 says these five characters must be escaped, and
#: without it a search box is a filter-injection hole.
_FILTER_ESCAPES = {
    "\\": r"\5c",
    "*": r"\2a",
    "(": r"\28",
    ")": r"\29",
    "\0": r"\00",
}


def escape_filter(value: str) -> str:
    """Escape a value for use inside an LDAP filter (RFC 4515)."""
    out = []
    for ch in str(value or ""):
        out.append(_FILTER_ESCAPES.get(ch, ch))
    return "".join(out)


def _filter_for(kind: EntryKind, term: str) -> str:
    """The search filter for one kind of object.

    Active Directory's own object classes, with the attribute triple that
    actually matters for finding a person: display name, login name, address.
    """
    safe = escape_filter(term.strip())
    wild = f"*{safe}*" if safe else "*"
    match kind:
        case EntryKind.USER:
            # Exclude computer accounts, which are also `user` objects in AD
            # and are never the answer to "who is this person".
            # userPrincipalName as well as mail: it is the attribute that
            # actually matches at sign-in more often, and plenty of accounts
            # have one without the other -- searching only `mail` made those
            # people unfindable by their own address.
            return (
                "(&(objectCategory=person)(objectClass=user)"
                f"(|(displayName={wild})(sAMAccountName={wild})(mail={wild})"
                f"(userPrincipalName={wild})(cn={wild})))"
            )
        case EntryKind.GROUP:
            return f"(&(objectClass=group)(|(cn={wild})(sAMAccountName={wild})))"
        case EntryKind.OU:
            return f"(&(objectClass=organizationalUnit)(|(ou={wild})(name={wild})))"


_ATTRIBUTES = {
    EntryKind.USER: ["displayName", "sAMAccountName", "mail", "userPrincipalName", "cn"],
    EntryKind.GROUP: ["cn", "sAMAccountName", "description"],
    EntryKind.OU: ["ou", "name", "description"],
}


def _one(value: Any) -> str:
    """ldap3 returns some attributes as a list and some as a scalar."""
    if value in (None, "", []):
        return ""
    if isinstance(value, list | tuple):
        return str(value[0]) if value else ""
    return str(value)


def _to_entry(kind: EntryKind, dn: str, attrs: dict[str, Any]) -> DirectoryEntry:
    if kind is EntryKind.USER:
        # userPrincipalName is the address that matches at sign-in more often
        # than mail does, so it wins when both are present.
        email = _one(attrs.get("userPrincipalName")) or _one(attrs.get("mail"))
        name = _one(attrs.get("displayName")) or _one(attrs.get("cn")) or email
        return DirectoryEntry(
            dn=dn, kind=kind, name=name, email=email,
            login=_one(attrs.get("sAMAccountName")),
        )
    if kind is EntryKind.GROUP:
        return DirectoryEntry(
            dn=dn, kind=kind,
            name=_one(attrs.get("cn")) or _one(attrs.get("sAMAccountName")),
            login=_one(attrs.get("sAMAccountName")),
            description=_one(attrs.get("description")),
        )
    return DirectoryEntry(
        dn=dn, kind=kind,
        name=_one(attrs.get("ou")) or _one(attrs.get("name")),
        description=_one(attrs.get("description")),
    )


def _describe(exc: Exception) -> str:
    """Turn an ldap3 failure into something an administrator can act on.

    The library's own messages name the symptom, not the fix. Which of the six
    things that can be wrong actually is wrong is the entire question when a
    directory will not answer.
    """
    text = str(exc) or exc.__class__.__name__
    lowered = text.lower()
    if "invalidcredentials" in lowered or "invalid credentials" in lowered:
        return (
            "The domain controller refused the reading account. Check the "
            "account name and its password under Settings."
        )
    if (
        "invalid server address" in lowered
        or "name or service not known" in lowered
        or "nodename nor servname" in lowered
        or "getaddrinfo" in lowered
    ):
        return (
            "The domain controller's name could not be resolved. Check the "
            "address under Settings, and that this server uses a DNS resolver "
            "that knows your domain."
        )
    if "socket connection error" in lowered or "connection refused" in lowered:
        return (
            "Could not reach the domain controller. Check the address, and that "
            "this server is allowed to connect to it on that port."
        )
    if "timed out" in lowered or "timeout" in lowered:
        return (
            "The domain controller accepted the connection but did not answer "
            "in time. It may be reachable but not serving LDAP on that port."
        )
    if "certificate" in lowered or "ssl" in lowered or "tls" in lowered:
        return (
            "The encrypted connection was refused. The controller's certificate "
            "is not trusted by this server, or it is not offering LDAPS on that "
            "port."
        )
    if "no such object" in lowered or "invaliddnsyntax" in lowered:
        return (
            "The starting point for the search does not exist on this "
            "controller. Check 'Where to start searching' under Settings."
        )
    if "sizelimit" in lowered:
        return "The directory returned too many results. Narrow the search."
    return f"The directory lookup failed: {text}"


def _connection(config: DirectoryConfig) -> Any:
    """Build a bound, read-only ldap3 connection."""
    from ldap3 import ALL, SAFE_SYNC, Connection, Server

    server = Server(
        config.server_uri,
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        get_info=ALL,
        use_ssl=config.uses_tls,
    )
    return Connection(
        server,
        user=config.bind_dn or None,
        password=config.bind_password or None,
        # read_only stops a bug here from ever modifying the directory.
        read_only=True,
        auto_bind=True,
        receive_timeout=RECEIVE_TIMEOUT_SECONDS,
        client_strategy=SAFE_SYNC,
    )


def _response_rows(connection: Any, outcome: Any) -> list[tuple[str, dict[str, Any]]]:
    """Every entry a search returned, as ``(dn, attributes)``.

    This exists because of a bug that no test could have caught. The code used
    to read ``connection.entries``, which **is never populated under
    SAFE_SYNC** -- that strategy hands the results back from ``search()`` as
    ``(status, result, response, request)`` instead. Production dials
    SAFE_SYNC, so every real directory search returned zero entries: the
    settings test said "found no people under the starting point" about a
    controller holding plenty, the type-ahead offered nothing, and a sign-in
    could never find the account to bind as. Meanwhile the tests passed,
    because they inject a ``MOCK_SYNC`` connection and *that* strategy does
    populate ``.entries``. The mock disagreeing with the real client on where
    the answer lives is the whole trap.

    So the response list is read rather than the convenience attribute, from
    the return value when it is SAFE_SYNC's tuple and from
    ``connection.response`` otherwise. Both strategies fill that list with the
    same shape, which is why this works for the mock and the real thing alike.

    ``searchResRef`` rows -- referrals to another controller, not results --
    carry no attributes and are skipped.
    """
    response = outcome[2] if isinstance(outcome, tuple) else connection.response
    rows: list[tuple[str, dict[str, Any]]] = []
    for item in response or []:
        if item.get("type") != "searchResEntry":
            continue
        rows.append((str(item.get("dn") or ""), dict(item.get("attributes") or {})))
    return rows


def _search_blocking(
    config: DirectoryConfig,
    kind: EntryKind,
    term: str,
    *,
    connection: Any = None,
) -> DirectoryResult:
    """The synchronous body, run in a worker thread by :func:`search`.

    ``connection`` lets a caller supply a ready ldap3 connection instead of
    one being dialled. The tests pass an ldap3 ``MOCK_SYNC`` connection, which
    is what makes the happy path here verifiable without a domain controller --
    the mock's entries live on the connection's strategy, not on the server, so
    injecting a server would hand back an empty directory. A supplied
    connection is left open, because it is not ours to close.
    """
    borrowed = connection is not None
    try:
        if connection is None:
            connection = _connection(config)
        # A search base narrower than the configured one for OUs would hide
        # the very thing being looked for, so all three start from the base.
        outcome = connection.search(
            search_base=config.base_dn,
            search_filter=_filter_for(kind, term),
            attributes=_ATTRIBUTES[kind],
            size_limit=MAX_RESULTS + 1,
        )
        # A `sizeLimitExceeded` result is not a failure: the server answered
        # and stopped at the ceiling we asked for, and the entries it did send
        # are all present. That is what `truncated` reports.
        entries = [
            _to_entry(kind, dn, attrs)
            for dn, attrs in _response_rows(connection, outcome)
        ]
        truncated = len(entries) > MAX_RESULTS
        return DirectoryResult(entries=entries[:MAX_RESULTS], truncated=truncated)
    except Exception as exc:   # every failure becomes a message, not a 500
        log.warning("directory.search_failed", kind=kind.value, error=str(exc)[:200])
        return DirectoryResult(error=_describe(exc))
    finally:
        if connection is not None and not borrowed:
            try:
                connection.unbind()
            except Exception:  # noqa: S110 - closing must not mask the result
                pass


async def search(
    config: DirectoryConfig,
    kind: EntryKind,
    term: str = "",
    *,
    connection: Any = None,
) -> DirectoryResult:
    """Search the directory. Never raises; failures come back as a message."""
    if not config.configured:
        return DirectoryResult(
            error="Active Directory is not configured. Set the domain controller "
                  "address and the starting point under Settings."
        )
    # ldap3 is synchronous. Off the event loop, with a ceiling of its own, so
    # even a library-level hang cannot pin the worker for ever.
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_search_blocking, config, kind, term, connection=connection),
            timeout=CONNECT_TIMEOUT_SECONDS + RECEIVE_TIMEOUT_SECONDS + 2,
        )
    except TimeoutError:
        return DirectoryResult(
            error="The directory did not answer in time. Check that the domain "
                  "controller is reachable from this server."
        )


async def probe(config: DirectoryConfig, *, connection: Any = None) -> DirectoryResult:
    """Bind and read one entry, to report whether the settings work at all.

    Deliberately a real search rather than a bind alone: a bind can succeed
    against a controller whose base DN is wrong, and that failure would then
    only appear later, to somebody else.
    """
    result = await search(config, EntryKind.USER, "", connection=connection)
    if result.ok and not result.entries:
        return DirectoryResult(
            error="Connected and searched, but found no people under the "
                  "starting point. Check 'Where to start searching'."
        )
    return result


async def authenticate(
    config: DirectoryConfig, email: str, password: str, *, connection: Any = None
) -> DirectoryEntry | None:
    """Check a password against the directory. Returns the person, or None.

    The bind *is* the check: LDAP has no "verify this password" call, so
    authenticating means binding as that user and seeing whether the server
    accepts it. Which means the account has to be found first, with the
    reading account, and then a *second* connection opened as them -- the
    reading connection cannot be re-bound without losing it for everyone else.

    Deliberately returns None rather than raising for a wrong password: the
    caller turns that into the same message an unknown local account gets, so
    the sign-in form cannot be used to find out who exists.

    An empty password is refused without contacting the server at all. Many
    directories treat a bind with no password as an *anonymous* bind and
    happily succeed, which would turn "leave the password blank" into a way in.
    """
    if not config.configured:
        return None
    if not password:
        log.info("directory.empty_password_refused", email=email[:64])
        return None

    found = await search(config, EntryKind.USER, email, connection=connection)
    if not found.ok or not found.entries:
        return None

    # An address that matches more than one directory account is not something
    # to guess at.
    matches = [
        e for e in found.entries
        if e.email.lower() == email.strip().lower()
        or e.login.lower() == email.strip().lower().split("@")[0]
    ]
    if len(matches) != 1:
        log.info(
            "directory.ambiguous_or_missing", email=email[:64], candidates=len(matches)
        )
        return None
    person = matches[0]

    if await asyncio.to_thread(_bind_as, config, person.dn, password):
        return person
    return None


def _bind_as(config: DirectoryConfig, dn: str, password: str) -> bool:
    """Try to bind as one account. True when the directory accepts it.

    Its own connection, because re-binding the reading connection would break
    it for every other request, and its own short timeout, because a sign-in
    must not hang on a slow controller.
    """
    from ldap3 import SAFE_SYNC, Connection, Server
    from ldap3.core.exceptions import LDAPException

    server = Server(
        config.server_uri,
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        use_ssl=config.uses_tls,
    )
    connection = None
    try:
        connection = Connection(
            server,
            user=dn,
            password=password,
            read_only=True,
            auto_bind=True,
            receive_timeout=RECEIVE_TIMEOUT_SECONDS,
            client_strategy=SAFE_SYNC,
        )
        return bool(connection.bound)
    except LDAPException:
        # A refused bind is the expected answer to a wrong password, so it is
        # not logged as an error.
        return False
    except Exception as exc:   # anything else is worth seeing
        log.warning("directory.bind_failed", error=str(exc)[:160])
        return False
    finally:
        if connection is not None:
            try:
                connection.unbind()
            except Exception:  # noqa: S110 - closing must not mask the answer
                pass


async def group_memberships(
    config: DirectoryConfig, dn: str, *, connection: Any = None
) -> set[str]:
    """The groups this account belongs to, by common name.

    Read with the *reading* account, not as the person signing in: their own
    rights over the directory are not this platform's business, and a
    directory that hides group membership from ordinary users would otherwise
    silently produce nobody having any role.
    """
    if not config.configured:
        return set()
    safe = escape_filter(dn)
    result = await asyncio.wait_for(
        asyncio.to_thread(
            _groups_blocking, config, f"(&(objectClass=group)(member={safe}))", connection
        ),
        timeout=CONNECT_TIMEOUT_SECONDS + RECEIVE_TIMEOUT_SECONDS + 2,
    )
    return result


def _groups_blocking(config: DirectoryConfig, search_filter: str, connection: Any) -> set[str]:
    borrowed = connection is not None
    try:
        if connection is None:
            connection = _connection(config)
        outcome = connection.search(
            search_base=config.base_dn,
            search_filter=search_filter,
            attributes=["cn", "sAMAccountName"],
            size_limit=200,
        )
        names: set[str] = set()
        for _dn, attrs in _response_rows(connection, outcome):
            for attr in ("cn", "sAMAccountName"):
                value = _one(attrs.get(attr))
                if value:
                    names.add(value)
        return names
    except Exception as exc:
        log.warning("directory.groups_failed", error=str(exc)[:160])
        return set()
    finally:
        if connection is not None and not borrowed:
            try:
                connection.unbind()
            except Exception:  # noqa: S110
                pass


def dn_looks_valid(dn: str) -> bool:
    """A cheap sanity check on a distinguished name from a form.

    Not validation of existence -- only that this is shaped like a DN, so an
    obviously wrong paste is caught before a round trip.
    """
    return bool(re.match(r"^\s*[A-Za-z]+=[^,]+(,\s*[A-Za-z]+=[^,]+)*\s*$", str(dn or "")))

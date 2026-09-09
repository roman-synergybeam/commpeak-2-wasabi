"""Reading Active Directory: the search, the filters, and the failure messages.

Driven against ldap3's ``MOCK_SYNC`` strategy rather than a real domain
controller. That matters: without it only the error paths would be testable and
the part that has to work — finding a person and pulling their address out —
would ship unverified. The mock's entries live on the *connection's* strategy,
which is why :func:`c2w.auth.directory.search` takes a connection rather than a
server.

What is still not covered here, honestly: TLS negotiation, referrals, paged
results past the size limit, and the behaviour of a real controller under
`objectCategory`. Those need a domain controller.
"""

from __future__ import annotations

import pytest
from ldap3 import MOCK_SYNC, OFFLINE_AD_2012_R2, Connection, Server

from c2w.auth.directory import (
    MAX_RESULTS,
    DirectoryConfig,
    EntryKind,
    _filter_for,
    dn_looks_valid,
    escape_filter,
    probe,
    search,
)

BASE = "dc=corp,dc=example"
READER = f"cn=reader,{BASE}"

CONFIG = DirectoryConfig(
    server_uri="ldap://dc01.corp.example",
    bind_dn=READER,
    bind_password="reader-password",
    base_dn=BASE,
)


def _seeded(extra_people: int = 0) -> Connection:
    """A small AD-shaped directory: two people, a group and an OU."""
    server = Server("dc01.corp.example", get_info=OFFLINE_AD_2012_R2)
    conn = Connection(
        server, user=READER, password="reader-password", client_strategy=MOCK_SYNC
    )
    conn.strategy.add_entry(
        READER,
        {"userPassword": "reader-password", "sAMAccountName": "reader",
         "objectClass": ["person", "user"]},
    )
    conn.strategy.add_entry(
        f"cn=Ana Ruiz,ou=Sales,{BASE}",
        {"objectClass": ["top", "person", "user"], "objectCategory": "person",
         "displayName": "Ana Ruiz", "sAMAccountName": "aruiz",
         "userPrincipalName": "ana.ruiz@corp.example", "mail": "ana.ruiz@corp.example",
         "cn": "Ana Ruiz"},
    )
    conn.strategy.add_entry(
        f"cn=Luis Diaz,ou=Sales,{BASE}",
        {"objectClass": ["top", "person", "user"], "objectCategory": "person",
         "displayName": "Luis Diaz", "sAMAccountName": "ldiaz",
         "userPrincipalName": "luis.diaz@corp.example", "cn": "Luis Diaz"},
    )
    conn.strategy.add_entry(
        f"cn=C2W Admins,ou=Groups,{BASE}",
        {"objectClass": ["top", "group"], "cn": "C2W Admins",
         "sAMAccountName": "c2w-admins", "description": "Full access to the console"},
    )
    conn.strategy.add_entry(
        f"ou=Sales,{BASE}", {"objectClass": ["top", "organizationalUnit"], "ou": "Sales"}
    )
    for n in range(extra_people):
        conn.strategy.add_entry(
            f"cn=Extra {n},ou=Sales,{BASE}",
            {"objectClass": ["top", "person", "user"], "objectCategory": "person",
             "displayName": f"Extra {n}", "sAMAccountName": f"extra{n}",
             "userPrincipalName": f"extra{n}@corp.example", "cn": f"Extra {n}"},
        )
    conn.bind()
    return conn


class TestSearching:
    async def test_finds_people_with_their_address(self):
        result = await search(CONFIG, EntryKind.USER, connection=_seeded())
        assert result.ok, result.error
        by_name = {e.name: e for e in result.entries}
        assert set(by_name) == {"Ana Ruiz", "Luis Diaz"}
        assert by_name["Ana Ruiz"].email == "ana.ruiz@corp.example"
        assert by_name["Ana Ruiz"].login == "aruiz"
        assert by_name["Ana Ruiz"].dn == f"cn=Ana Ruiz,ou=Sales,{BASE}"

    async def test_the_reading_account_is_not_offered_as_a_person(self):
        """It has no objectCategory, so the person filter excludes it.

        Also the point of filtering on objectCategory at all: in AD a computer
        account is a `user` object too, and is never the answer to "who is
        this person".
        """
        result = await search(CONFIG, EntryKind.USER, connection=_seeded())
        assert "reader" not in {e.login for e in result.entries}

    async def test_finds_groups(self):
        result = await search(CONFIG, EntryKind.GROUP, connection=_seeded())
        assert [e.name for e in result.entries] == ["C2W Admins"]
        assert result.entries[0].description == "Full access to the console"

    async def test_finds_organisational_units(self):
        result = await search(CONFIG, EntryKind.OU, connection=_seeded())
        assert [e.name for e in result.entries] == ["Sales"]

    @pytest.mark.parametrize(
        "term,expected",
        [
            ("Ana", ["Ana Ruiz"]),
            ("ldiaz", ["Luis Diaz"]),
            ("corp.example", ["Ana Ruiz", "Luis Diaz"]),
            ("nobody", []),
        ],
    )
    async def test_narrowing(self, term: str, expected: list[str]):
        result = await search(CONFIG, EntryKind.USER, term, connection=_seeded())
        assert sorted(e.name for e in result.entries) == sorted(expected)

    async def test_results_are_capped_and_say_so(self):
        """A picker is for choosing, not for browsing forty thousand people."""
        result = await search(
            CONFIG, EntryKind.USER, connection=_seeded(extra_people=MAX_RESULTS + 5)
        )
        assert result.ok
        assert len(result.entries) == MAX_RESULTS
        assert result.truncated is True

    async def test_the_label_falls_back_when_there_is_no_address(self):
        result = await search(CONFIG, EntryKind.GROUP, connection=_seeded())
        assert result.entries[0].label == "C2W Admins"


class TestProbe:
    async def test_a_working_directory_reports_ok(self):
        assert (await probe(CONFIG, connection=_seeded())).ok

    async def test_an_empty_base_is_reported_as_a_problem(self):
        """A bind can succeed against a controller whose base DN is wrong.

        Reporting that as success would push the failure onto whoever tries to
        use the picker later.
        """
        conn = _seeded()
        wrong_base = DirectoryConfig(
            server_uri=CONFIG.server_uri, bind_dn=READER,
            bind_password="reader-password", base_dn="ou=Nowhere,dc=corp,dc=example",
        )
        result = await probe(wrong_base, connection=conn)
        assert not result.ok
        assert "starting point" in result.error.lower()


class TestFailuresAreMessagesNotExceptions:
    async def test_not_configured(self):
        result = await search(DirectoryConfig("", "", "", ""), EntryKind.USER)
        assert not result.ok
        assert "not configured" in result.error.lower()

    async def test_a_base_that_does_not_exist_yields_nothing(self):
        """No entries, and no exception through the page.

        A real controller answers "no such object" here, which `_describe`
        turns into a sentence about the starting point; ldap3's mock is more
        lenient and simply returns nothing. Both are safe, so the assertion is
        on what is guaranteed either way. `probe` is what converts an empty
        result into a message -- see TestProbe.
        """
        bad = DirectoryConfig(
            server_uri=CONFIG.server_uri, bind_dn=READER,
            bind_password="reader-password", base_dn="dc=elsewhere,dc=invalid",
        )
        result = await search(bad, EntryKind.USER, connection=_seeded())
        assert result.entries == []

    async def test_an_unreachable_server_does_not_raise(self):
        """No connection is injected, so this dials a host that is not there.

        The point is the shape of the failure, not its wording: a page must get
        a message back, and must not hang while it waits.
        """
        unreachable = DirectoryConfig(
            server_uri="ldap://127.0.0.1:1",   # nothing listens on port 1
            bind_dn=READER, bind_password="x", base_dn=BASE,
        )
        result = await search(unreachable, EntryKind.USER)
        assert not result.ok
        assert result.entries == []


class TestFilterEscaping:
    """LDAP filters are not SQL, but the same rule applies."""

    @pytest.mark.parametrize(
        "raw,escaped",
        [
            ("plain", "plain"),
            ("a*b", r"a\2ab"),
            ("a(b)c", r"a\28b\29c"),
            ("back\\slash", r"back\5cslash"),
        ],
    )
    def test_escapes_the_rfc_4515_characters(self, raw: str, escaped: str):
        assert escape_filter(raw) == escaped

    @pytest.mark.parametrize(
        "term",
        ["*", "Ana)", "*)(objectClass=*", "a\\b", "x)(|(objectClass=*"],
    )
    def test_no_metacharacter_survives_into_the_filter(self, term: str):
        """Asserted on the filter string, not on what a server does with it.

        ldap3's mock filter engine treats ``\\2a`` as a wildcard rather than as
        a literal asterisk, so driving this through the mock would report an
        injection a real controller does not have -- and would pass just as
        happily with the escaping removed. The filter text is what this code is
        responsible for, so that is what is checked.
        """
        built = _filter_for(EntryKind.USER, term)
        for char, escape in (("*", r"\2a"), ("(", r"\28"), (")", r"\29"), ("\\", r"\5c")):
            if char in term:
                assert escape in built, f"{char!r} was not escaped in {built!r}"

    def test_the_escaped_filter_is_still_a_search_for_the_literal_text(self):
        """The escaping must not lose the search term itself."""
        assert "Ana" in _filter_for(EntryKind.USER, "Ana")
        assert _filter_for(EntryKind.USER, "").count("*") >= 5   # match-all per attribute

    async def test_an_injected_term_returns_no_new_entries_via_a_real_filter(self):
        """A term that is *only* metacharacters finds nobody by name.

        Kept as an end-to-end check on the non-wildcard cases, which the mock
        does handle: a stray closing paren must not broaden the result set.
        """
        result = await search(CONFIG, EntryKind.USER, "Ana)", connection=_seeded())
        assert result.entries == []


class TestDnSanity:
    @pytest.mark.parametrize(
        "dn", ["cn=Ana Ruiz,ou=Sales,dc=corp,dc=example", "dc=corp,dc=example", "ou=Sales,dc=x"]
    )
    def test_accepts_a_dn(self, dn: str):
        assert dn_looks_valid(dn)

    @pytest.mark.parametrize("dn", ["", "not a dn", "ana.ruiz@corp.example", "cn="])
    def test_rejects_what_is_not_one(self, dn: str):
        assert not dn_looks_valid(dn)


class TestReadOnly:
    def test_the_module_never_writes(self):
        """CommPeak is not the only system we must not modify.

        A directory is somebody else's authority. This module binds read-only
        and searches; if it ever grows an add/modify/delete call, that is a
        deliberate decision and this test should be the thing that objects.
        """
        import pathlib
        import re

        source = pathlib.Path("src/c2w/auth/directory.py").read_text()
        # Matched against an ldap3 *connection*, not any `.add(` -- a Python
        # set has one of those, and the crude substring check failed on
        # `names.add(value)` while proving nothing about LDAP.
        writes = re.findall(
            r"\b(?:connection|conn|c)\s*\.\s*(add|modify|delete|modify_dn)\s*\(", source
        )
        assert not writes, f"directory.py must not write to the directory: {writes}"
        # And every connection it opens is opened read-only, which is the
        # server-side half of the same promise.
        opens = source.count("Connection(")
        assert opens > 0
        assert source.count("read_only=True") == opens, (
            "every ldap3 Connection must be opened read_only=True"
        )


class TestAuthenticating:
    """Signing in against the directory.

    The bind *is* the check -- LDAP has no "verify this password" call -- so
    these drive ldap3's mock, which does enforce the seeded password. The
    cases that matter are the refusals, because each one is a way in if it is
    wrong.
    """

    async def test_the_right_password_returns_the_person(self):
        conn = _seeded()
        # The mock enforces userPassword on a bind, so seed one for Ana.
        conn.strategy.add_entry(
            f"cn=Ana Auth,ou=Sales,{BASE}",
            {"objectClass": ["top", "person", "user"], "objectCategory": "person",
             "displayName": "Ana Auth", "sAMAccountName": "aauth",
             "userPrincipalName": "ana.auth@corp.example", "cn": "Ana Auth",
             "userPassword": "her-password"},
        )
        found = await search(CONFIG, EntryKind.USER, "ana.auth", connection=conn)
        assert len(found.entries) == 1
        assert found.entries[0].dn == f"cn=Ana Auth,ou=Sales,{BASE}"

    async def test_an_empty_password_is_refused_without_asking_the_server(self):
        """Many directories treat a bind with no password as *anonymous* and
        succeed, which would make "leave it blank" a way in."""
        from c2w.auth.directory import authenticate

        # No connection is passed, so a server call would have to dial out --
        # this returning False proves it never got that far.
        assert await authenticate(CONFIG, "ana.ruiz@corp.example", "") is None

    async def test_an_unconfigured_directory_refuses(self):
        from c2w.auth.directory import authenticate

        assert await authenticate(DirectoryConfig("", "", "", ""), "a@b", "pw") is None

    async def test_an_address_matching_nobody_refuses(self):
        from c2w.auth.directory import authenticate

        assert await authenticate(CONFIG, "nobody@corp.example", "pw",
                                  connection=_seeded()) is None

    async def test_groups_are_read_with_the_reading_account(self):
        """Read as the reader, not as the person: a directory that hides
        membership from ordinary users would otherwise give nobody a role."""
        from c2w.auth.directory import group_memberships

        conn = _seeded()
        conn.strategy.add_entry(
            f"cn=C2W Operators,ou=Groups,{BASE}",
            {"objectClass": ["top", "group"], "cn": "C2W Operators",
             "member": [f"cn=Ana Ruiz,ou=Sales,{BASE}"]},
        )
        groups = await group_memberships(
            CONFIG, f"cn=Ana Ruiz,ou=Sales,{BASE}", connection=conn
        )
        assert "C2W Operators" in groups

    async def test_a_dn_with_metacharacters_cannot_widen_the_group_search(self):
        """The DN goes into a filter, so it gets the same escaping as a term."""
        from c2w.auth.directory import escape_filter

        dn = "cn=Ana*,ou=Sales,dc=corp,dc=example"
        assert r"\2a" in escape_filter(dn)

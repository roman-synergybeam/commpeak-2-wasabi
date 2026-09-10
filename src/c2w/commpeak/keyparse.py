"""Parse CommPeak recording object keys into structured metadata.

CommPeak stores recordings in an S3-compatible bucket under an hour-resolution
prefix hierarchy, documented at
https://docs.commpeak.com/docs/recordings-access-accounts-out

    /{year}/{month}/{day}/{hour}/{direction}-{number}-{extension}-{date}-{time}-{uniqueid}.{seq}.{ext}

A real example from the documentation::

    /2025/11/11/02/out-441632960770-101-20211111-125343-1636635223.0.flac

Critically, the key contains **no ``call_uuid``**, while the CDR API keys every
call on ``call_uuid``/``call_id``.  The ``uniqueid`` component is a FreeSWITCH
channel id -- a unix epoch second at channel creation -- which is the strongest
join key we get from the filename.  Everything in :mod:`c2w.commpeak.correlate`
builds on the fields extracted here, so this module stays dependency-free and
exhaustively tested.

The parser is deliberately tolerant: CommPeak's naming has varied across PBX
versions and tenants, so an unrecognised key yields a :class:`ParsedKey` with
``confidence_inputs`` reduced rather than raising.  An object we cannot parse is
still inventoried and still offloaded; it simply correlates on fewer signals.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final

__all__ = [
    "DEFAULT_KEY_ROOT",
    "Direction",
    "ParsedKey",
    "day_prefix",
    "iter_day_prefixes",
    "parse_key",
]

# Directional prefixes CommPeak uses.  "out"/"in" are documented; the others
# appear in PBX-generated recordings (conference, queue, voicemail legs).
_DIRECTION_ALIASES: Final[dict[str, str]] = {
    "out": "out",
    "outbound": "out",
    "in": "in",
    "inbound": "in",
    "conf": "conf",
    "conference": "conf",
    "queue": "queue",
    "vm": "voicemail",
    "voicemail": "voicemail",
}

Direction = str

# out-441632960770-101-20211111-125343-1636635223.0.flac
#  ^dir ^number     ^ext ^date   ^time  ^uniqueid  ^seq ^ext
_FULL: Final[re.Pattern[str]] = re.compile(
    r"""
    ^
    (?P<direction>[a-z]+)          -
    (?P<number>\+?\d{1,20})        -
    (?P<extension>[A-Za-z0-9_]{1,32}) -
    (?P<date>\d{8})                -
    (?P<time>\d{6})                -
    (?P<uniqueid>\d{9,12})
    (?:\.(?P<seq>\d{1,4}))?
    \.(?P<ext>[A-Za-z0-9]{1,8})
    $
    """,
    re.VERBOSE,
)

# Same shape without the extension field:
# out-441632960770-20211111-125343-1636635223.0.flac
_NO_EXTENSION: Final[re.Pattern[str]] = re.compile(
    r"""
    ^
    (?P<direction>[a-z]+)   -
    (?P<number>\+?\d{1,20}) -
    (?P<date>\d{8})         -
    (?P<time>\d{6})         -
    (?P<uniqueid>\d{9,12})
    (?:\.(?P<seq>\d{1,4}))?
    \.(?P<ext>[A-Za-z0-9]{1,8})
    $
    """,
    re.VERBOSE,
)

# 1788871734.100994-out-005551999752466-201-20260908-124856.flac
#  ^uniqueid ^seq     ^dir ^number        ^ext ^date   ^time  ^ext
#
# The channel id comes *first* here, not last. This is what InterMagnum's PBX
# instances actually write, and Go4Rex's write the documented order, so both
# shapes are live at once across the two organisations. Without this pattern
# the salvage path below still recovered the uniqueid and the timestamp -- but
# it took the leading channel id to be the phone `number`, because that is the
# first six-or-more digit run in the basename. A wrong number is worse than no
# number: it feeds `numbers_agree` during correlation and the indexed search
# column, so it produces confident false matches rather than an obvious gap.
_UNIQUEID_FIRST: Final[re.Pattern[str]] = re.compile(
    r"""
    ^
    (?P<uniqueid>\d{9,12})
    (?:\.(?P<seq>\d{1,8}))?       -
    (?P<direction>[a-z]+)          -
    (?P<number>\+?\d{1,20})        -
    (?P<extension>[A-Za-z0-9_]{1,32}) -
    (?P<date>\d{8})                -
    (?P<time>\d{6})
    \.(?P<ext>[A-Za-z0-9]{1,8})
    $
    """,
    re.VERBOSE,
)

# Last-resort salvage: pull out whatever recognisable tokens exist anywhere in
# the basename.  Used only when the structured patterns fail.
_ANY_UNIQUEID: Final[re.Pattern[str]] = re.compile(r"(?<!\d)(?P<uniqueid>1\d{9})(?!\d)")
_ANY_DATETIME: Final[re.Pattern[str]] = re.compile(r"(?<!\d)(?P<date>\d{8})-(?P<time>\d{6})(?!\d)")
_ANY_NUMBER: Final[re.Pattern[str]] = re.compile(r"(?<!\d)(?P<number>\d{6,20})(?!\d)")

# The date path a key sits under. Measured against all eight live buckets, it
# is `recordings/YYYY/MM/DD/<basename>` -- a root prefix the documentation
# omits, and **no hour level**, which the documentation shows. So the hour
# group is optional: real keys have three date components, and the documented
# four-component form is still accepted in case an account somewhere uses it.
#
# `\d{4}/` before it would also match the leading digits of a phone number in
# a basename, so the pattern is anchored to a path separator or the start.
_PREFIX: Final[re.Pattern[str]] = re.compile(
    r"(?:^|/)(?P<year>\d{4})/(?P<month>\d{2})/(?P<day>\d{2})/(?:(?P<hour>\d{2})/)?"
)

_AUDIO_EXTENSIONS: Final[frozenset[str]] = frozenset({"flac", "mp3", "wav", "ogg", "opus", "m4a"})

# CommPeak writes sidecar JSON next to some recordings; we inventory those
# separately rather than treating them as media.
_SIDECAR_EXTENSIONS: Final[frozenset[str]] = frozenset({"json", "txt", "xml"})


@dataclass(slots=True)
class ParsedKey:
    """Structured view of one recording object key.

    ``parsed_ok`` means the full documented pattern matched.  Partial results
    still carry whatever was recoverable; callers must treat every optional
    field as genuinely optional.
    """

    key: str
    basename: str
    parsed_ok: bool = False
    direction: Direction | None = None
    number: str | None = None
    extension: str | None = None
    started_at: datetime | None = None
    uniqueid: int | None = None
    seq: int = 0
    file_ext: str | None = None
    prefix_hour: datetime | None = None
    is_audio: bool = False
    is_sidecar: bool = False
    #: Names of the fields that were successfully recovered.  The correlator
    #: uses this to decide which match tiers are even attempted.
    confidence_inputs: set[str] = field(default_factory=set)

    @property
    def uniqueid_time(self) -> datetime | None:
        """Channel-creation time implied by the FreeSWITCH unique id."""
        if self.uniqueid is None:
            return None
        return datetime.fromtimestamp(self.uniqueid, tz=UTC)

    @property
    def call_group_key(self) -> str | None:
        """Key grouping the parts of one split recording together.

        CommPeak emits multi-part recordings as the same ``uniqueid`` with an
        incrementing ``seq``.  Grouping on the unique id keeps the parts of a
        call together even when correlation to a CDR fails.
        """
        if self.uniqueid is None:
            return None
        return f"{self.uniqueid}"


def _to_datetime(date: str, time: str) -> datetime | None:
    """Build a UTC datetime from the ``YYYYMMDD`` / ``HHMMSS`` key fields."""
    try:
        return datetime.strptime(f"{date}{time}", "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return None


def _prefix_time(key: str) -> datetime | None:
    """The date bucket a key sits in, from its path.

    Midnight when the path stops at the day, which is what every live bucket
    does. This is only ever a *fallback* anchor for correlation -- the epoch
    channel id and the basename's own wall clock are both far more precise and
    are tried first -- so a whole-day granularity here costs nothing when it is
    used and is better than the `None` the hour-only pattern returned for every
    real key.
    """
    m = _PREFIX.search(key)
    if not m:
        return None
    try:
        return datetime(
            int(m["year"]),
            int(m["month"]),
            int(m["day"]),
            int(m["hour"] or 0),
            tzinfo=UTC,
        )
    except ValueError:
        return None


def _normalise_number(raw: str) -> str:
    """Strip a leading ``+`` so numbers compare consistently with CDR fields.

    CDR ``src`` values arrive as ``593990899917@did.commpeak.com`` and ``dst``
    as bare digits, so the correlator compares digit strings only.
    """
    return raw.lstrip("+")


def parse_key(key: str) -> ParsedKey:
    """Parse a recording object key. Never raises."""
    basename = key.rsplit("/", 1)[-1]
    result = ParsedKey(key=key, basename=basename, prefix_hour=_prefix_time(key))
    if result.prefix_hour is not None:
        result.confidence_inputs.add("prefix_hour")

    for pattern in (_FULL, _NO_EXTENSION, _UNIQUEID_FIRST):
        m = pattern.match(basename)
        if not m:
            continue
        groups = m.groupdict()
        result.parsed_ok = True
        result.direction = _DIRECTION_ALIASES.get(
            groups["direction"].lower(), groups["direction"].lower()
        )
        result.number = _normalise_number(groups["number"])
        result.extension = groups.get("extension")
        result.started_at = _to_datetime(groups["date"], groups["time"])
        result.uniqueid = int(groups["uniqueid"])
        result.seq = int(groups["seq"] or 0)
        result.file_ext = groups["ext"].lower()
        result.confidence_inputs.update({"direction", "number", "uniqueid"})
        if result.extension:
            result.confidence_inputs.add("extension")
        if result.started_at:
            result.confidence_inputs.add("started_at")
        break
    else:
        # Structured match failed -- salvage individual tokens so the object is
        # still correlatable on time and/or number.
        stem, _, ext = basename.rpartition(".")
        if ext:
            result.file_ext = ext.lower()
        head = basename.split("-", 1)[0].lower()
        if head in _DIRECTION_ALIASES:
            result.direction = _DIRECTION_ALIASES[head]
            result.confidence_inputs.add("direction")
        if uid := _ANY_UNIQUEID.search(stem or basename):
            result.uniqueid = int(uid["uniqueid"])
            result.confidence_inputs.add("uniqueid")
        if dt := _ANY_DATETIME.search(stem or basename):
            result.started_at = _to_datetime(dt["date"], dt["time"])
            if result.started_at:
                result.confidence_inputs.add("started_at")
        if num := _ANY_NUMBER.search(stem or basename):
            result.number = _normalise_number(num["number"])
            result.confidence_inputs.add("number")

    if result.file_ext:
        result.is_audio = result.file_ext in _AUDIO_EXTENSIONS
        result.is_sidecar = result.file_ext in _SIDECAR_EXTENSIONS
    return result


#: Where the date tree starts inside a CommPeak bucket.
#:
#: Measured on all eight live buckets. The published key layout starts at the
#: year; the real one does not. Overridable per organisation through
#: `source.key_root_prefix`, because it is somebody else's bucket layout and
#: not ours to assume for ever.
DEFAULT_KEY_ROOT: Final[str] = "recordings/"


def day_prefix(moment: datetime, root: str = DEFAULT_KEY_ROOT) -> str:
    """The listing prefix for one day, e.g. ``recordings/2025/11/11/``.

    A **day**, not an hour, and with a root. Both corrections come from
    measuring the live buckets: keys are `recordings/YYYY/MM/DD/<basename>`
    with files directly under the day.

    This was `hour_prefix`, producing `2025/11/11/02/`. That matched nothing in
    any of the eight buckets, so a scan listed empty prefix after empty prefix
    and finished *successfully* having found nothing -- 170 recorded runs, all
    `ok = true`, all `discovered = 0`. A scanner that cannot find anything and
    does not say so is worse than one that fails.
    """
    utc = moment.astimezone(UTC)
    return f"{root}{utc.year:04d}/{utc.month:02d}/{utc.day:02d}/"


def iter_day_prefixes(start: datetime, end: datetime, root: str = DEFAULT_KEY_ROOT):
    """Yield every day prefix in ``[start, end]`` inclusive, oldest first.

    Day-at-a-time enumeration is what makes a multi-million-object bucket scan
    resumable: each prefix is an independent listing whose completion can be
    recorded, so a scan interrupted after hours of work resumes where it
    stopped rather than starting again.

    A day is a larger unit of work than the hour this used to yield -- more
    objects per listing, and the S3 paginator handles that -- but it is the
    unit the bucket is actually organised into, and inventing a level that does
    not exist bought resumability at the price of finding nothing.
    """
    from datetime import timedelta

    cursor = start.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    last = end.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    step = timedelta(days=1)
    while cursor <= last:
        yield day_prefix(cursor, root)
        cursor += step

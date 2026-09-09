"""The catalogue of runtime settings.

Every tunable the platform has is declared here and stored in the database, not
in an env file.  This module holds only the *schema* -- name, type, default,
whether a brand may override it -- while values live in the ``app_settings``
table and are read through :mod:`c2w.settings`.

Why a registry rather than free-form rows: a typo in a settings key would
otherwise silently fall back to a default, and an operator editing settings
through the UI needs to know what a setting means, what type it takes and what
happens if they change it.  The registry supplies all of that, and
``validate()`` rejects bad values at write time instead of at 3am in a worker.

Each spec carries a human ``label`` and a ``unit``.  The dotted key is a machine
identifier -- for the CLI, for logs, for a support conversation -- and nobody
administering this platform should have to read one to find a setting, so the UI
shows the label and keeps the key as a small hint.

Two values are deliberately *not* here, because they are what gets the process
to the database in the first place: the database URL and the master key.  See
:class:`c2w.config.Bootstrap`.
"""

from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

#: The zones these businesses actually work in, nearest the top.
#:
#: Both are Seychelles-registered brokers selling into Latin America and
#: Iberia, so the useful list is the Americas, the two Iberian zones, and the
#: Seychelles zone the entities are registered in -- not a generic European
#: list. A menu of all 486 IANA names is harder to use than a text box.
#:
#: America/Puerto_Rico is the default because it is -04:00 all year with no
#: daylight saving, which is what makes a schedule set to 02:00 actually run at
#: 02:00 in January and July alike.
TIMEZONE_CHOICES: Final[tuple[str, ...]] = (
    "America/Puerto_Rico",       # UTC-04:00 all year
    "America/Santo_Domingo",     # UTC-04:00 all year
    "America/Caracas",           # UTC-04:00 all year
    "America/La_Paz",            # UTC-04:00 all year
    "America/Manaus",            # UTC-04:00 all year
    "America/Sao_Paulo",         # Brazil, UTC-03:00
    "America/Argentina/Buenos_Aires",
    "America/Bogota",            # UTC-05:00
    "America/Lima",              # UTC-05:00
    "America/Guayaquil",         # Ecuador, UTC-05:00
    "America/Mexico_City",
    "America/Santiago",          # Chile, with daylight saving
    "America/New_York",
    "America/Los_Angeles",
    "Europe/Lisbon",             # Portugal
    "Europe/Madrid",             # Spain
    "Europe/London",
    "Indian/Mahe",               # Seychelles, UTC+04:00
    "UTC",
)

#: Wasabi's regional endpoints, so the region is chosen rather than typed --
#: a mistyped region is a bucket that cannot be reached at all.
WASABI_REGION_CHOICES: Final[tuple[str, ...]] = (
    "eu-central-1",
    "eu-central-2",
    "eu-west-1",
    "eu-west-2",
    "eu-south-1",
    "us-east-1",
    "us-east-2",
    "us-central-1",
    "us-west-1",
    "ca-central-1",
    "ap-northeast-1",
    "ap-northeast-2",
    "ap-southeast-1",
    "ap-southeast-2",
)

_HOURS: Final[tuple[str, ...]] = tuple(str(h) for h in range(24))

#: The languages these calls are actually in. Naming them matters for accuracy:
#: a recogniser told the language beats one guessing, and Brazilian Portuguese
#: and European Portuguese are different enough to be worth separating.
LANGUAGE_CHOICES: Final[tuple[str, ...]] = (
    "auto-detect",
    "en — English",
    "es — Spanish (Latin America)",
    "es-ES — Spanish (Spain)",
    "pt-BR — Portuguese (Brazil)",
    "pt-PT — Portuguese (Portugal)",
)

__all__ = [
    "CATEGORY_ORDER",
    "SETTINGS",
    "SettingSpec",
    "SettingType",
    "get_spec",
    "specs_by_category",
]


class SettingType(enum.StrEnum):
    STRING = "string"
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    SECRET = "secret"  # noqa: S105 - a type label, not a credential
    JSON = "json"


@dataclass(frozen=True, slots=True)
class SettingSpec:
    key: str
    type: SettingType
    default: Any
    category: str
    #: Human-readable name shown in the UI.
    label: str
    #: One or two sentences on what this does and what changing it costs.
    description: str
    #: Unit rendered beside the input, so "Archive recordings older than [90]
    #: days" reads as a sentence rather than a bare number.
    unit: str = ""
    #: Whether a brand may override the global value.  Engine-wide limits are
    #: global-only so one brand cannot lift a cap that protects the host.
    brand_overridable: bool = False
    #: Secrets are sealed before storage and never returned to a client.
    sensitive: bool = False
    #: Optional extra validation beyond the type check.
    validator: Callable[[Any], None] | None = None
    #: True when changing the value requires a service restart to take effect.
    restart_required: bool = False
    #: Where to read more. Some of these settings are a value copied out of
    #: someone else's admin console, and the useful help is a link to the page
    #: it comes from rather than a paraphrase of it.
    help_url: str = ""
    help_label: str = ""
    #: Fixed set of acceptable values, rendered as a menu instead of a text box.
    choices: tuple[str, ...] = ()


def _positive(value: Any) -> None:
    if value <= 0:
        raise ValueError("must be greater than zero")


def _non_negative(value: Any) -> None:
    if value < 0:
        raise ValueError("must be zero or greater")


def _concurrency(value: Any) -> None:
    if not 1 <= value <= 200:
        raise ValueError("must be between 1 and 200")


def _commpeak_concurrency(value: Any) -> None:
    # CommPeak's own documentation recommends 5 concurrent transfers per S3
    # account.  Allowing an operator to set 50 here just earns throttling and
    # a slower migration, so the ceiling is deliberate.
    if not 1 <= value <= 10:
        raise ValueError("CommPeak recommends 5; values above 10 will be throttled")


def _ttl(value: Any) -> None:
    if not 30 <= value <= 3600:
        raise ValueError("must be between 30 and 3600 seconds")


def _hour(value: Any) -> None:
    if not 0 <= value <= 23:
        raise ValueError("must be an hour between 0 and 23")


def _http_url(value: Any) -> None:
    """Reject an address that will not work as one.

    Worth checking on write: a malformed public address does not fail here, it
    fails later as a rejected single sign-on redirect, which is a much harder
    thing to trace back to a typed slash.
    """
    from urllib.parse import urlparse

    text_ = str(value or "").strip()
    if not text_:
        return
    parsed = urlparse(text_)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("must start with http:// or https://")
    if not parsed.netloc:
        raise ValueError("no host in the address")
    if "//" in parsed.path:
        raise ValueError("looks like it has a doubled slash")
    if parsed.netloc.endswith(":") or parsed.netloc.startswith(":"):
        raise ValueError("the port looks wrong")


def _hostname(value: Any) -> None:
    """A bare host name, not a full address."""
    text_ = str(value or "").strip()
    if not text_:
        return
    if "://" in text_ or "/" in text_:
        raise ValueError("just the host name, without http:// or a path")


def _log_level(value: Any) -> None:
    if str(value).upper() not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        raise ValueError("must be DEBUG, INFO, WARNING or ERROR")


_SPECS: Final[tuple[SettingSpec, ...]] = (
    # -- General -----------------------------------------------------------
    SettingSpec(
        key="core.base_url",
        type=SettingType.STRING,
        default="http://localhost:8000",
        category="Web address and sessions",
        label="Public web address",
        description="Where this system is reachable, for example "
        "https://recordings.example.com. Alerts link to it, and single sign-on "
        "returns to it -- so a wrong value here shows up as a refused login.",
        validator=_http_url,
    ),
    SettingSpec(
        key="core.environment",
        choices=('production', 'staging', 'dev'),
        type=SettingType.STRING,
        default="production",
        category="Web address and sessions",
        label="Environment",
        description="Label for this installation: production, staging or dev.",
        restart_required=True,
    ),
    SettingSpec(
        key="core.session_ttl_seconds",
        choices=('3600', '14400', '28800', '43200', '86400'),
        type=SettingType.INT,
        default=8 * 3600,
        category="Web address and sessions",
        label="Stay signed in for",
        description="How long a sign-in lasts before it has to be repeated.",
        unit="seconds",
        validator=_positive,
    ),
    # -- CommPeak calls ---------------------------------------------------
    SettingSpec(
        key="transfer.enabled",
        type=SettingType.BOOL,
        default=False,
        category="Copying to the archive",
        label="Copy recordings to the archive",
        description="Turn on once archive storage is configured and tested. "
        "Recordings are still discovered and matched to calls while this is "
        "off, so nothing is lost and nothing needs re-scanning later.",
    ),
    SettingSpec(
        key="transfer.concurrency_global",
        choices=('5', '10', '20', '30', '50', '100'),
        type=SettingType.INT,
        default=20,
        category="Copying to the archive",
        label="Total simultaneous transfers",
        description="The ceiling across every account and every organisation. "
        "The per-account and per-organisation limits sit under this one, so "
        "lowering it slows everything down at once.",
        unit="at a time",
        validator=_concurrency,
    ),
    SettingSpec(
        key="transfer.concurrency_per_destination",
        choices=('2', '5', '10', '20', '30'),
        type=SettingType.INT,
        default=10,
        category="Copying to the archive",
        label="Simultaneous uploads per archive",
        description="Upload limit for a single archive destination.",
        unit="at a time",
        validator=_concurrency,
    ),
    SettingSpec(
        key="transfer.concurrency_per_brand",
        choices=('1', '2', '5', '10', '20'),
        type=SettingType.INT,
        default=5,
        category="Copying to the archive",
        label="Simultaneous transfers per organisation",
        description="Stops one company's backfill consuming the whole server.",
        unit="at a time",
        validator=_concurrency,
    ),
    SettingSpec(
        key="transfer.bandwidth_limit_mbps",
        choices=('0', '50', '100', '200', '500', '1000'),
        type=SettingType.FLOAT,
        default=0.0,
        category="Copying to the archive",
        label="Bandwidth limit",
        description="Set this when recordings are pulled over the same "
        "connection that carries live calls. Zero means take everything "
        "available.",
        unit="Mbit/s (0 = unlimited)",
        validator=_non_negative,
    ),
    SettingSpec(
        key="transfer.multipart_threshold_bytes",
        choices=('8388608', '16777216', '33554432', '67108864'),
        type=SettingType.INT,
        default=16 * 1024 * 1024,
        category="Copying to the archive",
        label="Use multi-part upload for files above",
        description="Larger recordings are uploaded in pieces so an "
        "interrupted transfer can resume instead of starting again.",
        unit="bytes",
        validator=_positive,
    ),
    SettingSpec(
        key="transfer.multipart_chunk_bytes",
        choices=('5242880', '8388608', '16777216', '33554432'),
        type=SettingType.INT,
        default=16 * 1024 * 1024,
        category="Copying to the archive",
        label="Size of each upload piece",
        description="Increased automatically for very large recordings to stay "
        "within the 10,000-piece limit.",
        unit="bytes",
        validator=_positive,
    ),
    SettingSpec(
        key="transfer.max_attempts",
        choices=('3', '5', '8', '10'),
        type=SettingType.INT,
        default=5,
        category="Copying to the archive",
        label="Give up on a recording after",
        description="Problems that a retry cannot fix -- a wrong password, a "
        "missing address-list entry -- stop immediately regardless of this, and "
        "raise an alert instead.",
        unit="attempts",
        validator=_positive,
    ),
    SettingSpec(
        key="transfer.retry_backoff_seconds",
        type=SettingType.JSON,
        default=[30, 120, 600, 1800],
        category="Copying to the archive",
        label="Wait between retries",
        description="Waiting periods before each new attempt, applied with a "
        "little randomness so a shared outage does not make every recording "
        "retry at the same instant.",
        unit="seconds",
    ),
    SettingSpec(
        key="transfer.job_claim_batch",
        choices=('1', '4', '8', '16', '32'),
        type=SettingType.INT,
        default=8,
        category="Copying to the archive",
        label="Recordings a worker takes at once",
        description="How much work each worker picks up per round.",
        unit="at a time",
        validator=_positive,
    ),
    SettingSpec(
        key="transfer.job_lease_seconds",
        choices=('300', '600', '900', '1800', '3600'),
        type=SettingType.INT,
        default=900,
        category="Copying to the archive",
        label="Assume a worker has stopped after",
        description="Its recordings are then handed to another worker. Must be "
        "longer than the slowest single recording takes to copy.",
        unit="seconds",
        validator=_positive,
    ),
    SettingSpec(
        key="transfer.write_sidecar_metadata",
        type=SettingType.BOOL,
        default=True,
        category="Copying to the archive",
        label="Save call details beside each recording",
        description="Writes a small file next to every archived recording "
        "describing the call it belongs to. If this database were ever lost, "
        "the archive would still say who called whom and when.",
        brand_overridable=True,
    ),
    # -- Retention ---------------------------------------------------------
    SettingSpec(
        key="retention.offload_after_days",
        choices=('0', '7', '30', '60', '90', '180', '365'),
        type=SettingType.INT,
        default=90,
        category="Retention",
        label="Archive recordings older than",
        description="Recordings are listed and searchable straight away; this "
        "is only when the copy to long-term storage happens.",
        unit="days",
        validator=_non_negative,
        brand_overridable=True,
    ),
    SettingSpec(
        key="retention.allow_source_deletion",
        type=SettingType.BOOL,
        default=False,
        category="Retention",
        label="Allow deleting from CommPeak",
        description="Not implemented, and cannot be enabled from here. "
        "CommPeak states that deleted recordings cannot be recovered, so this "
        "system has no code path that deletes there.",
    ),
    SettingSpec(
        key="retention.keep_archive_years",
        choices=('1', '2', '3', '5', '7', '10'),
        type=SettingType.INT,
        default=7,
        category="Retention",
        label="Keep archived recordings for",
        description="How long recordings are retained in long-term storage.",
        unit="years",
        validator=_non_negative,
        brand_overridable=True,
    ),
    # -- Playback and downloads --------------------------------------------
    SettingSpec(
        key="media.presign_ttl_seconds",
        choices=('60', '300', '900', '1800', '3600'),
        type=SettingType.INT,
        default=300,
        category="Playback and downloads",
        label="Playback link expires after",
        description="Playing a recording hands the browser a temporary link "
        "straight to storage. Keep this short: the link is the key.",
        unit="seconds",
        validator=_ttl,
    ),
    SettingSpec(
        key="media.proxy_enabled",
        type=SettingType.BOOL,
        default=True,
        category="Playback and downloads",
        label="Stream audio through this server when needed",
        description="Only used where a direct storage link is not acceptable. "
        "It works, but the audio then consumes this server's bandwidth.",
    ),
    SettingSpec(
        key="media.transcode_enabled",
        type=SettingType.BOOL,
        default=False,
        category="Playback and downloads",
        label="Convert FLAC to MP3 for playback",
        description="For browsers that cannot play FLAC. Requires ffmpeg.",
    ),
    SettingSpec(
        key="media.ffmpeg_path",
        choices=('/usr/bin/ffmpeg', '/usr/local/bin/ffmpeg', '/snap/bin/ffmpeg'),
        type=SettingType.STRING,
        default="/usr/bin/ffmpeg",
        category="Playback and downloads",
        label="Location of ffmpeg",
        description="Only needed when conversion is switched on.",
    ),
    # -- Notifications -----------------------------------------------------
    SettingSpec(
        key="alerts.enabled",
        type=SettingType.BOOL,
        default=True,
        category="Alerts",
        label="Send alerts",
        description="Master switch for Telegram and Slack notifications.",
    ),
    SettingSpec(
        key="alerts.dedupe_window_seconds",
        choices=('300', '900', '1800', '3600', '21600'),
        type=SettingType.INT,
        default=1800,
        category="Alerts",
        label="Suppress repeated alerts for",
        description="One problem can affect thousands of recordings at once. "
        "Without this, that becomes thousands of identical messages and people "
        "mute the channel.",
        unit="seconds",
        validator=_non_negative,
    ),
    SettingSpec(
        key="alerts.telegram_bot_token",
        type=SettingType.SECRET,
        default="",
        category="Alerts",
        label="Telegram bot token",
        description="From @BotFather. Stored encrypted and never shown again.",
        sensitive=True,
        brand_overridable=True,
    ),
    SettingSpec(
        key="alerts.telegram_chat_id",
        type=SettingType.STRING,
        default="",
        category="Alerts",
        label="Telegram chat",
        description="The group or channel that alerts are posted to.",
        brand_overridable=True,
    ),
    SettingSpec(
        key="alerts.slack_webhook_url",
        type=SettingType.SECRET,
        default="",
        category="Alerts",
        label="Slack webhook address",
        description="An incoming-webhook address from your Slack workspace. "
        "Stored encrypted and never shown again.",
        sensitive=True,
        brand_overridable=True,
    ),
    SettingSpec(
        key="alerts.daily_digest_hour",
        choices=_HOURS,
        type=SettingType.INT,
        default=8,
        category="Alerts",
        label="Send the daily summary at",
        description="A once-a-day digest of what was archived and what failed.",
        unit=":00 UTC",
        validator=_hour,
    ),
    # -- Scheduling --------------------------------------------------------
    SettingSpec(
        key="schedule.reconcile_hour",
        choices=_HOURS,
        type=SettingType.INT,
        default=3,
        category="Scheduling",
        label="Run the nightly archive check at",
        description="Compares CommPeak, this database and the archive against "
        "each other, and re-queues anything missing or corrupted.",
        unit=":00 UTC",
        validator=_hour,
    ),
    SettingSpec(
        key="schedule.cdr_poll_seconds",
        choices=('60', '300', '600', '900', '1800'),
        type=SettingType.INT,
        default=300,
        category="Scheduling",
        label="Fetch new call records every",
        description="How often to ask CommPeak for calls it has logged.",
        unit="seconds",
        validator=_positive,
    ),
    # -- Logs and monitoring -----------------------------------------------
    SettingSpec(
        key="observability.log_level",
        choices=('DEBUG', 'INFO', 'WARNING', 'ERROR'),
        type=SettingType.STRING,
        default="INFO",
        category="Logs and monitoring",
        label="Log detail",
        description="INFO is right for normal running. DEBUG is very noisy and "
        "will include every S3 request; use it while diagnosing something and "
        "put it back afterwards.",
        validator=_log_level,
        restart_required=True,
    ),
    SettingSpec(
        key="observability.log_json",
        type=SettingType.BOOL,
        default=True,
        category="Logs and monitoring",
        label="Write logs as JSON",
        description="Easier for log collectors to read; harder for a person. "
        "Turn off when reading the log by eye.",
        restart_required=True,
    ),
    SettingSpec(
        key="observability.metrics_enabled",
        type=SettingType.BOOL,
        default=True,
        category="Logs and monitoring",
        label="Publish Prometheus metrics",
        description="Exposes transfer counts and timings for monitoring.",
        restart_required=True,
    ),
    # -- Sign-in and access ------------------------------------------------
    SettingSpec(
        key="org.display_name",
        type=SettingType.STRING,
        default="",
        category="Your company",
        label="Company name",
        description="How this organisation is named in alerts, exports and the "
        "daily summary. Leave blank to use the name it was created with.",
        brand_overridable=True,
    ),
    SettingSpec(
        key="org.contact_email",
        type=SettingType.STRING,
        default="",
        category="Your company",
        label="Where to reach someone here",
        description="Included in alerts so whoever receives one knows who to "
        "contact. Not used to send mail.",
        brand_overridable=True,
    ),
    SettingSpec(
        key="org.timezone",
        choices=TIMEZONE_CHOICES,
        type=SettingType.STRING,
        default="America/Puerto_Rico",
        category="Your company",
        label="Time zone",
        description="Schedules for this organisation run in this zone, and the "
        "clock in the corner shows it. The server's own clock is not used. The "
        "zones offered first are -04:00 all year, so a job set for 02:00 runs "
        "at 02:00 in January and in July.",
        help_url="https://en.wikipedia.org/wiki/List_of_tz_database_time_zones",
        help_label="list of time-zone names",
        brand_overridable=True,
    ),
    SettingSpec(
        key="org.data_region_note",
        choices=('', 'No restriction', 'EU only', 'UK only', 'US only', 'Customer country only'),
        type=SettingType.STRING,
        default="",
        category="Your company",
        label="Where recordings may be stored",
        description="A note for whoever configures storage next -- for example "
        "\"EU only\". Recorded here so the requirement is not carried in "
        "somebody's memory. It is not enforced.",
        brand_overridable=True,
    ),
    # -- CommPeak calls ---------------------------------------------------
    SettingSpec(
        key="commpeak.s3_endpoint",
        choices=('https://recordings.commpeak.com',),
        type=SettingType.STRING,
        default="https://recordings.commpeak.com",
        category="CommPeak calls",
        label="Recordings address",
        validator=_http_url,
        description="Where recordings are read from, for every CommPeak account "
        "this organisation has. Each account has its own bucket and its own "
        "credentials; the address is the same for all of them.",
        help_url="https://docs.commpeak.com/docs/recordings-access-accounts-out",
        help_label="CommPeak's own instructions",
        brand_overridable=True,
    ),
    SettingSpec(
        key="commpeak.cdr_api_base",
        type=SettingType.STRING,
        default="",
        category="CommPeak calls",
        label="Call records address",
        validator=_http_url,
        description="Your PBX Stats instance, which is where the list of calls "
        "comes from. It is per-account, not one shared address, and looks like "
        "https://yourname.stats.pbx.commpeak.com \u2014 the name is the one in "
        "your CommPeak console. Without it recordings are still archived, just "
        "with no call details attached.",
        help_url="https://docs.commpeak.com/reference/pbx-stats-api",
        help_label="CommPeak: the PBX Stats API",
        brand_overridable=True,
    ),
    SettingSpec(
        key="commpeak.cdr_api_path",
        choices=('/api/cdrs',),
        type=SettingType.STRING,
        default="/api/cdrs",
        category="CommPeak calls",
        label="Call records path",
        description="The part of the address that returns calls. There is only "
        "one, and it is filled in already; it is here so a change at CommPeak's "
        "end does not need a new release.",
        brand_overridable=True,
    ),
    SettingSpec(
        key="commpeak.cdr_auth_scheme",
        type=SettingType.STRING,
        default="header",
        category="CommPeak calls",
        label="How to authenticate",
        description="PBX Stats wants the key on its own in an Authorization "
        "header, which is the first option and the right one. The others exist "
        "only for an account that has been set up differently.",
        choices=("header", "bearer", "basic", "query", "none"),
        brand_overridable=True,
    ),
    SettingSpec(
        key="commpeak.cdr_api_user",
        type=SettingType.STRING,
        default="",
        category="CommPeak calls",
        label="Call records user name",
        description="Only needed when authentication is set to Basic. Leave "
        "empty otherwise.",
        brand_overridable=True,
    ),
    SettingSpec(
        key="commpeak.cdr_api_token",
        type=SettingType.SECRET,
        default="",
        category="CommPeak calls",
        label="Call records API key",
        description="The API key from your CommPeak console. Encrypted before "
        "it is stored and never shown again.",
        help_url="https://docs.commpeak.com/reference/pbx-stats-api",
        help_label="Where to find it",
        sensitive=True,
        brand_overridable=True,
    ),
    SettingSpec(
        key="commpeak.cdr_page_size",
        choices=('100', '250', '500', '1000'),
        type=SettingType.INT,
        default=500,
        category="CommPeak calls",
        label="Calls fetched per request",
        description="How many calls to ask for at a time. Larger is fewer "
        "requests but a longer wait for each one.",
        unit="calls",
        validator=_positive,
        brand_overridable=True,
    ),
    SettingSpec(
        key="commpeak.cdr_poll_minutes",
        choices=('1', '5', '10', '15', '30', '60'),
        type=SettingType.INT,
        default=15,
        category="CommPeak calls",
        label="Check for new calls every",
        description="How often to ask for calls that have finished since the "
        "last check.",
        unit="minutes",
        validator=_positive,
        brand_overridable=True,
    ),
    SettingSpec(
        key="commpeak.cdr_overlap_minutes",
        choices=('0', '5', '15', '30', '60', '180'),
        type=SettingType.INT,
        default=30,
        category="CommPeak calls",
        label="Re-check the last",
        description="Each check goes back this far beyond where the last one "
        "ended. A call is written to CommPeak's records when it finishes, not "
        "when it started, so a long call can appear behind one already seen \u2014 "
        "without this overlap those are missed for good.",
        unit="minutes",
        validator=_non_negative,
        brand_overridable=True,
    ),
    # -- CommPeak SMS (TextPeak) -------------------------------------------
    SettingSpec(
        key="sms.enabled",
        type=SettingType.BOOL,
        default=False,
        category="CommPeak messages",
        label="Collect text messages",
        description="Fetch sent and received messages from CommPeak TextPeak "
        "and list them alongside calls. Off until an API key is set below.",
        help_url="https://docs.commpeak.com/reference/textpeak-api",
        help_label="CommPeak: the TextPeak API",
        brand_overridable=True,
    ),
    SettingSpec(
        key="sms.api_base",
        choices=('https://gw.commpeak.com',),
        type=SettingType.STRING,
        default="https://gw.commpeak.com",
        category="CommPeak messages",
        label="Messages address",
        validator=_http_url,
        description="Where messages are read from. Unlike call records this is "
        "one shared address for every account, so it is filled in already.",
        brand_overridable=True,
    ),
    SettingSpec(
        key="sms.api_path",
        choices=('/textpeak/streams/messages',),
        type=SettingType.STRING,
        default="/textpeak/streams/messages",
        category="CommPeak messages",
        label="Sent messages path",
        description="The part of the address that returns messages you sent, "
        "with their delivery status. Filled in already.",
        brand_overridable=True,
    ),
    SettingSpec(
        key="sms.incoming_path",
        choices=('/textpeak/streams/incoming_messages',),
        type=SettingType.STRING,
        default="/textpeak/streams/incoming_messages",
        category="CommPeak messages",
        label="Received messages path",
        description="Replies and inbound messages come from a different address "
        "than sent ones, and carry different fields \u2014 there is no delivery "
        "status on a message that has arrived. Filled in already.",
        brand_overridable=True,
    ),
    SettingSpec(
        key="sms.api_token",
        type=SettingType.SECRET,
        default="",
        category="CommPeak messages",
        label="Messages API key",
        description="Your TextPeak API key, sent in an Authorization header. "
        "Encrypted before it is stored and never shown again. This is a "
        "different key from the one for call records.",
        sensitive=True,
        brand_overridable=True,
    ),
    SettingSpec(
        key="sms.stream_id",
        type=SettingType.STRING,
        default="",
        category="CommPeak messages",
        label="Only this stream",
        description="A TextPeak stream id, if this organisation should see one "
        "stream rather than every message on the account. Leave empty for all "
        "of them.",
        brand_overridable=True,
    ),
    SettingSpec(
        key="sms.page_size",
        choices=('50', '100', '250', '500'),
        type=SettingType.INT,
        default=100,
        category="CommPeak messages",
        label="Messages fetched per request",
        description="How many messages to ask for at a time. Larger is fewer "
        "requests; TextPeak refuses very large pages, so 100 is a safe middle.",
        unit="messages",
        validator=_positive,
        brand_overridable=True,
    ),
    SettingSpec(
        key="sms.poll_minutes",
        choices=('5', '10', '15', '30', '60'),
        type=SettingType.INT,
        default=15,
        category="CommPeak messages",
        label="Check for new messages every",
        description="How often to ask for messages that have arrived or changed "
        "since the last check.",
        unit="minutes",
        validator=_positive,
        brand_overridable=True,
    ),
    SettingSpec(
        key="sms.overlap_hours",
        choices=('1', '6', '12', '24', '72'),
        type=SettingType.INT,
        default=24,
        category="CommPeak messages",
        label="Re-check the last",
        description="Each check re-reads this far back, because delivery "
        "receipts arrive long after the message was sent \u2014 a message seen as "
        "\u201csent\u201d becomes \u201cdelivered\u201d hours later, and without this it would "
        "stay wrong for ever.",
        unit="hours",
        validator=_positive,
        brand_overridable=True,
    ),
    SettingSpec(
        key="source.read_only",
        type=SettingType.BOOL,
        default=True,
        category="CommPeak calls",
        label="Never write to CommPeak",
        description="This system only ever reads from CommPeak. The ability to "
        "write or delete there is not in the software at all, so this cannot be "
        "switched off from here.",
        help_url="https://docs.commpeak.com/docs/recordings-access-accounts-out",
        help_label="CommPeak: deleted recordings cannot be restored",
    ),
    SettingSpec(
        key="source.concurrency_per_connection",
        choices=('1', '2', '3', '4', '5', '6', '8', '10'),
        type=SettingType.INT,
        default=5,
        category="CommPeak calls",
        label="Simultaneous downloads per account",
        description="CommPeak recommends five and slows you down above it, so a "
        "higher number makes a migration take longer, not less.",
        unit="at a time",
        validator=_commpeak_concurrency,
    ),
    SettingSpec(
        key="source.list_page_size",
        choices=('100', '250', '500', '1000'),
        type=SettingType.INT,
        default=1000,
        category="CommPeak calls",
        label="Recordings listed per request",
        description="How many recordings to ask CommPeak about at once while "
        "looking for new ones.",
        unit="recordings",
        validator=_positive,
    ),
    SettingSpec(
        key="source.incremental_poll_seconds",
        choices=('60', '300', '600', '900', '1800', '3600'),
        type=SettingType.INT,
        default=300,
        category="CommPeak calls",
        label="Look for new recordings every",
        description="How often to check CommPeak for recordings that have "
        "appeared since the last look.",
        unit="seconds",
        validator=_positive,
        brand_overridable=True,
    ),
    SettingSpec(
        key="source.incremental_overlap_hours",
        choices=('0', '1', '2', '3', '6', '12', '24'),
        type=SettingType.INT,
        default=3,
        category="CommPeak calls",
        label="Re-check the most recent",
        description="A call that starts at 10:59 and runs ten minutes is filed "
        "under 10:00 well after that hour has passed. Looking again at a few "
        "hours already seen is what stops those being missed.",
        unit="hours",
        validator=_non_negative,
    ),
    # -- Wasabi storage ----------------------------------------------------
    SettingSpec(
        key="wasabi.region",
        choices=WASABI_REGION_CHOICES,
        type=SettingType.STRING,
        default="eu-central-1",
        category="Wasabi storage",
        label="Region suggested for new storage",
        description="An organisation can have as many Wasabi accounts and "
        "buckets as it needs; this is only the region offered first when adding "
        "one. Pick where its recordings are allowed to live -- moving terabytes "
        "afterwards is slow and is charged for.",
        help_url="https://docs.wasabi.com/v1/docs/what-are-the-service-urls-for-wasabis-different-storage-regions",
        help_label="Wasabi's list of regions",
        brand_overridable=True,
    ),
    SettingSpec(
        key="wasabi.path_prefix",
        choices=('archive', 'recordings', 'c2w'),
        type=SettingType.STRING,
        default="archive",
        category="Wasabi storage",
        label="Folder inside each bucket",
        description="In every one of this organisation's buckets, recordings go "
        "under this folder, then by tenant and date. Keeping an organisation to "
        "its own folder is what makes per-organisation lifecycle rules and usage "
        "reporting possible.",
        brand_overridable=True,
    ),
    SettingSpec(
        key="wasabi.verify_after_upload",
        type=SettingType.BOOL,
        default=True,
        category="Wasabi storage",
        label="Read the copy back and check it",
        description="A successful upload is not proof that what arrived matches "
        "what was sent. Leave this on: the archive is eventually the only copy.",
    ),
    SettingSpec(
        key="wasabi.min_free_gb",
        choices=('1', '2', '5', '10', '20', '50'),
        type=SettingType.INT,
        default=5,
        category="Wasabi storage",
        label="Refuse to run below",
        description="Stops a copy starting when the server itself is nearly out "
        "of disk, since a part-written file has to go somewhere.",
        unit="GB free",
        validator=_non_negative,
    ),
    # -- Microsoft 365 -----------------------------------------------------
    SettingSpec(
        key="auth.oidc_entra_enabled",
        type=SettingType.BOOL,
        default=False,
        category="Microsoft 365",
        label="Let people sign in with Microsoft",
        description="Staff use their existing work account instead of a password "
        "kept here. Fill in the three values below first.",
        help_url="https://learn.microsoft.com/entra/identity-platform/quickstart-register-app",
        help_label="how to register the application",
        brand_overridable=True,
    ),
    SettingSpec(
        key="auth.oidc_entra_tenant_id",
        type=SettingType.STRING,
        default="",
        category="Microsoft 365",
        label="Directory (tenant) ID",
        description="Identifies your Microsoft organisation. Copy it from the "
        "overview page of the Entra admin centre.",
        help_url="https://entra.microsoft.com",
        help_label="Entra admin centre",
        brand_overridable=True,
    ),
    SettingSpec(
        key="auth.oidc_entra_client_id",
        type=SettingType.STRING,
        default="",
        category="Microsoft 365",
        label="Application (client) ID",
        description="From the app registration you create for this system.",
        brand_overridable=True,
    ),
    SettingSpec(
        key="auth.oidc_entra_client_secret",
        type=SettingType.SECRET,
        default="",
        category="Microsoft 365",
        label="Client secret",
        description="Created under Certificates & secrets on that registration. "
        "Encrypted before it is stored and never shown again. Note its expiry -- "
        "sign-in stops working the day it lapses.",
        sensitive=True,
        brand_overridable=True,
    ),
    SettingSpec(
        key="auth.entra_redirect_note",
        type=SettingType.STRING,
        default="",
        category="Microsoft 365",
        label="Redirect address to register",
        description="Add this exact address to the app registration as a Web "
        "redirect URI, or sign-in is refused. It is your public address followed "
        "by /auth/entra/callback.",
        brand_overridable=True,
    ),
    SettingSpec(
        key="auth.entra_allowed_domains",
        type=SettingType.STRING,
        default="",
        category="Microsoft 365",
        label="Only allow these email domains",
        description="Comma separated, for example contoso.com. Leave blank to "
        "accept anyone your directory lets through.",
        brand_overridable=True,
    ),
    # -- Google Workspace --------------------------------------------------
    SettingSpec(
        key="auth.oidc_google_enabled",
        type=SettingType.BOOL,
        default=False,
        category="Google Workspace",
        label="Let people sign in with Google",
        description="Staff use their existing Google work account. Fill in the "
        "two values below first.",
        help_url="https://developers.google.com/identity/openid-connect/openid-connect",
        help_label="how to create the credentials",
        brand_overridable=True,
    ),
    SettingSpec(
        key="auth.oidc_google_client_id",
        type=SettingType.STRING,
        default="",
        category="Google Workspace",
        label="Client ID",
        description="From an OAuth client of type Web application in the Google "
        "Cloud console.",
        help_url="https://console.cloud.google.com/apis/credentials",
        help_label="Google Cloud credentials",
        brand_overridable=True,
    ),
    SettingSpec(
        key="auth.oidc_google_client_secret",
        type=SettingType.SECRET,
        default="",
        category="Google Workspace",
        label="Client secret",
        description="Shown once when the client is created. Encrypted before it "
        "is stored and never shown again.",
        sensitive=True,
        brand_overridable=True,
    ),
    SettingSpec(
        key="auth.google_allowed_domains",
        type=SettingType.STRING,
        default="",
        category="Google Workspace",
        label="Only allow these email domains",
        description="Comma separated. Without this, any Google account can "
        "attempt to sign in -- set it to your own domain.",
        brand_overridable=True,
    ),
    # -- Active Directory --------------------------------------------------
    SettingSpec(
        key="ldap.enabled",
        type=SettingType.BOOL,
        default=False,
        category="Active Directory",
        label="Take the list of people from Active Directory",
        description="For an on-premises directory. If your accounts are in "
        "Microsoft 365, use that section instead -- this is for a domain "
        "controller you run yourself.",
        brand_overridable=True,
    ),
    SettingSpec(
        key="ldap.server_uri",
        type=SettingType.STRING,
        default="",
        category="Active Directory",
        label="Domain controller address",
        description="For example ldaps://dc01.corp.example:636. Use ldaps, or "
        "the password below crosses your network in the clear.",
        brand_overridable=True,
    ),
    SettingSpec(
        key="ldap.bind_dn",
        type=SettingType.STRING,
        default="",
        category="Active Directory",
        label="Account used to read the directory",
        description="A read-only service account, for example "
        "CN=svc-c2w,OU=Service,DC=corp,DC=example. It needs no privileges beyond "
        "reading users and groups.",
        brand_overridable=True,
    ),
    SettingSpec(
        key="ldap.bind_password",
        type=SettingType.SECRET,
        default="",
        category="Active Directory",
        label="Its password",
        description="Encrypted before it is stored and never shown again.",
        sensitive=True,
        brand_overridable=True,
    ),
    SettingSpec(
        key="ldap.base_dn",
        type=SettingType.STRING,
        default="",
        category="Active Directory",
        label="Where to start searching",
        description="For example DC=corp,DC=example. Narrow it to the part of "
        "the tree your staff are in.",
        brand_overridable=True,
    ),
    SettingSpec(
        key="ldap.admin_group",
        type=SettingType.STRING,
        default="",
        category="Active Directory",
        label="Group that gets full access",
        description="Members become administrators here: they can change "
        "settings, add storage and see the audit log.",
        brand_overridable=True,
    ),
    SettingSpec(
        key="ldap.user_group",
        type=SettingType.STRING,
        default="",
        category="Active Directory",
        label="Group that gets ordinary access",
        description="Members can search calls and listen to recordings, but not "
        "download them or change anything. Anyone in neither group cannot sign "
        "in at all.",
        brand_overridable=True,
    ),
    SettingSpec(
        key="ldap.sync_interval_hours",
        choices=('1', '3', '6', '12', '24'),
        type=SettingType.INT,
        default=6,
        category="Active Directory",
        label="Re-read the directory every",
        description="Someone removed from a group loses access at the next read, "
        "so keep this short enough for your offboarding process.",
        unit="hours",
        validator=_positive,
        brand_overridable=True,
    ),
    SettingSpec(
        key="ldap.remove_when_gone",
        type=SettingType.BOOL,
        default=True,
        category="Active Directory",
        label="Switch off accounts that leave the groups",
        description="Their history in the audit log is kept -- only the ability "
        "to sign in is withdrawn.",
        brand_overridable=True,
    ),
    # -- Two-factor --------------------------------------------------------
    SettingSpec(
        key="auth.local_accounts_enabled",
        type=SettingType.BOOL,
        default=True,
        category="Two-factor and passwords",
        label="Allow email and password sign-in",
        description="Switch this off once staff sign in through Microsoft, Google "
        "or your directory. The main administrator keeps password sign-in either "
        "way, as a way back in if single sign-on breaks.",
    ),
    SettingSpec(
        key="mfa.require_totp",
        type=SettingType.BOOL,
        default=False,
        category="Two-factor and passwords",
        label="Require an authenticator app",
        description="Everyone signing in with a password kept here must set up "
        "an authenticator, and is walked through it at their next sign-in "
        "before they can go any further. People who sign in through Microsoft, "
        "Google or your directory are unaffected -- that system already carries "
        "whatever second factor you have set there. Anyone who has turned it on "
        "for themselves keeps being asked either way.",
        help_url="https://datatracker.ietf.org/doc/html/rfc6238",
        help_label="How time-based codes work (RFC 6238)",
    ),
    SettingSpec(
        key="mfa.issuer_name",
        type=SettingType.STRING,
        default="c2w",
        category="Two-factor and passwords",
        label="Name shown in the authenticator",
        description="What people see next to the code in their app.",
    ),
    SettingSpec(
        key="auth.password_min_length",
        choices=('8', '10', '12', '14', '16', '20'),
        type=SettingType.INT,
        default=12,
        category="Two-factor and passwords",
        label="Shortest password allowed",
        description="Applies to accounts kept here. Accounts from Microsoft, "
        "Google or your directory follow that system's own rules.",
        unit="characters",
        validator=_positive,
    ),
    SettingSpec(
        key="auth.lockout_attempts",
        choices=('3', '5', '8', '10', '20'),
        type=SettingType.INT,
        default=8,
        category="Two-factor and passwords",
        label="Lock an account after",
        description="Wrong passwords in a row before sign-in is refused for a "
        "while. These credentials open recorded phone calls, so an unlimited "
        "number of guesses is not acceptable.",
        unit="wrong attempts",
        validator=_positive,
    ),
    SettingSpec(
        key="auth.lockout_minutes",
        choices=('5', '15', '30', '60'),
        type=SettingType.INT,
        default=15,
        category="Two-factor and passwords",
        label="Keep it locked for",
        description="The lock clears itself, so nobody has to be called out to "
        "release it.",
        unit="minutes",
        validator=_positive,
    ),
    # -- Cloudflare --------------------------------------------------------
    SettingSpec(
        key="turnstile.enabled",
        type=SettingType.BOOL,
        default=False,
        category="Cloudflare",
        label="Check visitors are human at sign-in",
        description="Adds Cloudflare's Turnstile challenge to the sign-in page. "
        "Worth having if the console is reachable from the internet.",
        help_url="https://developers.cloudflare.com/turnstile/get-started/",
        help_label="how to get the two keys",
    ),
    SettingSpec(
        key="turnstile.site_key",
        type=SettingType.STRING,
        default="",
        category="Cloudflare",
        label="Turnstile site key",
        description="The public half. It appears in the page, so it is not a "
        "secret.",
    ),
    SettingSpec(
        key="turnstile.secret_key",
        type=SettingType.SECRET,
        default="",
        category="Cloudflare",
        label="Turnstile secret key",
        description="The private half, used by this server to check the "
        "challenge. Encrypted before it is stored.",
        sensitive=True,
    ),
    SettingSpec(
        key="turnstile.skip_on_lan",
        type=SettingType.BOOL,
        default=True,
        category="Cloudflare",
        label="Skip the challenge on the local network",
        description="Keeps you able to sign in from the office if Cloudflare is "
        "unreachable. Leave on unless you have another way in.",
    ),
    SettingSpec(
        key="tunnel.enabled",
        type=SettingType.BOOL,
        default=False,
        category="Cloudflare",
        label="Publish through a Cloudflare tunnel",
        description="Reaches the console from outside without opening a port on "
        "your firewall. The tunnel runs as its own service; this only records "
        "how it is set up.",
        help_url="https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/",
        help_label="how to create a tunnel",
    ),
    SettingSpec(
        key="tunnel.hostname",
        type=SettingType.STRING,
        default="",
        category="Cloudflare",
        label="Public address",
        description="The name people use to reach the console, for example "
        "recordings.example.com. Host name only -- no https:// and no path.",
        validator=_hostname,
    ),
    SettingSpec(
        key="tunnel.token",
        type=SettingType.SECRET,
        default="",
        category="Cloudflare",
        label="Tunnel token",
        description="Issued when you create the tunnel. Encrypted before it is "
        "stored and never shown again.",
        sensitive=True,
    ),
    # -- Transcription -----------------------------------------------------
    SettingSpec(
        key="transcribe.enabled",
        type=SettingType.BOOL,
        default=False,
        category="Transcription and voice analysis",
        label="Transcribe archived recordings",
        description="Not yet built. The settings are here so the shape of it is "
        "agreed and the schema is in place; no recogniser is wired up, so "
        "switching this on transcribes nothing today.",
        brand_overridable=True,
    ),
    SettingSpec(
        key="transcribe.engine",
        type=SettingType.STRING,
        default="whisper-local",
        category="Transcription and voice analysis",
        label="Which recogniser to use",
        description="Running Whisper on this server keeps recordings and their "
        "transcripts inside your own infrastructure, which matters for call "
        "recordings. The hosted options are faster and cost per minute.",
        choices=(
            "whisper-local",
            "faster-whisper-local",
            "openai-whisper-api",
            "azure-speech",
            "google-speech",
            "aws-transcribe",
        ),
        help_url="https://github.com/openai/whisper#available-models-and-languages",
        help_label="Whisper's models and languages",
        brand_overridable=True,
    ),
    SettingSpec(
        key="transcribe.model",
        type=SettingType.STRING,
        default="medium",
        category="Transcription and voice analysis",
        label="Model size",
        description="Larger is more accurate and much slower. For Spanish and "
        "Portuguese over a phone line, medium is the usual floor -- small and "
        "below lose accented speech and names.",
        choices=("tiny", "base", "small", "medium", "large-v3", "turbo"),
        brand_overridable=True,
    ),
    SettingSpec(
        key="transcribe.primary_language",
        type=SettingType.STRING,
        default="auto-detect",
        category="Transcription and voice analysis",
        label="Main language on these calls",
        description="Telling the recogniser the language beats letting it "
        "guess, especially on short calls. Choose auto-detect only if the calls "
        "are genuinely mixed.",
        choices=LANGUAGE_CHOICES,
        brand_overridable=True,
    ),
    SettingSpec(
        key="transcribe.also_expect",
        type=SettingType.STRING,
        default="",
        category="Transcription and voice analysis",
        label="Also expect",
        description="A second language that turns up on these calls. With "
        "auto-detect this narrows the guess; with a fixed main language it is "
        "used when detection disagrees strongly.",
        choices=("", *LANGUAGE_CHOICES[1:]),
        brand_overridable=True,
    ),
    SettingSpec(
        key="transcribe.diarize",
        type=SettingType.BOOL,
        default=True,
        category="Transcription and voice analysis",
        label="Separate the speakers",
        description="Marks who is speaking when, so a transcript reads as a "
        "conversation. Where the call has an agent extension the agent's turns "
        "are labelled with their name.",
        brand_overridable=True,
    ),
    SettingSpec(
        key="transcribe.word_timestamps",
        type=SettingType.BOOL,
        default=True,
        category="Transcription and voice analysis",
        label="Timestamp every phrase",
        description="Lets the player jump to a phrase found by searching, which "
        "is most of the value of having a transcript at all.",
        brand_overridable=True,
    ),
    SettingSpec(
        key="transcribe.redact_numbers",
        type=SettingType.BOOL,
        default=False,
        category="Transcription and voice analysis",
        label="Mask long digit sequences",
        description="Replaces card-length and account-length runs of digits in "
        "the stored text. The audio is untouched -- this only limits what a "
        "text search can turn up.",
        brand_overridable=True,
    ),
    SettingSpec(
        key="transcribe.only_longer_than",
        type=SettingType.INT,
        default=15,
        category="Transcription and voice analysis",
        label="Skip calls shorter than",
        description="Very short calls are ring-outs and wrong numbers. Skipping "
        "them saves most of the cost for none of the value.",
        unit="seconds",
        choices=("0", "5", "10", "15", "30", "60"),
        validator=_non_negative,
        brand_overridable=True,
    ),
    SettingSpec(
        key="transcribe.concurrency",
        type=SettingType.INT,
        default=1,
        category="Transcription and voice analysis",
        label="Recordings transcribed at once",
        description="Transcription is the heaviest thing this server will do. "
        "Keep it low, or it competes with the copying that has a deadline.",
        unit="at a time",
        choices=("1", "2", "3", "4", "6", "8"),
        validator=_positive,
    ),
    SettingSpec(
        key="analysis.sentiment",
        type=SettingType.BOOL,
        default=False,
        category="Transcription and voice analysis",
        label="Score how the call went",
        description="Not yet built. Intended as a per-call reading from the "
        "transcript, for finding calls worth listening to rather than for "
        "judging anyone by.",
        brand_overridable=True,
    ),
    SettingSpec(
        key="analysis.keywords",
        type=SettingType.STRING,
        default="",
        category="Transcription and voice analysis",
        label="Flag calls mentioning",
        description="Comma separated words or phrases. A call whose transcript "
        "contains one is marked so it can be found later -- complaint words, a "
        "competitor's name, a compliance phrase.",
        brand_overridable=True,
    ),
    SettingSpec(
        key="analysis.retain_transcript_years",
        type=SettingType.INT,
        default=7,
        category="Transcription and voice analysis",
        label="Keep transcripts for",
        description="A transcript is personal data in its own right and is far "
        "easier to search than audio, so it is worth keeping for no longer than "
        "the recording it came from.",
        unit="years",
        choices=("1", "2", "3", "5", "7", "10"),
        validator=_non_negative,
        brand_overridable=True,
    ),
)

SETTINGS: Final[dict[str, SettingSpec]] = {s.key: s for s in _SPECS}

#: Every category, in a stable order. The settings page groups these into a
#: left-hand rail (see `_SETTING_GROUPS` in `web/routes.py`); this tuple is the
#: registry's own list, and `specs_by_category` follows it.
CATEGORY_ORDER: Final[tuple[str, ...]] = (
    "Your company",
    "CommPeak calls",
    "CommPeak messages",
    "Wasabi storage",
    "Copying to the archive",
    "Retention",
    "Playback and downloads",
    "Transcription and voice analysis",
    "Alerts",
    "Scheduling",
    "Two-factor and passwords",
    "Web address and sessions",
    "Active Directory",
    "Microsoft 365",
    "Google Workspace",
    "Cloudflare",
    "Logs and monitoring",
)


def get_spec(key: str) -> SettingSpec:
    try:
        return SETTINGS[key]
    except KeyError:
        raise KeyError(
            f"unknown setting {key!r}; declare it in c2w.settings_spec before use"
        ) from None


def specs_by_category() -> dict[str, list[SettingSpec]]:
    """Grouped and ordered for rendering the settings page."""
    grouped: dict[str, list[SettingSpec]] = {}
    for spec in _SPECS:
        grouped.setdefault(spec.category, []).append(spec)
    ordered = {name: grouped.pop(name) for name in CATEGORY_ORDER if name in grouped}
    ordered.update(grouped)  # anything new, so a missing entry is never dropped
    return ordered

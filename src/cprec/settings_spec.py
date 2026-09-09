"""The catalogue of runtime settings.

Every tunable the platform has is declared here and stored in the database, not
in an env file.  This module holds only the *schema* -- name, type, default,
whether a brand may override it -- while values live in the ``app_settings``
table and are read through :mod:`cprec.settings`.

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
:class:`cprec.config.Bootstrap`.
"""

from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

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
    #: Whether ``cprec-admin settings reveal`` may print this in clear text.
    #: Off for everything by default: the CLI must never become a way to dump
    #: CommPeak or Wasabi credentials.  Opt in only for values that exist to be
    #: handed to another process, and say why in the description.
    shell_exportable: bool = False


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


def _log_level(value: Any) -> None:
    if str(value).upper() not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        raise ValueError("must be DEBUG, INFO, WARNING or ERROR")


_SPECS: Final[tuple[SettingSpec, ...]] = (
    # -- General -----------------------------------------------------------
    SettingSpec(
        key="core.base_url",
        type=SettingType.STRING,
        default="http://localhost:8000",
        category="General",
        label="Public web address",
        description="Where this system is reachable. Used to build links in "
        "alerts and to complete single sign-on.",
    ),
    SettingSpec(
        key="core.environment",
        type=SettingType.STRING,
        default="production",
        category="General",
        label="Environment",
        description="Label for this installation: production, staging or dev.",
        restart_required=True,
    ),
    SettingSpec(
        key="core.session_ttl_seconds",
        type=SettingType.INT,
        default=8 * 3600,
        category="General",
        label="Stay signed in for",
        description="How long a sign-in lasts before it has to be repeated.",
        unit="seconds",
        validator=_positive,
    ),
    # -- CommPeak (source) -------------------------------------------------
    SettingSpec(
        key="source.read_only",
        type=SettingType.BOOL,
        default=True,
        category="CommPeak (source)",
        label="Never write to CommPeak",
        description="This system only ever reads from CommPeak. The ability to "
        "write or delete there is not present in the code at all, so this "
        "switch is a statement of intent rather than something to turn off.",
    ),
    SettingSpec(
        key="source.concurrency_per_connection",
        type=SettingType.INT,
        default=5,
        category="CommPeak (source)",
        label="Simultaneous downloads per CommPeak account",
        description="CommPeak recommends 5 and throttles above it, so higher "
        "values make a migration slower, not faster.",
        unit="at a time",
        validator=_commpeak_concurrency,
    ),
    SettingSpec(
        key="source.list_page_size",
        type=SettingType.INT,
        default=1000,
        category="CommPeak (source)",
        label="Recordings listed per request",
        description="How many objects to ask CommPeak for at once while "
        "scanning a bucket.",
        unit="objects",
        validator=_positive,
    ),
    SettingSpec(
        key="source.incremental_poll_seconds",
        type=SettingType.INT,
        default=300,
        category="CommPeak (source)",
        label="Look for new recordings every",
        description="How often to check CommPeak for recordings that have "
        "appeared since the last scan.",
        unit="seconds",
        validator=_positive,
        brand_overridable=True,
    ),
    SettingSpec(
        key="source.incremental_overlap_hours",
        type=SettingType.INT,
        default=3,
        category="CommPeak (source)",
        label="Re-check the most recent",
        description="A call that starts at 10:59 and runs for ten minutes is "
        "filed under 10:00 well after that hour has passed. Re-scanning a few "
        "hours of already-seen time is what stops those being missed.",
        unit="hours",
        validator=_non_negative,
    ),
    # -- Archiving ---------------------------------------------------------
    SettingSpec(
        key="transfer.enabled",
        type=SettingType.BOOL,
        default=False,
        category="Archiving",
        label="Copy recordings to the archive",
        description="Turn on once archive storage is configured and tested. "
        "Recordings are still discovered and matched to calls while this is "
        "off, so nothing is lost and nothing needs re-scanning later.",
    ),
    SettingSpec(
        key="transfer.concurrency_global",
        type=SettingType.INT,
        default=20,
        category="Archiving",
        label="Total simultaneous transfers",
        description="Across every account and organisation.",
        unit="at a time",
        validator=_concurrency,
    ),
    SettingSpec(
        key="transfer.concurrency_per_destination",
        type=SettingType.INT,
        default=10,
        category="Archiving",
        label="Simultaneous uploads per archive",
        description="Upload limit for a single archive destination.",
        unit="at a time",
        validator=_concurrency,
    ),
    SettingSpec(
        key="transfer.concurrency_per_brand",
        type=SettingType.INT,
        default=5,
        category="Archiving",
        label="Simultaneous transfers per organisation",
        description="Stops one company's backfill consuming the whole server.",
        unit="at a time",
        validator=_concurrency,
    ),
    SettingSpec(
        key="transfer.bandwidth_limit_mbps",
        type=SettingType.FLOAT,
        default=0.0,
        category="Archiving",
        label="Bandwidth limit",
        description="Set this when recordings are pulled over the same "
        "connection that carries live calls.",
        unit="Mbit/s (0 = unlimited)",
        validator=_non_negative,
    ),
    SettingSpec(
        key="transfer.multipart_threshold_bytes",
        type=SettingType.INT,
        default=16 * 1024 * 1024,
        category="Archiving",
        label="Use multi-part upload for files above",
        description="Larger recordings are uploaded in pieces so an "
        "interrupted transfer can resume instead of starting again.",
        unit="bytes",
        validator=_positive,
    ),
    SettingSpec(
        key="transfer.multipart_chunk_bytes",
        type=SettingType.INT,
        default=16 * 1024 * 1024,
        category="Archiving",
        label="Size of each upload piece",
        description="Increased automatically for very large recordings to stay "
        "within the 10,000-piece limit.",
        unit="bytes",
        validator=_positive,
    ),
    SettingSpec(
        key="transfer.max_attempts",
        type=SettingType.INT,
        default=5,
        category="Archiving",
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
        category="Archiving",
        label="Wait between retries",
        description="Waiting periods before each new attempt, applied with a "
        "little randomness so a shared outage does not make every recording "
        "retry at the same instant.",
        unit="seconds",
    ),
    SettingSpec(
        key="transfer.job_claim_batch",
        type=SettingType.INT,
        default=8,
        category="Archiving",
        label="Recordings a worker takes at once",
        description="How much work each worker picks up per round.",
        unit="at a time",
        validator=_positive,
    ),
    SettingSpec(
        key="transfer.job_lease_seconds",
        type=SettingType.INT,
        default=900,
        category="Archiving",
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
        category="Archiving",
        label="Save call details beside each recording",
        description="Writes a small file next to every archived recording "
        "describing the call it belongs to. If this database were ever lost, "
        "the archive would still say who called whom and when.",
        brand_overridable=True,
    ),
    # -- Retention ---------------------------------------------------------
    SettingSpec(
        key="retention.offload_after_days",
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
        category="Notifications",
        label="Send alerts",
        description="Master switch for Telegram and Slack notifications.",
    ),
    SettingSpec(
        key="alerts.dedupe_window_seconds",
        type=SettingType.INT,
        default=1800,
        category="Notifications",
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
        category="Notifications",
        label="Telegram bot token",
        description="From @BotFather. Stored encrypted and never shown again.",
        sensitive=True,
        brand_overridable=True,
    ),
    SettingSpec(
        key="alerts.telegram_chat_id",
        type=SettingType.STRING,
        default="",
        category="Notifications",
        label="Telegram chat",
        description="The group or channel that alerts are posted to.",
        brand_overridable=True,
    ),
    SettingSpec(
        key="alerts.slack_webhook_url",
        type=SettingType.SECRET,
        default="",
        category="Notifications",
        label="Slack webhook address",
        description="An incoming-webhook address from your Slack workspace. "
        "Stored encrypted and never shown again.",
        sensitive=True,
        brand_overridable=True,
    ),
    SettingSpec(
        key="alerts.daily_digest_hour",
        type=SettingType.INT,
        default=8,
        category="Notifications",
        label="Send the daily summary at",
        description="A once-a-day digest of what was archived and what failed.",
        unit=":00 UTC",
        validator=_hour,
    ),
    # -- Scheduling --------------------------------------------------------
    SettingSpec(
        key="schedule.reconcile_hour",
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
        type=SettingType.INT,
        default=300,
        category="Scheduling",
        label="Fetch new call records every",
        description="How often to ask CommPeak for calls it has logged.",
        unit="seconds",
        validator=_positive,
    ),
    # -- Logging and metrics -----------------------------------------------
    SettingSpec(
        key="observability.log_level",
        type=SettingType.STRING,
        default="INFO",
        category="Logging and metrics",
        label="Log detail",
        description="DEBUG, INFO, WARNING or ERROR.",
        validator=_log_level,
        restart_required=True,
    ),
    SettingSpec(
        key="observability.log_json",
        type=SettingType.BOOL,
        default=True,
        category="Logging and metrics",
        label="Write logs as JSON",
        description="Easier for log collectors to read; harder for a person. "
        "Turn off when reading the log by eye.",
        restart_required=True,
    ),
    SettingSpec(
        key="observability.metrics_enabled",
        type=SettingType.BOOL,
        default=True,
        category="Logging and metrics",
        label="Publish Prometheus metrics",
        description="Exposes transfer counts and timings for monitoring.",
        restart_required=True,
    ),
    # -- Sign-in and access ------------------------------------------------
    SettingSpec(
        key="auth.local_accounts_enabled",
        type=SettingType.BOOL,
        default=True,
        category="Sign-in and access",
        label="Allow email and password sign-in",
        description="Switch off once staff sign in through your directory. The "
        "main administrator keeps password sign-in either way, as a way back in "
        "if single sign-on breaks.",
    ),
    SettingSpec(
        key="auth.oidc_entra_enabled",
        type=SettingType.BOOL,
        default=False,
        category="Sign-in and access",
        label="Sign in with Microsoft Entra ID",
        description="Lets staff use their Microsoft work account. Directory "
        "groups can then decide what each person may do.",
    ),
    SettingSpec(
        key="auth.oidc_entra_tenant_id",
        type=SettingType.STRING,
        default="",
        category="Sign-in and access",
        label="Entra directory (tenant) ID",
        description="From the Microsoft Entra admin centre.",
    ),
    SettingSpec(
        key="auth.oidc_entra_client_id",
        type=SettingType.STRING,
        default="",
        category="Sign-in and access",
        label="Entra application (client) ID",
        description="From the app registration you create for this system.",
    ),
    SettingSpec(
        key="auth.oidc_entra_client_secret",
        type=SettingType.SECRET,
        default="",
        category="Sign-in and access",
        label="Entra client secret",
        description="Stored encrypted and never shown again.",
        sensitive=True,
    ),
    SettingSpec(
        key="auth.oidc_google_enabled",
        type=SettingType.BOOL,
        default=False,
        category="Sign-in and access",
        label="Sign in with Google Workspace",
        description="Lets staff use their Google work account.",
    ),
    SettingSpec(
        key="auth.oidc_google_client_id",
        type=SettingType.STRING,
        default="",
        category="Sign-in and access",
        label="Google client ID",
        description="From the Google Cloud console.",
    ),
    SettingSpec(
        key="auth.oidc_google_client_secret",
        type=SettingType.SECRET,
        default="",
        category="Sign-in and access",
        label="Google client secret",
        description="Stored encrypted and never shown again.",
        sensitive=True,
    ),
    # -- Developer tools ---------------------------------------------------
    SettingSpec(
        key="integrations.shadcn_mcp_token",
        type=SettingType.SECRET,
        default="",
        category="Developer tools",
        label="shadcn.io access token",
        description="Kept here so it lives in the database rather than in a "
        "configuration file. Nothing in this system reads it at runtime; it is "
        "handed to development tooling on demand.",
        sensitive=True,
        shell_exportable=True,
    ),
)

SETTINGS: Final[dict[str, SettingSpec]] = {s.key: s for s in _SPECS}

#: Order the settings page presents categories in: the things an operator
#: touches while getting started first, machinery afterwards.
CATEGORY_ORDER: Final[tuple[str, ...]] = (
    "General",
    "CommPeak (source)",
    "Archiving",
    "Retention",
    "Playback and downloads",
    "Notifications",
    "Scheduling",
    "Sign-in and access",
    "Logging and metrics",
    "Developer tools",
)


def get_spec(key: str) -> SettingSpec:
    try:
        return SETTINGS[key]
    except KeyError:
        raise KeyError(
            f"unknown setting {key!r}; declare it in cprec.settings_spec before use"
        ) from None


def specs_by_category() -> dict[str, list[SettingSpec]]:
    """Grouped and ordered for rendering the settings page."""
    grouped: dict[str, list[SettingSpec]] = {}
    for spec in _SPECS:
        grouped.setdefault(spec.category, []).append(spec)
    ordered = {name: grouped.pop(name) for name in CATEGORY_ORDER if name in grouped}
    ordered.update(grouped)  # anything new, so a missing entry is never dropped
    return ordered

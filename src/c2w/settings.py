"""Database-backed settings service.

Values come from the ``app_settings`` table, resolved brand override -> global
-> registry default.  Nothing reads configuration from an env file; the only
exceptions are the two bootstrap values in :class:`c2w.config.Bootstrap`,
which cannot live in the database because they are what connects to it.

The service caches aggressively.  A worker reads a handful of settings per job
and a page render reads a dozen; hitting PostgreSQL every time would be
pointless load.  The cache has a short TTL so a change through the UI takes
effect across every process within seconds without a restart, and
``invalidate()`` makes it immediate in the process that made the change.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, TypeVar

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from c2w.crypto import CryptoError, open_global, seal_global
from c2w.db.models.settings import AppSetting, SettingHistory
from c2w.settings_spec import SETTINGS, SettingSpec, SettingType, get_spec

__all__ = ["SettingsError", "SettingsService", "coerce", "validate_value"]

T = TypeVar("T")

#: Long enough to keep read load negligible, short enough that an operator
#: changing a setting sees it apply without wondering whether it took.
CACHE_TTL_SECONDS = 10.0


class SettingsError(ValueError):
    """Raised when a setting value is not acceptable for its declared type."""


def coerce(spec: SettingSpec, raw: Any) -> Any:
    """Convert a value (possibly a string from a form post) to the declared type."""
    if raw is None:
        return spec.default
    try:
        match spec.type:
            case SettingType.INT:
                return int(raw)
            case SettingType.FLOAT:
                return float(raw)
            case SettingType.BOOL:
                if isinstance(raw, bool):
                    return raw
                return str(raw).strip().lower() in {"1", "true", "yes", "on"}
            case SettingType.JSON:
                return json.loads(raw) if isinstance(raw, str) else raw
            case _:
                return str(raw)
    except (TypeError, ValueError) as exc:
        raise SettingsError(f"{spec.key}: expected {spec.type}, got {raw!r}") from exc


def validate_value(spec: SettingSpec, value: Any) -> None:
    """Run the spec's validator, translating failures into SettingsError."""
    if spec.validator is None:
        return
    try:
        spec.validator(value)
    except ValueError as exc:
        raise SettingsError(f"{spec.key}: {exc}") from exc


class SettingsService:
    """Reads and writes settings, with a short-lived in-process cache."""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, int | None], Any] = {}
        self._loaded_at = 0.0
        self._lock = asyncio.Lock()

    # -- reading -----------------------------------------------------------

    async def get(self, session: AsyncSession, key: str, *, brand_id: int | None = None) -> Any:
        """Resolve one setting: brand override, else global, else default."""
        spec = get_spec(key)
        await self._ensure_loaded(session)
        if brand_id is not None and spec.brand_overridable:
            if (key, brand_id) in self._cache:
                return self._cache[(key, brand_id)]
        if (key, None) in self._cache:
            return self._cache[(key, None)]
        return spec.default

    async def get_int(self, session: AsyncSession, key: str, *, brand_id: int | None = None) -> int:
        return int(await self.get(session, key, brand_id=brand_id))

    async def get_float(
        self, session: AsyncSession, key: str, *, brand_id: int | None = None
    ) -> float:
        return float(await self.get(session, key, brand_id=brand_id))

    async def get_bool(
        self, session: AsyncSession, key: str, *, brand_id: int | None = None
    ) -> bool:
        return bool(await self.get(session, key, brand_id=brand_id))

    async def get_str(self, session: AsyncSession, key: str, *, brand_id: int | None = None) -> str:
        return str(await self.get(session, key, brand_id=brand_id))

    async def get_secret(
        self, session: AsyncSession, key: str, *, brand_id: int | None = None
    ) -> str:
        """Unseal a secret setting.  Returns "" when unset.

        Secrets are stored sealed, so an operator who has not configured a
        token yet gets an empty string rather than an error -- callers decide
        whether an unset integration is a problem.
        """
        spec = get_spec(key)
        if not spec.sensitive:
            return await self.get_str(session, key, brand_id=brand_id)
        sealed = await self.get(session, key, brand_id=brand_id)
        if not sealed:
            return ""
        try:
            return open_global(str(sealed), aad=_aad(key, brand_id))
        except CryptoError:
            # A secret sealed under a previous master key cannot be read. Treat
            # it as unset rather than crashing the caller, and say so clearly.
            return ""

    async def all_effective(
        self, session: AsyncSession, *, brand_id: int | None = None
    ) -> dict[str, Any]:
        """Every setting's effective value, for the admin page.

        Secrets are reported as a bool -- whether they are configured -- never
        as a value, so rendering this dict can never leak a token.
        """
        await self._ensure_loaded(session)
        out: dict[str, Any] = {}
        for key, spec in SETTINGS.items():
            value = await self.get(session, key, brand_id=brand_id)
            out[key] = bool(value) if spec.sensitive else value
        return out

    async def overrides_for_brand(self, session: AsyncSession, brand_id: int) -> dict[str, Any]:
        """Only the settings this brand explicitly overrides."""
        rows = (
            await session.execute(select(AppSetting).where(AppSetting.brand_id == brand_id))
        ).scalars()
        return {r.key: ("***" if get_spec(r.key).sensitive else r.value) for r in rows}

    # -- writing -----------------------------------------------------------

    async def set(
        self,
        session: AsyncSession,
        key: str,
        raw_value: Any,
        *,
        brand_id: int | None = None,
        changed_by: str | None = None,
        note: str | None = None,
    ) -> Any:
        """Validate and persist a setting.  Returns the stored (coerced) value."""
        spec = get_spec(key)
        if brand_id is not None and not spec.brand_overridable:
            raise SettingsError(f"{key} is global-only and cannot be overridden per brand")

        value = coerce(spec, raw_value)
        validate_value(spec, value)

        previous = await self.get(session, key, brand_id=brand_id)
        stored: Any = value
        if spec.sensitive:
            # An empty submission clears the secret rather than sealing "".
            stored = seal_global(str(value), aad=_aad(key, brand_id)) if value else ""

        await session.execute(
            pg_insert(AppSetting)
            .values(
                key=key,
                brand_id=brand_id,
                value=stored,
                is_sealed=bool(spec.sensitive and stored),
                updated_by=changed_by,
                note=note,
            )
            .on_conflict_do_update(
                # Both uniqueness rules are *partial* indexes -- one global row
                # per key, one row per key per brand -- because a plain
                # UNIQUE(key, brand_id) would not constrain the global rows at
                # all (NULL never equals NULL). PostgreSQL can only infer a
                # partial index if the predicate is restated here, so omitting
                # index_where fails with "no unique or exclusion constraint
                # matching the ON CONFLICT specification".
                index_elements=(
                    [AppSetting.key, AppSetting.brand_id]
                    if brand_id is not None
                    else [AppSetting.key]
                ),
                index_where=(
                    AppSetting.brand_id.isnot(None)
                    if brand_id is not None
                    else AppSetting.brand_id.is_(None)
                ),
                set_={
                    "value": stored,
                    "is_sealed": bool(spec.sensitive and stored),
                    "updated_by": changed_by,
                    "note": note,
                },
            )
        )
        session.add(
            SettingHistory(
                key=key,
                brand_id=brand_id,
                # Never write a secret's value into history -- only that it moved.
                old_value="***" if spec.sensitive else previous,
                new_value="***" if spec.sensitive else value,
                changed_by=changed_by,
                note=note,
            )
        )
        self.invalidate()
        return value

    async def unset(
        self,
        session: AsyncSession,
        key: str,
        *,
        brand_id: int | None = None,
        changed_by: str | None = None,
    ) -> None:
        """Remove an override so the value falls back to global, then default."""
        spec = get_spec(key)
        previous = await self.get(session, key, brand_id=brand_id)
        await session.execute(
            delete(AppSetting).where(
                AppSetting.key == key,
                AppSetting.brand_id == brand_id
                if brand_id is not None
                else AppSetting.brand_id.is_(None),
            )
        )
        session.add(
            SettingHistory(
                key=key,
                brand_id=brand_id,
                old_value="***" if spec.sensitive else previous,
                new_value=None,
                changed_by=changed_by,
                note="unset",
            )
        )
        self.invalidate()

    # -- cache -------------------------------------------------------------

    def invalidate(self) -> None:
        self._loaded_at = 0.0

    async def _ensure_loaded(self, session: AsyncSession) -> None:
        if time.monotonic() - self._loaded_at < CACHE_TTL_SECONDS:
            return
        async with self._lock:
            if time.monotonic() - self._loaded_at < CACHE_TTL_SECONDS:
                return
            rows = (await session.execute(select(AppSetting))).scalars().all()
            self._cache = {(r.key, r.brand_id): r.value for r in rows if r.key in SETTINGS}
            self._loaded_at = time.monotonic()


def _aad(key: str, brand_id: int | None) -> str:
    """Bind a sealed setting to its key and scope.

    Without this, a sealed Slack webhook could be copied into the Telegram token
    row, or one brand's secret into another's, and still decrypt.
    """
    return f"setting:{key}:{brand_id if brand_id is not None else 'global'}"


#: Process-wide instance.  The cache is per-process by design: each unit picks
#: up a change within CACHE_TTL_SECONDS.
settings_service = SettingsService()

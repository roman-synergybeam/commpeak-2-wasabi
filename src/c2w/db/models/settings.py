"""Settings storage.

All runtime configuration lives here rather than in env files, so it can be
changed through the admin UI, is versioned with an audit trail, and is identical
across every process on the host without a redeploy.

Resolution order is brand override -> global row -> registry default, so a brand
only stores the settings it actually differs on.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from c2w.db.base import Base, IdMixin


class AppSetting(Base, IdMixin):
    """One configured setting value, global or brand-scoped.

    ``brand_id IS NULL`` is the global row.  A unique index cannot enforce
    "one global row per key" on a nullable column, so the migration adds two
    partial unique indexes instead -- one for global, one per brand.
    """

    __tablename__ = "app_settings"
    __table_args__ = (Index("ix_app_settings_key", "key"),)

    key: Mapped[str] = mapped_column(String(120), nullable=False)
    brand_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("brands.id", ondelete="CASCADE")
    )
    #: JSONB keeps one column usable for ints, bools, strings and lists without
    #: string-parsing on every read.
    value: Mapped[dict | list | str | int | float | bool | None] = mapped_column(JSONB)
    #: Sealed values carry ciphertext in ``value`` and this flag set, so a
    #: reader never mistakes ciphertext for a usable value.
    is_sealed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text(
            "now()",
        ),
    )
    updated_by: Mapped[str | None] = mapped_column(String(200))
    note: Mapped[str | None] = mapped_column(Text)


class SettingHistory(Base, IdMixin):
    """Append-only record of settings changes.

    Configuration changes are a frequent root cause of "it stopped working at
    some point", and a bandwidth cap or retention window changing silently is
    exactly the kind of thing nobody remembers doing.  Secret values are never
    recorded here -- only that they changed.
    """

    __tablename__ = "setting_history"
    __table_args__ = (Index("ix_setting_history_key_at", "key", "at"),)

    key: Mapped[str] = mapped_column(String(120), nullable=False)
    brand_id: Mapped[int | None] = mapped_column(BigInteger)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    old_value: Mapped[dict | list | str | int | float | bool | None] = mapped_column(JSONB)
    new_value: Mapped[dict | list | str | int | float | bool | None] = mapped_column(JSONB)
    changed_by: Mapped[str | None] = mapped_column(String(200))
    note: Mapped[str | None] = mapped_column(Text)

"""Bootstrap configuration -- deliberately tiny.

Everything the platform can configure lives in the database (see
:mod:`cprec.settings_spec` and :mod:`cprec.settings`).  Exactly two values
cannot, because they are what a process needs in order to reach the database
and to make sense of what it finds there:

* ``CPREC_DATABASE_URL`` -- where the settings are.
* ``CPREC_MASTER_KEY`` -- the key that unseals stored credentials.  Without it
  the rows are unreadable ciphertext.

Both are supplied by the systemd unit: the URL as an ``Environment=`` line and
the key via ``LoadCredential=``, which puts it in a file readable only by the
service and keeps it out of the process environment and out of
``systemctl show``.  There is no ``.env`` file, and no env-file fallback -- a
setting that appears in two places eventually disagrees with itself.

``CPREC_PLATFORM_DATABASE_URL`` is optional and only differs from the main URL
in the role it connects as: workers need the ``BYPASSRLS`` platform role because
transfers legitimately span brands.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, PostgresDsn, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["Bootstrap", "get_bootstrap", "redact_dsn"]


class Bootstrap(BaseSettings):
    """The minimum needed to start. No env files, no defaults for secrets."""

    model_config = SettingsConfigDict(
        env_prefix="CPREC_",
        # No env_file: settings belong in the database.
        extra="ignore",
    )

    database_url: PostgresDsn = Field(
        default="postgresql+asyncpg://cprec@127.0.0.1:5432/cprec"  # type: ignore[arg-type]
    )
    #: Connects as the BYPASSRLS platform role. Falls back to database_url,
    #: which is correct for single-role development but not for production.
    platform_database_url: PostgresDsn | None = None

    master_key: SecretStr = SecretStr("")
    #: systemd LoadCredential= hands us a path, not a value. Preferred over
    #: master_key because the secret never enters the environment.
    master_key_file: Path | None = None

    @field_validator("master_key_file", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        return None if v in ("", None) else v

    @model_validator(mode="after")
    def _read_key_file(self) -> Bootstrap:
        """Load the master key from its credential file when one is given."""
        if self.master_key_file is not None and not self.master_key.get_secret_value():
            try:
                text = self.master_key_file.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise ValueError(
                    f"cannot read CPREC_MASTER_KEY_FILE at {self.master_key_file}: {exc}"
                ) from exc
            object.__setattr__(self, "master_key", SecretStr(text))
        return self

    @property
    def effective_platform_url(self) -> str:
        return str(self.platform_database_url or self.database_url)

    @property
    def sync_database_url(self) -> str:
        """psycopg URL, for Alembic and the few sync-only maintenance paths."""
        return str(self.database_url).replace("+asyncpg", "+psycopg")

    def masked(self) -> dict[str, str]:
        """Safe to log: no secret material."""
        return {
            "database_url": redact_dsn(str(self.database_url)),
            "platform_database_url": redact_dsn(self.effective_platform_url),
            "master_key": "set" if self.master_key.get_secret_value() else "MISSING",
        }


def redact_dsn(dsn: str) -> str:
    """Strip the password from a DSN so it is safe to log."""
    if "@" not in dsn or "//" not in dsn:
        return dsn
    scheme, _, rest = dsn.partition("//")
    creds, _, host = rest.rpartition("@")
    if not creds:
        return dsn
    return f"{scheme}//{creds.split(':', 1)[0]}:***@{host}"


@lru_cache(maxsize=1)
def get_bootstrap() -> Bootstrap:
    return Bootstrap()

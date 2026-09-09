"""Structured logging.

Logs are JSON in production so journald output can be shipped and queried by
tenant/recording/job.  Two rules are enforced here rather than left to callers:

* the master key and any ``*_sealed`` value is redacted, because a stack trace
  or a debug log is exactly how credentials leak;
* every record carries the service name, so ``journalctl`` output from four
  unit types stays separable.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


_REDACT_KEYS = re.compile(r"(secret|password|token|_sealed|master_key|access_key)", re.IGNORECASE)
_REDACTED = "***"


def _redact(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Strip anything that looks like a credential from every log record."""
    for key in list(event_dict):
        if _REDACT_KEYS.search(key):
            event_dict[key] = _REDACTED
    return event_dict


def configure_logging(service: str, *, level_name: str = "INFO", json_output: bool = True) -> None:
    """Set up logging.

    Takes plain arguments rather than reading configuration itself: logging must
    work before the database is reachable, and the log settings live in the
    database.  Processes call this once at startup with defaults, then again via
    :func:`reconfigure_from_settings` once the database is available.
    """
    level = getattr(logging, level_name.upper(), logging.INFO)

    shared = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _redact,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    renderer = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )

    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=level)
    for noisy in ("botocore", "aiobotocore", "boto3", "urllib3", "sqlalchemy.engine"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    structlog.contextvars.bind_contextvars(service=service, pid=os.getpid())


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


async def reconfigure_from_settings(service: str, session: AsyncSession) -> None:
    """Re-apply logging using the values stored in the database."""
    from cprec.settings import settings_service

    configure_logging(
        service,
        level_name=await settings_service.get_str(session, "observability.log_level"),
        json_output=await settings_service.get_bool(session, "observability.log_json"),
    )

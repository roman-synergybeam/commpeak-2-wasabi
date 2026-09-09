"""Roles and permissions.

Two decisions worth stating, because both are requirements rather than
implementation details:

``recordings.play`` and ``recordings.download`` are separate permissions.  Some
organisations permit staff to listen to a call for quality review but forbid
taking a copy off the platform, and collapsing the two would make that policy
unexpressible.

``recordings.delete`` is granted to nobody below ``SUPER_ADMIN``, and deleting
at the CommPeak source additionally requires two settings to be turned on.
Recording deletion there is irreversible.
"""

from __future__ import annotations

import enum
from typing import Final

from cprec.db.models.auth import Role

__all__ = ["ROLE_PERMISSIONS", "Permission", "has_permission", "permissions_for"]


class Permission(enum.StrEnum):
    RECORDINGS_VIEW = "recordings.view"
    RECORDINGS_PLAY = "recordings.play"
    RECORDINGS_DOWNLOAD = "recordings.download"
    RECORDINGS_DELETE = "recordings.delete"
    RECORDINGS_EXPORT = "recordings.export"

    CDR_VIEW = "cdr.view"
    CDR_EXPORT = "cdr.export"

    STORAGE_VIEW = "storage.view"
    STORAGE_MANAGE = "storage.manage"

    SYNC_VIEW = "sync.view"
    SYNC_MANAGE = "sync.manage"

    USERS_MANAGE = "users.manage"
    INTEGRATIONS_MANAGE = "integrations.manage"
    SETTINGS_VIEW = "settings.view"
    SETTINGS_MANAGE = "settings.manage"
    AUDIT_VIEW = "audit.view"
    BRANDS_MANAGE = "brands.manage"


_ALL: Final[frozenset[Permission]] = frozenset(Permission)

ROLE_PERMISSIONS: Final[dict[Role, frozenset[Permission]]] = {
    Role.SUPER_ADMIN: _ALL,
    Role.TENANT_ADMIN: frozenset(_ALL - {Permission.BRANDS_MANAGE, Permission.RECORDINGS_DELETE}),
    Role.RECORDING_ADMIN: frozenset(
        {
            Permission.RECORDINGS_VIEW,
            Permission.RECORDINGS_PLAY,
            Permission.RECORDINGS_DOWNLOAD,
            Permission.RECORDINGS_EXPORT,
            Permission.CDR_VIEW,
            Permission.CDR_EXPORT,
            Permission.STORAGE_VIEW,
            Permission.SYNC_VIEW,
            Permission.SYNC_MANAGE,
            Permission.SETTINGS_VIEW,
            Permission.AUDIT_VIEW,
        }
    ),
    Role.SUPERVISOR: frozenset(
        {
            Permission.RECORDINGS_VIEW,
            Permission.RECORDINGS_PLAY,
            Permission.RECORDINGS_DOWNLOAD,
            Permission.CDR_VIEW,
            Permission.CDR_EXPORT,
            Permission.SYNC_VIEW,
        }
    ),
    # An agent may review their own calls but not take copies away.
    Role.AGENT: frozenset(
        {
            Permission.RECORDINGS_VIEW,
            Permission.RECORDINGS_PLAY,
            Permission.CDR_VIEW,
        }
    ),
    # An auditor's job is to inspect the trail, including who listened to what,
    # but not to remove anything from it.
    Role.AUDITOR: frozenset(
        {
            Permission.RECORDINGS_VIEW,
            Permission.RECORDINGS_PLAY,
            Permission.CDR_VIEW,
            Permission.CDR_EXPORT,
            Permission.AUDIT_VIEW,
            Permission.SYNC_VIEW,
            Permission.STORAGE_VIEW,
            Permission.SETTINGS_VIEW,
        }
    ),
    Role.READ_ONLY: frozenset({Permission.RECORDINGS_VIEW, Permission.CDR_VIEW}),
}


def permissions_for(role: Role | str) -> frozenset[Permission]:
    return ROLE_PERMISSIONS.get(Role(role), frozenset())


def has_permission(role: Role | str, permission: Permission) -> bool:
    return permission in permissions_for(role)

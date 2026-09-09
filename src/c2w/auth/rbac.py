"""Roles and permissions.

Two decisions worth stating, because both are requirements rather than
implementation details:

``recordings.play`` and ``recordings.download`` are separate permissions.  Some
organisations permit staff to listen to a call for quality review but forbid
taking a copy off the platform, and collapsing the two would make that policy
unexpressible.

``recordings.delete`` separates the two roles that matter: an admin has it, an
operator does not. It only ever removes the archive copy -- deleting at the
CommPeak source is not implemented at all, and CommPeak documents its deletions
as irreversible.
"""

from __future__ import annotations

import enum
from typing import Final

from c2w.db.models.auth import Role

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

#: Everything an operator does: find a call, hear it, take a copy of it.
_OPERATOR: Final[frozenset[Permission]] = frozenset(
    {
        Permission.RECORDINGS_VIEW,
        Permission.RECORDINGS_PLAY,
        Permission.RECORDINGS_DOWNLOAD,
        Permission.RECORDINGS_EXPORT,
        Permission.CDR_VIEW,
        Permission.CDR_EXPORT,
        Permission.SYNC_VIEW,
    }
)

ROLE_PERMISSIONS: Final[dict[Role, frozenset[Permission]]] = {
    # Runs the platform: every organisation, everything in each.
    Role.SUPER_ADMIN: _ALL,
    # Runs one organisation. The only role there that can delete a recording,
    # which is why it is the one that is handed out sparingly.
    Role.ADMIN: frozenset(_ALL - {Permission.BRANDS_MANAGE}),
    Role.OPERATOR: _OPERATOR,
}

def permissions_for(role: Role | str) -> frozenset[Permission]:
    return ROLE_PERMISSIONS.get(Role(role), frozenset())


def has_permission(role: Role | str, permission: Permission) -> bool:
    return permission in permissions_for(role)

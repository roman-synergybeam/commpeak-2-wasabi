"""Model registry.

Importing this package registers every table on ``Base.metadata``, which is
what Alembic autogeneration and the test fixtures rely on.
"""

from c2w.db.models import auth, core, settings

__all__ = ["auth", "core", "settings"]

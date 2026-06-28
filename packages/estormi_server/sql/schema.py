"""Public surface for the Estormi DB schema, split by role across three modules:

* :mod:`schema_init` — :data:`INIT_SQL`, the canonical base DDL (all tables);
* :mod:`schema_columns` — the additive column ALTERs and the pass that applies
  them (:func:`_apply_chunk_column_migrations`);
* :mod:`schema_migrations` — :data:`MIGRATION_SQL`, the post-column indexes,
  one-off backfills, and legacy-table drops.

The startup path applies them in order: ``INIT_SQL`` →
``_apply_chunk_column_migrations`` → ``MIGRATION_SQL``. This module re-exports
the names so callers keep importing them from ``estormi_server.sql.schema``.
"""

from __future__ import annotations

from estormi_server.sql.schema_columns import (
    CHUNK_COLUMN_MIGRATIONS,
    _apply_chunk_column_migrations,
)
from estormi_server.sql.schema_init import INIT_SQL
from estormi_server.sql.schema_migrations import MIGRATION_SQL

__all__ = [
    "INIT_SQL",
    "MIGRATION_SQL",
    "CHUNK_COLUMN_MIGRATIONS",
    "_apply_chunk_column_migrations",
]

"""SQLite schema (sql.schema) and read-side settings lookup (sql.connection) for the Estormi DB.

The shared connection itself lives in ``tools.py``.

Split out of the original ``tools.py`` so the bulk SQL is grouped by what
it does, not interleaved with the embedding / search code.
"""

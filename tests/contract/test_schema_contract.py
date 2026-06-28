"""Contracts that keep pytest databases aligned with the runtime schema."""

from __future__ import annotations

import aiosqlite
import pytest

from tests.helpers.database import apply_runtime_schema

pytestmark = [pytest.mark.integration, pytest.mark.contract]


class TestRuntimeSchemaContract:
    async def test_runtime_schema_is_idempotent_on_test_connection(self):
        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        try:
            await apply_runtime_schema(conn)
            await apply_runtime_schema(conn)
            cursor = await conn.execute(
                "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view')"
            )
            rows = await cursor.fetchall()
            await cursor.close()
        finally:
            await conn.close()

        objects = {(row["name"], row["type"]) for row in rows}
        expected = {
            ("chunks", "table"),
            ("settings", "table"),
        }
        assert expected <= objects

    async def test_fixture_schema_keeps_foreign_keys_active(self, db):
        cursor = await db.execute("PRAGMA foreign_keys")
        row = await cursor.fetchone()
        await cursor.close()

        assert row[0] == 1

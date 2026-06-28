"""Service layer behind the Google Calendar picker endpoints.

The router under :mod:`estormi_server.api.calendar_oauth` owns the OAuth flow
(URL minting, code exchange, the browser-redirect landing page) and the
secrets upload — request-scoped HTTP choreography with no reusable SQL. The
calendar *selection* and *group-type* state, however, is plain settings-table
JSON; reading and updating it lives here so it is unit-testable and shared
between the list and patch handlers.

The ``estormi_ingestion.google_calendar`` package is imported lazily inside the
functions so the test suite's ``patch`` of the ``auth`` / ``sync`` submodules
keeps resolving, and so import order stays cheap.
"""

from __future__ import annotations

import json

# The calendar ``group_type`` vocabulary lives in the canonical
# ``memory_core.labels`` source of truth; re-exported here so the Google-calendar
# router keeps importing it from the service layer rather than reaching into
# memory_core.
from memory_core.labels import GCAL_GROUP_TYPES  # noqa: F401


async def all_calendar_ids() -> list[str]:
    """Every calendar id Google exposes for the authenticated account.

    Used to materialize the "empty set means all selected" sentinel into an
    explicit set the moment the user deselects their first calendar (see
    :func:`apply_selection_update`). Builds the blocking googleapiclient
    service off the event loop. Raises ``HTTPException(401)`` when there are no
    credentials, matching the list endpoint's not-authenticated contract.
    """
    import asyncio  # noqa: PLC0415

    from fastapi import HTTPException  # noqa: PLC0415

    from estormi_ingestion.google_calendar import auth as gcal_auth  # noqa: PLC0415
    from estormi_ingestion.google_calendar import sync as gcal_sync  # noqa: PLC0415

    creds = gcal_auth.get_credentials()
    if creds is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    service = await asyncio.to_thread(gcal_sync._build_service, creds)
    items = await asyncio.to_thread(gcal_sync._list_user_calendars, service)
    return [c["id"] for c in items]


async def selected_ids(db) -> set[str]:
    """The explicitly-selected calendar id set from the settings JSON blob.

    An empty set is the "all calendars selected" sentinel. Missing rows or
    unparseable JSON behave as the empty set.
    """
    cur = await db.execute(
        "SELECT value FROM settings WHERE key = ?",
        ("google_calendar_selected_ids",),
    )
    row = await cur.fetchone()
    await cur.close()
    if not row or not row[0]:
        return set()
    try:
        val = json.loads(row[0])
        return {str(x) for x in val} if isinstance(val, list) else set()
    except (ValueError, TypeError):
        return set()


async def group_types(db) -> dict[str, str]:
    """Read the ``{calendar_id: group_type}`` map stored as a JSON blob in the
    settings table (single row keyed by ``google_calendar_group_types``).
    Missing rows or unparseable JSON behave as an empty map — the SPA renders
    ``unknown`` chips."""
    cur = await db.execute(
        "SELECT value FROM settings WHERE key = ?",
        ("google_calendar_group_types",),
    )
    row = await cur.fetchone()
    await cur.close()
    if not row or not row[0]:
        return {}
    try:
        val = json.loads(row[0])
        if not isinstance(val, dict):
            return {}
        return {str(k): str(v) for k, v in val.items()}
    except (ValueError, TypeError):
        return {}


async def apply_selection_update(
    db, calendar_id: str, selected: bool | None, group_type: str | None
) -> int:
    """Persist a partial update for one calendar row and re-tag its chunks.

    Mirrors the SPA's two controls: ``selected`` (toggle) and ``group_type``
    (chip). Either or both may be present. Returns the number of already-ingested
    chunks re-tagged (0 when ``group_type`` is unchanged). Caller validates
    ``group_type`` against :data:`GCAL_GROUP_TYPES` before calling.
    """
    from estormi_server.storage.chunk_admin import retag_chunks  # noqa: PLC0415
    from estormi_server.storage.tools import get_write_lock  # noqa: PLC0415

    # Compute the new selection set before taking the lock — these are reads.
    new_ids = None
    if selected is not None:
        ids = sorted(await selected_ids(db))
        if selected:
            # An empty stored set is the "all calendars selected" sentinel (see
            # the list endpoint and sync.cal_ids). Selecting a calendar that is
            # already on under that sentinel is a no-op — leave the set empty.
            # Materializing [calendar_id] would read as "sync ONLY this one" and
            # silently deselect every other calendar.
            if ids and calendar_id not in ids:
                ids.append(calendar_id)
        else:
            if ids:
                if calendar_id in ids:
                    ids.remove(calendar_id)
                if not ids:
                    # Removing the last explicitly-selected calendar empties the
                    # set, which would read back as the "all calendars selected"
                    # sentinel (see the list endpoint and sync.cal_ids) and
                    # silently re-enable every calendar — the opposite of intent.
                    # Materialize the full list minus this id instead.
                    ids = sorted(c for c in await all_calendar_ids() if c != calendar_id)
            else:
                # An empty stored set is the "all calendars selected" sentinel
                # (see the list endpoint and sync.cal_ids). Deselecting the
                # first calendar from that state must materialize the full list
                # minus this id — persisting [] would still read as "all
                # selected" and the calendar would keep syncing.
                ids = sorted(c for c in await all_calendar_ids() if c != calendar_id)
        new_ids = ids

    new_groups = None
    if group_type is not None:
        new_groups = await group_types(db)
        new_groups[calendar_id] = group_type

    # Serialise both INSERTs + the commit on the shared write lock so a
    # concurrent leaf writer's commit can't tear them. Leaf — retag_chunks below
    # takes the lock independently, after this releases. See ``tools._write_lock``.
    async with get_write_lock():
        if new_ids is not None:
            await db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                ("google_calendar_selected_ids", json.dumps(new_ids)),
            )
        if new_groups is not None:
            await db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                ("google_calendar_group_types", json.dumps(new_groups)),
            )
        await db.commit()

    # Re-tag events already ingested from this calendar so the new group_type
    # reaches search and the briefing without waiting for the next gcal sync.
    if group_type is not None:
        return (await retag_chunks("gcal", calendar_id, group_type))["retagged"]
    return 0

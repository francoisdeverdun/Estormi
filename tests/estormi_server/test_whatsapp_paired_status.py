"""Regression: a paired-but-IDLE WhatsApp bridge surfaces as Connected.

The WhatsApp bot runs in bounded nightly bursts (idle mode), so its
``connected`` flag flips back to false between syncs. Before commit
2da174a the only signal the Python layer exposed to the UI was
``connected``, which left a paired bridge showing as "Awaiting scan" most
of the time.

The fix adds a sticky ``paired`` bit driven by the ``wa.paired`` marker
file the Rust sidecar writes on the Connected event (cleared on LoggedOut
and on reset). The Python API:

  * **passes through** the sidecar's ``paired`` field on
    ``GET /api/whatsapp/status`` (the SPA's ``SourcesPanel`` reads it and
    flips the row to "Connected" when ``paired || connected``);
  * **falls back** to ``{"connected": False, "paired": False,
    "session_state": "UNAVAILABLE"}`` on the ``/settings/overview``
    endpoint when the sidecar is unreachable — so a missing sidecar
    can't accidentally read as Connected.

These tests pin the passthrough and the fallback.

The "paired" bit itself lives in the Rust sidecar as a marker file next
to ``wa.db`` (``apps/estormi-macos/src/whatsapp/``); it is *not* a settings
table row. We exercise the Python contract — what the SPA actually
consumes — by faking the sidecar response.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.regression]


def _fake_response(json_body: dict, status_code: int = 200) -> MagicMock:
    """A stand-in for ``httpx.Response`` exposing only ``.json()``/``.status_code``."""
    r = MagicMock()
    r.status_code = status_code
    r.json = MagicMock(return_value=json_body)
    return r


class _FakeAsyncClient:
    """Drop-in for ``httpx.AsyncClient`` that returns a fixed JSON body or raises."""

    def __init__(self, *, response=None, raise_exc=None):
        self._response = response
        self._raise = raise_exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None):
        if self._raise is not None:
            raise self._raise
        return self._response

    async def post(self, url, headers=None):
        if self._raise is not None:
            raise self._raise
        return self._response


async def test_status_endpoint_passes_paired_through_when_idle():
    """``GET /api/whatsapp/status`` returns the sidecar's ``paired`` field unchanged.

    The sidecar reports ``connected:false, paired:true, session_state:IDLE``
    between nightly bursts. The Python passthrough must preserve ``paired``
    so the SPA can decide "still set up" without the bridge being live.
    """
    from estormi_server.api import whatsapp_settings  # noqa: PLC0415

    sidecar_body = {
        "connected": False,
        "paired": True,
        "session_state": "IDLE",
        "always_on": False,
    }
    fake_client = _FakeAsyncClient(response=_fake_response(sidecar_body))

    with patch.object(
        whatsapp_settings.httpx,
        "AsyncClient",
        return_value=fake_client,
    ):
        result = await whatsapp_settings.whatsapp_status(MagicMock())

    assert result["paired"] is True, "paired bit must survive the passthrough"
    assert result["connected"] is False
    assert result["session_state"] == "IDLE"
    # The SPA's gate is `paired || connected` — verify the data shape lets
    # it conclude "Connected" even with the live link down.
    assert (result.get("paired") or result.get("connected")) is True


async def test_status_endpoint_negative_when_unpaired_and_idle():
    """Without ``paired:true``, an IDLE sidecar should NOT read as Connected."""
    from estormi_server.api import whatsapp_settings  # noqa: PLC0415

    sidecar_body = {
        "connected": False,
        "paired": False,
        "session_state": "IDLE",
        "always_on": False,
    }
    fake_client = _FakeAsyncClient(response=_fake_response(sidecar_body))

    with patch.object(
        whatsapp_settings.httpx,
        "AsyncClient",
        return_value=fake_client,
    ):
        result = await whatsapp_settings.whatsapp_status(MagicMock())

    assert result["paired"] is False
    assert result["connected"] is False
    # The SPA's gate falls to false — chip stays "Awaiting scan".
    assert not (result.get("paired") or result.get("connected"))


async def test_status_endpoint_unreachable_sidecar_returns_unavailable():
    """A ConnectError on the sidecar is swallowed and reads as unavailable."""
    from estormi_server.api import whatsapp_settings  # noqa: PLC0415

    fake_client = _FakeAsyncClient(raise_exc=httpx.ConnectError("sidecar down"))

    with patch.object(
        whatsapp_settings.httpx,
        "AsyncClient",
        return_value=fake_client,
    ):
        result = await whatsapp_settings.whatsapp_status(MagicMock())

    assert result["connected"] is False
    assert result["session_state"] == "UNAVAILABLE"
    # The status endpoint's own fallback only sets connected+session_state.
    # The settings_overview fallback is the one that explicitly sets
    # `paired:false` (see test below) — both must agree that a missing
    # sidecar never reads as Connected.
    assert result.get("paired", False) is False


async def test_settings_overview_fallback_includes_paired_false(wired_tools_db):
    """When the sidecar is unreachable, ``settings_overview`` falls back to
    a dict with an explicit ``paired:false``.

    This guards the specific shape the commit added (the sidecar fallback
    in ``api/overview.py``): without ``paired:false`` in the fallback, the SPA
    would see ``paired === undefined`` and fall back to ``connected`` which
    is also false — still "Awaiting scan", same user-visible result.
    The explicit field documents intent and matches the live-sidecar shape
    so the SPA's TypeScript narrowing stays sound.
    """
    from estormi_server.api import overview  # noqa: PLC0415

    db = wired_tools_db
    # Mark setup as completed so the endpoint runs the full path.
    await db.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        ("setup_completed", "1"),
    )
    await db.commit()

    fake_client = _FakeAsyncClient(raise_exc=httpx.ConnectError("sidecar down"))

    with (
        patch.object(
            overview.httpx,
            "AsyncClient",
            return_value=fake_client,
        ),
        # The overview endpoint also fetches pipeline state and the
        # governor readout. Both are best-effort; stub them away so this
        # test only exercises the WhatsApp fallback shape.
        patch(
            "estormi_server.services.pipeline_status.get_pipeline_data",
            return_value={
                "next_run_at": "",
                "last_run_started": "",
                "overall_status": "unknown",
                "last_run_failed_stages": [],
            },
        ),
    ):
        result = await overview.settings_overview(MagicMock())

    wa = result.get("whatsapp") if isinstance(result, dict) else None
    assert wa is not None, "settings_overview must expose a whatsapp dict"
    assert wa.get("connected") is False
    assert wa.get("paired") is False, "fallback must explicitly set paired:false — commit 2da174a"
    assert wa.get("session_state") == "UNAVAILABLE"


async def test_reset_clears_whatsapp_chats(wired_tools_db, tmp_path):
    """Disconnect forgets stale chat metadata so a re-pair starts clean.

    A data-only reset keeps ``wa.db`` (a credential, not ingested data), which
    strands WhatsApp: an already-paired device never gets a fresh HistorySync,
    so its chunks can't come back. The per-source Disconnect is the escape
    hatch — it must drop the ``whatsapp_chats`` rows too, otherwise the
    re-paired account shows phantom chats with zero chunks and inherits the
    old account's group_type labels.
    """
    from estormi_server.api import whatsapp_settings  # noqa: PLC0415

    db = wired_tools_db
    await db.execute(
        "INSERT INTO whatsapp_chats (chat_id, chat_name, group_type) VALUES (?, ?, ?)",
        ("123@g.us", "Old Group", "work"),
    )
    await db.commit()

    fake_client = _FakeAsyncClient(raise_exc=httpx.ConnectError("sidecar down"))

    # Point WA_DB_PATH at a throwaway file so the unlink loop has nothing real
    # to delete, and stub the sidecar call (unreachable is the cold-start path).
    with (
        patch.object(whatsapp_settings, "WA_DB_PATH", tmp_path / "wa.db"),
        patch.object(whatsapp_settings.httpx, "AsyncClient", return_value=fake_client),
    ):
        result = await whatsapp_settings.whatsapp_reset(MagicMock())

    assert result == {"reset": True}
    cursor = await db.execute("SELECT COUNT(*) FROM whatsapp_chats")
    (count,) = await cursor.fetchone()
    assert count == 0, "Disconnect must clear stale whatsapp_chats metadata"

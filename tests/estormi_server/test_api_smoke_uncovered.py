"""HTTP smoke tests for the seven previously-zero-tested API modules.

The deep-review found ``api/apple_folder_picker``,
``api/events``, ``api/whatsapp_settings``, ``api/search``, ``api/system`` and
``api/permissions`` had no tests referencing them at all — seven modules wired
into the live app with no regression coverage. This file gives each one a fast
happy-path
plus the most important error branch, so the next bad copy-paste lands on
a red CI instead of a user.

The intent is breadth, not depth: every endpoint here is also fair game
for deeper tests if behaviour gets complex, but the baseline gate is "we
notice if you break it".
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.integration

# ───────────────────────────────────────────────────────────────────────
# system.py — /health and /api/open-url (allow-list enforced)
# ───────────────────────────────────────────────────────────────────────


async def test_health_returns_ok(client):
    r = await client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in ("ok", "degraded")
    assert "sqlite" in body


async def test_open_url_rejects_outside_allowlist(client):
    """The allow-list is the only thing standing between a malicious caller
    and an arbitrary ``open(1)`` of any URL scheme registered with macOS.
    Confirm the gate fires."""
    r = await client.post("/api/open-url", json={"url": "https://example.com/"})
    assert r.status_code == 400
    assert r.json()["status"] == "error"


async def test_open_url_rejects_non_string_url(client):
    r = await client.post("/api/open-url", json={"url": 42})
    assert r.status_code == 400


async def test_open_url_rejects_malformed_body(client):
    r = await client.post(
        "/api/open-url",
        content="not-json",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 400


async def test_open_url_accepts_allowlisted_pane(client):
    """A pane on the allow-list should reach ``subprocess.run``. We mock the
    process so the test doesn't actually launch System Settings."""
    url = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
    fake = AsyncMock(return_value=None)
    with patch("estormi_server.api.system.asyncio.to_thread", fake):
        r = await client.post("/api/open-url", json={"url": url})
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


# ───────────────────────────────────────────────────────────────────────
# permissions.py — /api/permissions/recheck-fda (loopback re-snapshot re-probe)
# ───────────────────────────────────────────────────────────────────────


async def test_recheck_fda_returns_status(client):
    """The route delegates to ``recheck_full_disk_access`` and surfaces its
    verdict. We mock the probe so the test doesn't spawn a subprocess or touch
    the real chat.db."""
    with patch(
        "estormi_server.server.permissions.recheck_full_disk_access", return_value="authorized"
    ):
        r = await client.post("/api/permissions/recheck-fda", json={})
    assert r.status_code == 200
    assert r.json() == {"status": "authorized"}


# ───────────────────────────────────────────────────────────────────────
# apple_folder_picker.py — /api/pick-folder
# ───────────────────────────────────────────────────────────────────────


async def test_pick_folder_uses_default_prompt_when_body_omitted(client):
    fake_result = type(
        "R",
        (),
        {"returncode": 0, "stdout": "/Users/test/iCloudVault\n", "stderr": ""},
    )()
    with patch(
        "estormi_server.api.apple_folder_picker.asyncio.to_thread",
        AsyncMock(return_value=fake_result),
    ):
        r = await client.post("/api/pick-folder")
    assert r.status_code == 200
    assert r.json() == {"path": "/Users/test/iCloudVault"}


async def test_pick_folder_returns_null_on_cancel(client):
    fake_result = type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})()
    with patch(
        "estormi_server.api.apple_folder_picker.asyncio.to_thread",
        AsyncMock(return_value=fake_result),
    ):
        r = await client.post("/api/pick-folder", json={"prompt": "Pick:"})
    assert r.status_code == 200
    assert r.json() == {"path": None}


async def test_pick_folder_sanitiser_handles_quotes_and_backslashes():
    """Reach into the helper directly — it's the only thing standing
    between user-supplied text and an AppleScript string literal."""
    from estormi_server.api.apple_folder_picker import _sanitize_pick_folder_prompt

    # ``"`` gets escaped to ``\"`` (one extra backslash); ``\`` gets escaped
    # to ``\\`` (one extra backslash). The output must therefore contain the
    # escaped forms, not the raw ones.
    out = _sanitize_pick_folder_prompt('say "hi" \\back')
    assert '\\"hi\\"' in out
    assert "\\\\back" in out  # raw ``\`` doubled
    # Control chars get stripped, not preserved.
    assert "\n" not in _sanitize_pick_folder_prompt("a\nb")
    assert _sanitize_pick_folder_prompt("") == "Select a folder:"
    assert _sanitize_pick_folder_prompt(None) == "Select a folder:"  # type: ignore[arg-type]


# ───────────────────────────────────────────────────────────────────────
# search.py — /search_memory
# ───────────────────────────────────────────────────────────────────────


async def test_search_memory_rejects_empty_query(client):
    r = await client.post("/search_memory", json={"query": ""})
    assert r.status_code == 422  # pydantic min_length=1


async def test_search_memory_rejects_oversize_query(client):
    r = await client.post("/search_memory", json={"query": "x" * 2001})
    assert r.status_code == 422  # pydantic max_length=2000


async def test_search_memory_rejects_limit_out_of_range(client):
    r = await client.post("/search_memory", json={"query": "anything", "limit": 0})
    assert r.status_code == 422
    r = await client.post("/search_memory", json={"query": "anything", "limit": 101})
    assert r.status_code == 422


async def test_search_memory_happy_path_returns_json_list(client):
    """Body validates and the route returns the ``search_memory`` contract:
    a JSON array. With the embedder mocked and Qdrant returning no points the
    array is empty — but the wire shape (list, not an error object) is what a
    caller branches on, so pin it rather than only the status code."""
    r = await client.post(
        "/search_memory",
        json={"query": "test", "limit": 5, "source": "mail"},
    )
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    assert body == []  # empty Qdrant pool → empty result set, not an error


async def test_search_memory_happy_path_returns_scored_hits(client, mock_qdrant):
    """A populated Qdrant pool flows through the real RRF + recency
    post-processing and surfaces as ranked result dicts. Asserts the
    behaviour the SPA and briefing consume — payload fields preserved, the
    derived ``score``/``recency`` present and in range — not merely that the
    route returned 200."""
    from unittest.mock import MagicMock

    point = MagicMock()
    point.id = "11111111-1111-1111-1111-111111111111"
    point.score = 0.92
    point.payload = {
        "text": "Alice confirmed the Paris launch date.",
        "source": "mail",
        "source_id": "msg-1",
        "title": "Launch confirmation",
        "date": "2026-05-06T10:00:00Z",
        "date_ts": "2026-05-06T10:00:00+00:00",
        "url": "",
        "group_type": None,
        "pending_reply": None,
    }
    query_result = MagicMock()
    query_result.points = [point]
    mock_qdrant.query_points = AsyncMock(return_value=query_result)

    r = await client.post(
        "/search_memory",
        json={"query": "Paris launch", "limit": 5, "source": "mail"},
    )
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list) and len(body) == 1
    hit = body[0]
    # Payload is preserved end-to-end…
    assert hit["source"] == "mail"
    assert "Paris launch" in hit["text"]
    # …and the post-processing derived fields are present and well-formed.
    assert "score" in hit and "recency" in hit
    assert isinstance(hit["score"], float)
    assert 0.0 <= hit["recency"] <= 1.0


# ───────────────────────────────────────────────────────────────────────
# events.py — /api/events (SSE)
# ───────────────────────────────────────────────────────────────────────


async def test_engine_events_route_is_registered():
    """SSE streams don't close cleanly inside httpx's ASGITransport — the
    generator keeps yielding heartbeats and the test hangs. Asserting the
    route is registered at all is the realistic gate this layer can offer;
    actual SSE wire behaviour is exercised by the SPA's manual smoke tests
    and the ``subscribe()`` unit coverage in ``server.events``."""
    from estormi_server.main import app  # noqa: PLC0415

    route_paths = {getattr(r, "path", None) for r in app.routes}
    assert "/api/events" in route_paths


# ───────────────────────────────────────────────────────────────────────
# whatsapp_settings.py — auto-tag status + POST validation guards
# ───────────────────────────────────────────────────────────────────────


async def test_auto_tag_status_returns_state_payload(client):
    r = await client.get("/api/whatsapp/chats/auto-tag/status")
    assert r.status_code == 200
    body = r.json()
    # Shape check — the payload should always carry these keys, even at
    # rest (when no run has been triggered yet).
    for key in ("running", "total", "done", "errors", "last_chat"):
        assert key in body, f"missing key {key!r} in {body!r}"


async def test_auto_tag_accepts_non_dict_body(client):
    """A client posting a JSON array or scalar should NOT crash the route
    with AttributeError on ``body.get(...)``. The handler treats anything
    that isn't an object as 'no body'."""
    r = await client.post(
        "/api/whatsapp/chats/auto-tag",
        content=json.dumps([]),
        headers={"content-type": "application/json"},
    )
    # Rate limiting is disabled for the whole suite (see conftest), so a
    # non-dict body must be treated as "no body" and return 200 — not 500
    # from an AttributeError on ``body.get``.
    assert r.status_code == 200

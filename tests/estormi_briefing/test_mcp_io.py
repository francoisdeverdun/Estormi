"""MCP read-path helpers — response normalisation + best-effort failure.

Unit tests for :mod:`estormi_briefing.io.mcp_io`: both response shapes the
endpoints emit (bare list / ``{"results": [...]}`` envelope), the loud-log
path on a non-2xx contract mismatch, and the transport-failure → ``[]``
guarantee that keeps a momentary server hiccup from aborting a briefing run.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from estormi_briefing.io import mcp_io

pytestmark = pytest.mark.unit


class _FakeResponse:
    def __init__(self, *, status_code: int = 200, json_data=None, text: str = "") -> None:
        self.status_code = status_code
        self._json = json_data
        self.text = text

    def json(self):
        return self._json


class _FakeClient:
    """Async-context-manager httpx stand-in: returns a canned response or raises."""

    def __init__(self, *, resp: _FakeResponse | None = None, raise_exc: Exception | None = None):
        self._resp = resp
        self._raise = raise_exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, timeout=None):
        if self._raise is not None:
            raise self._raise
        return self._resp


def _patch_client(client: _FakeClient):
    return patch("estormi_briefing.io.mcp_io.httpx.AsyncClient", return_value=client)


async def test_bare_list_response_passes_through():
    client = _FakeClient(resp=_FakeResponse(json_data=[{"id": 1}, {"id": 2}]))
    with _patch_client(client):
        out = await mcp_io._search_mcp_memory({"query": "x"})
    assert out == [{"id": 1}, {"id": 2}]


async def test_results_envelope_is_unwrapped():
    client = _FakeClient(resp=_FakeResponse(json_data={"results": [{"id": 9}]}))
    with _patch_client(client):
        out = await mcp_io._fetch_around_mcp({"ts": "2026-06-09T00:00:00Z"})
    assert out == [{"id": 9}]


@pytest.mark.regression
async def test_non_2xx_returns_empty_and_warns_loudly():
    """A contract mismatch (e.g. window_days over the endpoint cap → 422) must
    log loudly, not silently degrade a section to empty — the failure mode the
    75-day-horizon vs 30-day-cap mismatch once shipped."""
    client = _FakeClient(resp=_FakeResponse(status_code=422, text="window_days exceeds cap"))
    with _patch_client(client), patch.object(mcp_io, "log", MagicMock()) as mock_log:
        out = await mcp_io._search_mcp_memory({"query": "x"})
    assert out == []
    assert mock_log.warning.called


async def test_transport_failure_returns_empty():
    client = _FakeClient(raise_exc=httpx.ConnectError("sidecar down"))
    with _patch_client(client):
        out = await mcp_io._fetch_around_mcp({"ts": "2026-06-09T00:00:00Z"})
    assert out == []

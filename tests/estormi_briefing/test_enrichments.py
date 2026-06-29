"""Weather enrichment helpers (network mocked)."""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import estormi_briefing.io.enrichments as enrichments

pytestmark = pytest.mark.unit


def _client_returning(payload: dict, status: int = 200):
    """An httpx.AsyncClient context manager whose .get returns ``payload``."""
    resp = MagicMock()
    resp.status_code = status
    resp.json = MagicMock(return_value=payload)
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx, client


def test_weather_code_label_known_and_unknown():
    assert enrichments.weather_code_label(0) == "clear sky"
    assert enrichments.weather_code_label(61) == "light rain"
    assert enrichments.weather_code_label(123456) == ""
    assert enrichments.weather_code_label(None) == ""


def test_format_weather_compact_line():
    out = enrichments.format_weather(
        {"label": "light rain", "t_min": 11.4, "t_max": 17.8, "precip_prob": 70}
    )
    assert out == "light rain, 11–18°C, 70% precip"


def test_format_weather_drops_low_precip_and_handles_empty():
    assert enrichments.format_weather(None) == ""
    out = enrichments.format_weather(
        {"label": "clear sky", "t_min": 9, "t_max": 21, "precip_prob": 5}
    )
    assert out == "clear sky, 9–21°C"  # <30% precip omitted


async def test_weather_for_parses_daily():
    ctx, _ = _client_returning(
        {
            "daily": {
                "time": ["2026-05-31"],
                "weather_code": [61],
                "temperature_2m_max": [18.0],
                "temperature_2m_min": [11.0],
                "precipitation_probability_max": [80],
            }
        }
    )
    with patch("estormi_briefing.io.enrichments.httpx.AsyncClient", return_value=ctx):
        out = await enrichments.weather_for((48.85, 2.35), date(2026, 5, 31))
    assert out["label"] == "light rain" and out["t_max"] == 18.0 and out["precip_prob"] == 80


async def test_geocode_city_keyless():
    ctx, client = _client_returning({"results": [{"latitude": 48.85, "longitude": 2.35}]})
    with patch("estormi_briefing.io.enrichments.httpx.AsyncClient", return_value=ctx):
        out = await enrichments.geocode_city("Paris")
    assert out == (48.85, 2.35)
    # keyless endpoint — no api_key param sent
    assert "api_key" not in client.get.call_args.kwargs.get("params", {})


async def test_enrichment_failure_degrades_to_none():
    """A non-200 (or raised) response yields None/empty, never an exception."""
    ctx, _ = _client_returning({}, status=503)
    with patch("estormi_briefing.io.enrichments.httpx.AsyncClient", return_value=ctx):
        assert await enrichments.geocode_city("Paris") is None
        assert await enrichments.weather_for((1, 2), date(2026, 5, 31)) is None

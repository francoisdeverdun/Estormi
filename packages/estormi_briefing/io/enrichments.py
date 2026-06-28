"""Weather enrichment for the briefing day-vision.

Network helpers, all best-effort and keyless: geocode the home city and fetch
the day's weather via Open-Meteo. Every function degrades to ``None`` / ``""``
on any failure — a flaky network must never block the briefing.

Kept side-effect-free apart from the HTTP calls so it unit-tests cleanly by
patching ``httpx.AsyncClient``.
"""

from __future__ import annotations

from datetime import date

import httpx
import structlog

log = structlog.get_logger()

_OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
_OPEN_METEO_GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"

# WMO weather codes → short English label. The day-vision renders in the user's
# language, so the model translates; we only need a stable, compact phrase.
_WMO: dict[int, str] = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "rime fog",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    66: "freezing rain",
    67: "heavy freezing rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    77: "snow grains",
    80: "rain showers",
    81: "rain showers",
    82: "violent rain showers",
    85: "snow showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm with hail",
    99: "thunderstorm with heavy hail",
}


def weather_code_label(code: int | None) -> str:
    """Human label for a WMO weather code (``""`` when unknown)."""
    if code is None:
        return ""
    return _WMO.get(int(code), "")


async def geocode_city(name: str, *, timeout: float = 8.0) -> tuple[float, float] | None:
    """Resolve a place name to ``(lat, lon)`` via Open-Meteo's keyless geocoder.

    City-level precision — enough for weather, and it needs no API key, so the
    home-location weather works with no configuration.
    """
    name = (name or "").strip()
    if not name:
        return None
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                _OPEN_METEO_GEOCODE,
                params={"name": name, "count": 1},
                timeout=timeout,
            )
            if r.status_code != 200:
                return None
            results = (r.json() or {}).get("results") or []
            if not results:
                return None
            return (float(results[0]["latitude"]), float(results[0]["longitude"]))
    except Exception as exc:  # noqa: BLE001 — enrichment is best-effort
        log.warning("city geocode failed for %r: %s", name[:60], exc)
        return None


async def weather_for(
    coords: tuple[float, float], day: date, *, timeout: float = 8.0
) -> dict | None:
    """Open-Meteo daily forecast for ``day`` at ``coords`` (keyless).

    Returns ``{"label", "t_min", "t_max", "precip_prob"}`` or ``None``.
    """
    lat, lon = coords
    iso = day.isoformat()
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                _OPEN_METEO,
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                    "precipitation_probability_max",
                    "timezone": "auto",
                    "start_date": iso,
                    "end_date": iso,
                },
                timeout=timeout,
            )
            if r.status_code != 200:
                return None
            daily = (r.json() or {}).get("daily") or {}
            if not daily.get("time"):
                return None
            return {
                "label": weather_code_label((daily.get("weather_code") or [None])[0]),
                "t_max": (daily.get("temperature_2m_max") or [None])[0],
                "t_min": (daily.get("temperature_2m_min") or [None])[0],
                "precip_prob": (daily.get("precipitation_probability_max") or [None])[0],
            }
    except Exception as exc:  # noqa: BLE001 — enrichment is best-effort
        log.warning("weather fetch failed: %s", exc)
        return None


def format_weather(weather: dict | None) -> str:
    """One-line weather summary for the prompt, or ``""``."""
    if not weather:
        return ""
    parts: list[str] = []
    if weather.get("label"):
        parts.append(str(weather["label"]))
    t_max, t_min = weather.get("t_max"), weather.get("t_min")
    if t_max is not None and t_min is not None:
        parts.append(f"{round(t_min)}–{round(t_max)}°C")
    elif t_max is not None:
        parts.append(f"{round(t_max)}°C")
    prob = weather.get("precip_prob")
    if prob is not None and prob >= 30:
        parts.append(f"{round(prob)}% precip")
    return ", ".join(parts)

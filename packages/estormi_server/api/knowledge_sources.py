"""Knowledge sources YAML CRUD + URL → source draft resolver.

Owns the ``/api/knowledge/...`` endpoints the Settings UI binds to:
loading and saving the sources YAML, opening it in Finder, and the
RSS / YouTube auto-detection that fills the "Add source" form. The pure
classification / SSRF / resolution logic lives in
:mod:`estormi_server.services.knowledge_sources`; this module is the HTTP shell.
"""

from __future__ import annotations

import asyncio
import re

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, RootModel

from estormi_server.server.limiter import limiter
from estormi_server.services import knowledge_sources as svc

# Re-export the service helpers under their historic ``_kb_*`` names so the
# settings_ui aggregator, the overview aggregator, and the existing unit tests
# (``from estormi_server.api import knowledge_sources as ks``) keep resolving
# to the same callables after the logic moved into ``services``.
_kb_yaml_path = svc.yaml_path
_kb_yaml_load = svc.yaml_load
_kb_deduce_kind = svc.deduce_kind
_kb_is_youtube = svc.is_youtube
_kb_youtube_label_from_url = svc.youtube_label_from_url
_kb_resolve_youtube = svc.resolve_youtube
_kb_url_is_public = svc.url_is_public
_kb_fetch_public = svc.fetch_public
_kb_resolve_rss = svc.resolve_rss

log = structlog.get_logger()

router = APIRouter()


# ── Knowledge sources YAML ───────────────────────────────────────────────────


async def _kb_register_path(db) -> None:  # type: ignore[type-arg]
    """Store the canonical YAML path in the DB so run_briefing.py picks it up."""
    from estormi_server.storage.tools import get_write_lock  # noqa: PLC0415

    # Leaf DELETE/INSERT→commit span — serialise on the shared write lock so a
    # concurrent writer's commit can't tear it. See ``tools._write_lock``.
    async with get_write_lock():
        await db.execute(
            "INSERT INTO settings (key, value) VALUES ('knowledge_sources_yaml', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(svc.yaml_path()),),
        )
        await db.commit()


_KB_MAX_SOURCES = 500


# Bind the body via Pydantic so Starlette rejects oversize payloads at the
# boundary; this stops a hostile caller from forcing FastAPI to buffer and
# parse a multi-MB JSON list before our len() check fires.
class _KbSourcesBody(RootModel[list[dict]]):
    root: list[dict] = Field(..., max_length=_KB_MAX_SOURCES)


@router.get("/api/knowledge/sources")
@limiter.limit("60/minute")
async def get_knowledge_sources(request: Request):
    return await asyncio.to_thread(svc.yaml_load)


@router.put("/api/knowledge/sources")
@limiter.limit("30/minute")
async def put_knowledge_sources(request: Request, body: _KbSourcesBody):
    import yaml as _yaml  # noqa: PLC0415

    from estormi_server.storage.tools import sqlite_conn  # noqa: PLC0415

    sources = body.root
    for s in sources:
        if "subtitle_langs" not in s:
            s["subtitle_langs"] = ["en", "fr"]

    path = svc.yaml_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    def _write_yaml() -> None:
        with open(path, "w", encoding="utf-8") as f:
            _yaml.dump({"sources": sources}, f, allow_unicode=True, sort_keys=False)

    try:
        await asyncio.to_thread(_write_yaml)
        db = sqlite_conn()
        await _kb_register_path(db)
        return {"status": "ok", "count": len(sources)}
    except Exception:
        log.exception("knowledge.sources.write_failed")
        return JSONResponse({"error": "failed to write knowledge sources"}, status_code=500)


@router.post("/api/knowledge/open-sources")
@limiter.limit("10/minute")
async def open_knowledge_sources_in_finder(request: Request):
    import subprocess  # noqa: PLC0415

    try:
        await asyncio.to_thread(subprocess.Popen, ["open", "-R", str(svc.yaml_path())])
    except OSError as exc:
        log.warning("knowledge_sources.open_in_finder_failed", error=str(exc))
        return JSONResponse({"error": "could not open Finder"}, status_code=500)
    return {"status": "ok"}


# ── Knowledge source resolution endpoint ─────────────────────────────────────


class _KbResolveBody(BaseModel):
    # Optional so a missing/empty url falls through to the explicit 400 below
    # (preserving the {"error": "url required"} contract) while a provided url
    # still gets length-bounded.
    url: str = Field("", max_length=2048)


@router.post("/api/knowledge/resolve")
@limiter.limit("30/minute")
async def resolve_knowledge_source(request: Request, body: _KbResolveBody):
    """Inspect a pasted URL and return a pre-filled knowledge-source draft.

    Deduces the source type from the URL, fetches a human label from the
    channel/feed metadata, and guesses a kind. Every field is editable in the
    UI — this endpoint only supplies sensible defaults.
    """
    url = body.url.strip()
    if not url:
        return JSONResponse({"error": "url required"}, status_code=400)
    if not re.match(r"https?://", url, re.IGNORECASE):
        return JSONResponse(
            {"error": "url must start with http:// or https://"},
            status_code=400,
        )
    if not await asyncio.to_thread(svc.url_is_public, url):
        return JSONResponse(
            {"error": "url must resolve to a public address"},
            status_code=400,
        )

    if svc.is_youtube(url):
        label = await asyncio.to_thread(svc.resolve_youtube, url)
        return {
            "type": "youtube_channel",
            "label": label,
            "url": url,
            "axis": svc.deduce_kind(f"{label} {url}"),
        }

    title, desc = await asyncio.to_thread(svc.resolve_rss, url)
    return {
        "type": "rss",
        "label": title or url,
        "urls": [url],
        "axis": svc.deduce_kind(f"{title} {desc} {url}"),
    }

"""Fetch YouTube videos and RSS articles for the Briefing engine."""

from __future__ import annotations

import calendar as _calendar
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import structlog

log = structlog.get_logger()


# Per-run scratch dir for yt-dlp subtitle downloads.
#
# Historically TMP_DIR was a fixed path per user (`/tmp/estormi_kb_<uid>`),
# which meant two concurrent runs (or a stale crash leftover) silently
# stepped on each other's VTT files. Each invocation now gets its own
# isolated temp tree; ``cleanup_tmp_dir()`` removes it at the end of a run
# (see ingest_world.py, which calls it once the fetch loop is done).
_RUN_TMP_DIR: Path | None = None


def _resolve_tmp_dir() -> Path:
    """Return the per-run scratch directory, creating it on first use."""
    global _RUN_TMP_DIR
    if _RUN_TMP_DIR is not None:
        return _RUN_TMP_DIR
    override = os.getenv("ESTORMI_KNOWLEDGE_TMP_DIR")
    if override:
        _RUN_TMP_DIR = Path(override)
        _RUN_TMP_DIR.mkdir(parents=True, exist_ok=True)
    else:
        _RUN_TMP_DIR = Path(tempfile.mkdtemp(prefix="estormi_kb_"))
    return _RUN_TMP_DIR


def cleanup_tmp_dir() -> None:
    """Remove the per-run scratch directory. Safe to call multiple times."""
    global _RUN_TMP_DIR
    if _RUN_TMP_DIR is None:
        return
    shutil.rmtree(_RUN_TMP_DIR, ignore_errors=True)
    _RUN_TMP_DIR = None


def _yt_dlp_bin() -> str:
    """Return the yt-dlp executable path, preferring the venv it was installed into."""
    # Prefer the same venv as the running interpreter.
    venv_bin = Path(sys.executable).parent / "yt-dlp"
    if venv_bin.exists():
        return str(venv_bin)
    # Fall back to shutil.which (covers PATH-based installs).
    found = shutil.which("yt-dlp")
    if found:
        return found
    raise FileNotFoundError("yt-dlp not found. Install it with: pip install yt-dlp")


def fetch_recent_videos(source: dict[str, Any], lookback_days: int = 1) -> list[dict[str, Any]]:
    """Return videos uploaded within the last `lookback_days` days from the channel.

    Uses full metadata extraction (no --flat-playlist) so upload_date is
    accurate and --dateafter can stop early on older videos.
    """
    cutoff = (date.today() - timedelta(days=lookback_days)).strftime("%Y%m%d")
    playlist_end = max(10, lookback_days * 3)
    result = subprocess.run(
        [
            _yt_dlp_bin(),
            "--skip-download",
            "--playlist-end",
            str(playlist_end),
            "--dateafter",
            cutoff,
            "--print",
            # The `j` field modifier JSON-encodes each value (including the
            # surrounding quotes), so a title containing a double-quote or
            # backslash stays valid JSON. With the bare `s` modifier such a
            # title produced malformed JSON that json.loads silently dropped
            # below, so that video was never ingested.
            '{"id":%(id)j,"title":%(title)j,"upload_date":%(upload_date)j}',
            # `--` terminates option parsing so a hostile URL beginning with
            # `--config` or similar can't be mis-parsed as a yt-dlp flag.
            "--",
            source["url"],
        ],
        capture_output=True,
        text=True,
        check=False,
        # Without a timeout, a network stall on the playlist endpoint freezes
        # the entire knowledge DAG. 10 min is generous for ~30 video metadata
        # entries.
        timeout=600,
    )
    if result.returncode not in (0, 1):
        log.warning(
            "yt-dlp exit %d for %s: %s",
            result.returncode,
            source["id"],
            result.stderr[:200],
        )
    videos = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            videos.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return videos


def download_transcript(video_id: str, langs: list[str]) -> str | None:
    """Download the best available subtitle track for a video and return cleaned text.

    Never raises on a stalled fetch: a ``TimeoutExpired`` on one language is
    logged and the next language is tried, so one slow YouTube response costs
    at most this video — not the whole source's collection run.
    """
    tmp_dir = _resolve_tmp_dir()
    for lang in langs:
        try:
            subprocess.run(
                [
                    _yt_dlp_bin(),
                    "--skip-download",
                    "--write-auto-sub",
                    "--sub-lang",
                    lang,
                    "--sub-format",
                    "vtt",
                    "--output",
                    str(tmp_dir / "%(id)s.%(ext)s"),
                    # `--` terminates option parsing — see fetch_recent_videos.
                    "--",
                    f"https://www.youtube.com/watch?v={video_id}",
                ],
                capture_output=True,
                check=False,
                # 5 min is plenty for a single subtitle file; without a timeout a
                # stalled YouTube response can freeze the knowledge pipeline forever.
                timeout=300,
            )
        except subprocess.TimeoutExpired:
            log.warning("Subtitle fetch timed out for video %s (lang %s)", video_id, lang)
            continue
        vtt_path = tmp_dir / f"{video_id}.{lang}.vtt"
        if vtt_path.exists():
            text = _strip_vtt(vtt_path)
            if text:
                return text
    log.warning("No transcript found for video %s (tried: %s)", video_id, langs)
    return None


_RSS_MAX_REDIRECTS = 5

# Hard cap on the RSS body we will buffer into memory. httpx has no default
# response-size limit, so a hostile or compromised feed could stream gigabytes
# and OOM the ingestion engine well within the request timeout. 16 MiB is far
# beyond any real feed; we abort the read once it is exceeded.
_RSS_MAX_BODY_BYTES = 16 * 1024 * 1024


def _rss_url_is_public(url: str) -> bool:
    """Mirror of mcp-server's ``_kb_url_is_public``: refuse hosts that resolve
    to private / loopback / link-local / metadata addresses so a hostile RSS
    URL in the user's sources YAML can't be weaponised into an SSRF probe.
    """
    try:
        host = urlparse(url).hostname
    except ValueError:
        return False
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except (ValueError, IndexError):
            return False
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return False
    return True


def _rss_fetch_public(url: str, *, timeout: float = 20.0) -> bytes | None:
    """Fetch an RSS feed body with the SSRF guard re-applied on every redirect.

    ``feedparser.parse(url)`` uses urllib internally and follows redirects with
    no per-hop check, so a public bootstrap URL that 302s to loopback would
    slip past the public-host gate. We fetch with httpx, follow redirects
    manually, and feed the bytes to ``feedparser.parse``.
    """
    try:
        import httpx  # noqa: PLC0415
    except ImportError:
        return None
    current = url
    for _ in range(_RSS_MAX_REDIRECTS):
        if not _rss_url_is_public(current):
            return None
        try:
            with httpx.stream(
                "GET",
                current,
                timeout=timeout,
                follow_redirects=False,
                headers={"User-Agent": "Estormi/1.0"},
            ) as resp:
                if resp.status_code in (301, 302, 303, 307, 308):
                    loc = resp.headers.get("location")
                    if not loc:
                        return _rss_read_capped(resp)
                    current = str(httpx.URL(current).join(loc))
                    continue
                return _rss_read_capped(resp)
        except Exception:
            return None
    return None


def _rss_read_capped(resp: Any) -> bytes | None:
    """Read a streamed httpx response body, refusing bodies over the cap.

    Rejects up front on an over-cap ``Content-Length``, then enforces the cap
    incrementally as bytes arrive so a server that omits or lies about the
    header still cannot stream us into an OOM. Returns ``None`` when the cap is
    exceeded (the feed is dropped, exactly as for any other fetch failure).
    """
    clen = resp.headers.get("content-length")
    if clen is not None:
        try:
            if int(clen) > _RSS_MAX_BODY_BYTES:
                return None
        except ValueError:
            pass
    buf = bytearray()
    for chunk in resp.iter_bytes():
        buf.extend(chunk)
        if len(buf) > _RSS_MAX_BODY_BYTES:
            return None
    return bytes(buf)


def fetch_rss_source(source: dict[str, Any]) -> list[dict[str, Any]]:
    """Fetch RSS articles from all URLs in source["urls"], filtered to the last window_hours.

    Returns a list of dicts: {title, summary, url, published}.
    Deduplicates by URL/guid across all feeds.
    """
    try:
        import feedparser  # noqa: PLC0415
    except ImportError:
        log.error("feedparser is not installed — RSS sources cannot be fetched")
        return []

    window_hours = int(source.get("window_hours", 24))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)

    seen: set[str] = set()
    articles: list[dict[str, Any]] = []

    for url in source.get("urls", []):
        try:
            body = _rss_fetch_public(url)
            if body is None:
                log.warning("RSS fetch skipped for %s: non-public or unreachable", url)
                continue
            feed = feedparser.parse(body)
            for entry in feed.entries:
                published: datetime | None = None
                for attr in ("published_parsed", "updated_parsed"):
                    t = getattr(entry, attr, None)
                    if t:
                        published = datetime.fromtimestamp(_calendar.timegm(t), tz=timezone.utc)
                        break
                if published is None or published < cutoff:
                    continue
                article_url = getattr(entry, "link", "") or getattr(entry, "id", "")
                if not article_url or article_url in seen:
                    continue
                seen.add(article_url)
                raw_summary = getattr(entry, "summary", "") or ""
                summary = re.sub(r"<[^>]+>", "", raw_summary).strip()[:500]
                articles.append(
                    {
                        "title": getattr(entry, "title", "").strip(),
                        "summary": summary,
                        "url": article_url,
                        "published": published.strftime("%Y-%m-%d"),
                    }
                )
        except Exception as exc:
            log.warning("RSS fetch error for %s: %s", url, exc)

    return articles


def _strip_vtt(path: Path) -> str:
    """Remove VTT timestamps and deduplicate adjacent repeated lines."""
    raw = path.read_text(encoding="utf-8", errors="replace")
    lines = raw.splitlines()
    text_lines: list[str] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if (
            line.startswith("WEBVTT")
            or line.startswith("NOTE")
            or line.startswith("Kind:")
            or line.startswith("Language:")
        ):
            continue
        # Skip timestamp lines: 00:00:00.000 --> 00:00:00.000
        if re.match(r"^\d{2}:\d{2}:\d{2}\.\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}", line):
            continue
        # Strip inline VTT tags like <00:00:05.000><c>text</c>
        line = re.sub(r"<[^>]+>", "", line).strip()
        if not line:
            continue
        # Deduplicate adjacent repeated lines
        if text_lines and text_lines[-1] == line:
            continue
        text_lines.append(line)
    return " ".join(text_lines)

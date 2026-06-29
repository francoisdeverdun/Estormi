"""Knowledge-source resolution + YAML persistence — the logic behind the
``/api/knowledge/sources`` and ``/api/knowledge/resolve`` endpoints.

This is the pure, network-and-DNS-aware core that the
:mod:`estormi_server.api.knowledge_sources` router drives: classify a pasted
URL into a source kind, fetch a human label from RSS/YouTube metadata (with an
SSRF guard re-applied on every redirect hop), and load/locate the sources YAML.
None of it touches FastAPI, so every heuristic and the SSRF guard is directly
unit-testable.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from pathlib import Path
from urllib.parse import urlparse

import httpx
import structlog

log = structlog.get_logger()


# ── Knowledge sources YAML ───────────────────────────────────────────────────


def yaml_path() -> Path:
    """Return the canonical knowledge sources YAML path in the user data directory."""
    from estormi_server.storage.tools import DATA_DIR  # noqa: PLC0415

    return Path(DATA_DIR) / "knowledge_sources.yaml"


def yaml_load() -> list[dict]:
    """Load sources from the data-dir YAML; no sources ship by default."""
    import yaml as _yaml  # noqa: PLC0415

    path = yaml_path()
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            return (_yaml.safe_load(f) or {}).get("sources", [])
    except Exception:
        return []  # best-effort: unreadable/malformed YAML yields no sources


# ── Source resolution (auto-detect type / label / kind) ──────────────────────

# Keyword heuristic for the source "kind" (the YAML `axis` field). Checked in
# order — the first axis with a hit wins, so the more specific buckets come
# before the generic ones. "news" is the catch-all default.
KIND_KEYWORDS: dict[str, tuple[str, ...]] = {
    "finance": (
        "finance",
        "financ",
        "bourse",
        "stock",
        "market",
        "trading",
        "trader",
        "invest",
        "crypto",
        "bitcoin",
        "wall street",
        "nasdaq",
        "patrimoine",
    ),
    "economic": (
        "econom",
        "macro",
        "gdp",
        "pib",
        "inflation",
        "business",
        "entreprise",
    ),
    "politic": (
        "politic",
        "politiq",
        "election",
        "élection",
        "geopolit",
        "géopolit",
        "government",
        "gouvernement",
        "parliament",
        "diplomat",
        "senate",
    ),
    "tech": (
        "tech",
        "technolog",
        "software",
        "developer",
        "coding",
        "programming",
        "artificial intelligence",
        "startup",
        "gadget",
        "science",
        "cyber",
        "hardware",
        "informatique",
        "numérique",
    ),
}


def deduce_kind(text: str) -> str:
    """Classify a source into one of news/tech/politic/economic/finance.

    Keyword heuristic over the channel/feed name, description and URL. The UI
    surfaces this as an editable default, so accuracy is a convenience rather
    than a contract.
    """
    haystack = f" {text.lower()} "
    for kind, words in KIND_KEYWORDS.items():
        if any(word in haystack for word in words):
            return kind
    return "news"


def is_youtube(url: str) -> bool:
    return bool(re.search(r"youtube\.com|youtu\.be", url, re.IGNORECASE))


def youtube_label_from_url(url: str) -> str:
    """Derive a readable label from a YouTube channel URL (handle / name segment)."""
    match = re.search(
        r"youtube\.com/(?:@([^/?#]+)|c/([^/?#]+)|user/([^/?#]+)|channel/([^/?#]+))",
        url,
        re.IGNORECASE,
    )
    if match:
        segment = next((g for g in match.groups() if g), "")
        label = segment.replace("-", " ").replace("_", " ").strip()
        if label:
            return label
    return "YouTube channel"


def resolve_youtube(url: str) -> str:
    """Return the channel display name via yt-dlp, falling back to the URL handle."""
    import shutil  # noqa: PLC0415
    import subprocess  # noqa: PLC0415

    binary = shutil.which("yt-dlp")
    if binary:
        try:
            result = subprocess.run(
                [
                    binary,
                    "--flat-playlist",
                    "--playlist-end",
                    "1",
                    "--print",
                    "%(channel)s",
                    "--",
                    url,
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=45,
            )
            name = next(
                (ln.strip() for ln in result.stdout.splitlines() if ln.strip()),
                "",
            )
            if name and name.upper() != "NA":
                return name
        except Exception:
            pass  # best-effort: fall back to the URL-derived label
    return youtube_label_from_url(url)


def url_is_public(url: str) -> bool:
    """Reject URLs whose host resolves to a private / loopback / link-local
    address before we let ``httpx`` fetch them. Prevents the resolver from
    being weaponised as an SSRF probe against LAN services or cloud metadata
    endpoints (169.254.169.254).

    Residual gap: this check and the subsequent ``httpx`` fetch each resolve
    DNS independently, so an attacker-controlled name can return a public A
    record here and a private one to ``httpx`` microseconds later (DNS
    rebinding / TOCTOU). Full pinning (connect to the validated IP literal with
    the Host header preserved) would close it but is not done here — the
    endpoint is loopback/first-party gated and rate-limited and only returns
    metadata, so the residual exposure is accepted.
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
        sockaddr = info[4]
        try:
            ip = ipaddress.ip_address(sockaddr[0])
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


MAX_REDIRECTS = 5


def fetch_public(url: str, *, timeout: float = 20.0) -> httpx.Response | None:
    """Fetch ``url`` with SSRF guard re-applied on every redirect hop.

    httpx's ``follow_redirects=True`` walks Location: headers without re-running
    our public-host check, so a public bootstrap host that 302s to
    ``http://169.254.169.254/...`` or a loopback admin port would bypass the
    guard. This wrapper does the redirect loop manually.
    """
    current = url
    for _ in range(MAX_REDIRECTS):
        if not url_is_public(current):
            return None
        resp = httpx.get(
            current,
            timeout=timeout,
            follow_redirects=False,
            headers={"User-Agent": "Estormi/1.0"},
        )
        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("location")
            if not loc:
                return resp
            current = str(httpx.URL(current).join(loc))
            continue
        return resp
    return None


def resolve_rss(url: str) -> tuple[str, str]:
    """Return (feed_title, feed_description) for an RSS/Atom feed URL."""
    try:
        import feedparser  # noqa: PLC0415
    except ImportError:
        return "", ""
    try:
        resp = fetch_public(url)
    except Exception:
        return "", ""  # best-effort: unreachable/malformed feed yields no metadata
    if resp is None:
        return "", ""
    feed = feedparser.parse(resp.content)
    meta = getattr(feed, "feed", {}) or {}
    title = (meta.get("title") or "").strip()
    desc = (meta.get("subtitle") or meta.get("description") or "").strip()
    return title, re.sub(r"<[^>]+>", "", desc).strip()

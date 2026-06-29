#!/usr/bin/env python3
"""End-to-end smoke test: seed 3 fixtures, probe semantics, clean up.

Uses source `__smoke_test__` so fixtures are isolated from real data and are
trivial to delete after the run (by source filter + content_hash prefix).
Idempotent: a second run overwrites the previous fixtures.
"""

from __future__ import annotations

import hashlib
import os
import sys
import time

import httpx

BASE = (
    os.environ.get("MCP_SERVER_URL")
    or f"http://localhost:{os.environ.get('MCP_SERVER_PORT', '8000')}"
).rstrip("/")
SMOKE_SOURCE = "__smoke_test__"

FIXTURES = [
    {
        "title": "Meeting with Alice about the Paris launch",
        "text": (
            "Met with Alice at Café de Flore on Tuesday. We discussed the product "
            "launch in Paris scheduled for May. Alice will coordinate with the PR "
            "agency and the primary user will handle the technical demo booth setup."
        ),
        "date": "2026-04-18T10:00:00Z",
    },
    {
        "title": "nomic vs bge embedding benchmark",
        "text": (
            "Benchmarked nomic-embed-text-v1.5 against bge-small-en: nomic wins on "
            "recall for French content, bge is faster but loses semantic nuance in "
            "mixed-language docs. Sticking with nomic."
        ),
        "date": "2026-04-20T14:22:11Z",
    },
    {
        "title": "Rendez-vous dentiste",
        "text": "Rendez-vous chez le dentiste, Dr. Martin, le 5 mai à 15h30.",
        "date": "2026-04-21T09:00:00Z",
    },
]


def _hash(text: str, salt: str = "") -> str:
    return "smoke-" + hashlib.sha256(f"{salt}:{text}".encode()).hexdigest()


def ingest(f: dict) -> None:
    body = {
        **f,
        "source": SMOKE_SOURCE,
        "content_hash": _hash(f["text"], f["title"]),
    }
    r = httpx.post(f"{BASE}/ingest_chunk", json=body, timeout=60)
    r.raise_for_status()


def search(query: str, source: str | None = None, limit: int = 5) -> list[dict]:
    body: dict = {"query": query, "limit": limit}
    if source:
        body["source"] = source
    r = httpx.post(f"{BASE}/search_memory", json=body, timeout=60)
    r.raise_for_status()
    return r.json()


def cleanup() -> None:
    httpx.post(
        f"{BASE}/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "delete_by_source",
                "arguments": {"source": SMOKE_SOURCE},
            },
        },
        timeout=30,
    )


def main() -> int:
    print(f"→ Target: {BASE}")
    httpx.get(f"{BASE}/health", timeout=5).raise_for_status()

    print("→ Seeding fixtures…")
    for f in FIXTURES:
        ingest(f)
    time.sleep(0.5)  # let Qdrant index

    checks: list[tuple[str, str]] = [
        ("Paris product launch meeting", "Alice"),
        ("which embedding model did we benchmark?", "nomic"),
        ("rendez-vous médecin", "dentiste"),
    ]
    all_ok = True
    print("→ Relevance probes (top-1 must contain expected token)…")
    for query, expected in checks:
        hits = search(query, source=SMOKE_SOURCE, limit=1)
        top = hits[0] if hits else {}
        text = (top.get("title", "") + " " + top.get("text", "")).lower()
        ok = expected.lower() in text
        score = top.get("score", 0)
        print(f"  {'✓' if ok else '✗'} {query!r}  score={score}  top={top.get('title', '')!r}")
        all_ok = all_ok and ok

    print("→ Source filter…")
    hits = search("benchmark", source=SMOKE_SOURCE, limit=3)
    only_smoke = hits and all(h.get("source") == SMOKE_SOURCE for h in hits)
    print(f"  {'✓' if only_smoke else '✗'} sources: {[h.get('source') for h in hits]}")
    all_ok = all_ok and only_smoke

    print("→ Cleanup…")
    cleanup()

    print("\n" + ("ALL PASSED ✓" if all_ok else "SOME CHECKS FAILED ✗"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        # Best-effort cleanup even on exception
        try:
            cleanup()
        except Exception:
            pass

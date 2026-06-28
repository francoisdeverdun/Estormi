#!/usr/bin/env python3
"""Data freshness check for Estormi.

Queries the local SQLite database for per-source freshness and chunk
counts, and reads launchd agent status. Outputs JSON or a human table.

Usage:
    python3 scripts/freshness_check.py          # human table
    python3 scripts/freshness_check.py --json   # JSON (for weekly_report.sh)
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent

# Load .env
env_path = ROOT / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

DATA_DIR = os.getenv(
    "ESTORMI_DATA_DIR",
    os.path.expanduser("~/Library/Application Support/Estormi"),
)
DB_PATH = os.path.join(DATA_DIR, "estormi.db")

# Expected max hours between ingests per source (used to flag staleness)
FRESHNESS_THRESHOLDS: dict[str, float] = {
    "notes": 26.0,  # daily (no new chunks when no new notes)
    "mail": 26.0,  # nightly pipeline
    "reminders": 26.0,  # nightly pipeline
    "documents": 26.0,  # nightly pipeline
    "whatsapp": 26.0,  # nightly pipeline
    "imessage": 26.0,  # nightly pipeline
    "knowledge": 26.0,  # nightly pipeline (world corpus)
    # ``gcal`` and ``whoop`` are deliberately omitted: both are opt-in
    # (``default_stage=False``) and run only on installs that configured them, so
    # the "missing from DB → never ingested" synthetic pass below must NOT flag
    # them as stale on the majority of installs that never enable them. Once they
    # do ingest, present rows still report via the ``.get(src, 26.0)`` fallback.
}

SQL = """
SELECT
    source,
    MAX(ingested_at)  AS last_ingested,
    MAX(date_ts)      AS last_date_ts,
    CAST(strftime('%s', MAX(ingested_at)) AS INTEGER) AS last_ingested_epoch,
    COUNT(*)          AS total_chunks,
    COUNT(CASE WHEN ingested_at > datetime('now', '-1 day')  THEN 1 END) AS chunks_24h,
    COUNT(CASE WHEN ingested_at > datetime('now', '-7 days') THEN 1 END) AS chunks_7d
FROM chunks
GROUP BY source
ORDER BY source;
"""


def run_sql() -> list[dict]:
    """Run the freshness query against the local SQLite database."""
    if not os.path.exists(DB_PATH):
        print(f"✗ Database not found: {DB_PATH}", file=sys.stderr)
        sys.exit(2)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(SQL).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def launchd_status() -> dict[str, dict]:
    """Return {label: {pid, exit_code}} for all Estormi agents."""
    try:
        out = subprocess.check_output(
            ["launchctl", "list"], stderr=subprocess.DEVNULL, timeout=10
        ).decode()
    except Exception:
        return {}
    agents = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3 and "estormi" in parts[2]:
            agents[parts[2]] = {
                "pid": None if parts[0] == "-" else parts[0],
                "exit_code": int(parts[1]) if parts[1].lstrip("-").isdigit() else parts[1],
            }
    return agents


def hours_since_epoch(epoch: int | None) -> float | None:
    if epoch is None:
        return None
    return (datetime.now(tz=timezone.utc).timestamp() - epoch) / 3600


def build_report() -> dict:
    now = datetime.now(tz=timezone.utc).isoformat()
    sources_raw = run_sql()
    agents = launchd_status()

    sources = []
    for row in sources_raw:
        src = row["source"]
        h = hours_since_epoch(row.get("last_ingested_epoch"))
        threshold = FRESHNESS_THRESHOLDS.get(src, 26.0)
        stale = h is None or h > threshold
        sources.append(
            {
                **row,
                "hours_since_ingest": round(h, 1) if h is not None else None,
                "threshold_hours": threshold,
                "stale": stale,
            }
        )

    # Sources defined in thresholds but missing from DB entirely
    present = {s["source"] for s in sources}
    for src, threshold in FRESHNESS_THRESHOLDS.items():
        if src not in present:
            sources.append(
                {
                    "source": src,
                    "last_ingested": None,
                    "last_date_ts": None,
                    "total_chunks": 0,
                    "chunks_24h": 0,
                    "chunks_7d": 0,
                    "hours_since_ingest": None,
                    "threshold_hours": threshold,
                    "stale": True,
                }
            )
    sources.sort(key=lambda s: s["source"])

    return {
        "generated_at": now,
        "sources": sources,
        "agents": agents,
        "summary": {
            "total_sources": len(sources),
            "stale_sources": sum(1 for s in sources if s["stale"]),
            "agents_loaded": len(agents),
            "agents_failed": sum(
                1 for a in agents.values() if a["exit_code"] not in (0, None, "-")
            ),
        },
    }


def print_table(report: dict) -> None:
    now = report["generated_at"][:19].replace("T", " ")
    print(f"\n=== Estormi — freshness report ({now} UTC) ===\n")

    hdr = f"{'Source':<16} {'Last ingest':>19} {'Age (h)':>8} {'Threshold':>10} {'7d':>6} {'24h':>5} {'Status':>8}"
    print(hdr)
    print("-" * len(hdr))
    for s in report["sources"]:
        age = f"{s['hours_since_ingest']:.1f}" if s["hours_since_ingest"] is not None else "never"
        ts = (s["last_ingested"] or "")[:19]
        status = "STALE" if s["stale"] else "OK"
        print(
            f"{s['source']:<16} {ts:>19} {age:>8} {s['threshold_hours']:>9.0f}h {s['chunks_7d']:>6} {s['chunks_24h']:>5} {status:>8}"
        )

    print()
    print("=== LaunchAgents ===\n")
    for label, info in sorted(report["agents"].items()):
        short = label.replace("app.estormi.local.", "")
        pid = info["pid"] or "-"
        ec = info["exit_code"]
        status = "running" if pid != "-" else ("OK" if ec == 0 else f"FAIL({ec})")
        print(f"  {short:<32} {status}")

    s = report["summary"]
    print(
        f"\n  Stale sources: {s['stale_sources']}/{s['total_sources']}"
        f"  |  Failed agents: {s['agents_failed']}/{s['agents_loaded']}\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="Output JSON instead of table")
    args = parser.parse_args()

    report = build_report()
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_table(report)

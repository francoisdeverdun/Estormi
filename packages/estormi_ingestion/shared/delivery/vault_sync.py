"""iCloud Drive vault sync for the Estormi iOS companion.

Writes the daily briefing as plain JSON files into a folder the user keeps
in iCloud Drive. The companion reads that folder directly through a
user-picked, security-scoped bookmark — no CloudKit, no Apple Developer
account.

Default location: the ``Estormi`` folder in the user's iCloud Drive
(``~/Library/Mobile Documents/com~apple~CloudDocs/Estormi``). Override
with the ``ESTORMI_VAULT_DIR`` environment variable.

Layout written under the vault directory::

    manifest.json            index + generatedAt, for cheap change detection
    briefings/<date>.json    one file per daily briefing (dated history)
    engines_history.json     rolling per-engine run log (timestamps +
                             counters) — drives the iOS companion's
                             "evolution over time" charts
    engine-logs/<run>.log    full captured output of recent runs, one file
                             per run (referenced by ``logId`` in the history),
                             fetched on demand by the companion's log modal
    metrics.json             point-in-time snapshot of the whole store —
                             total chunks, per-source composition, the
                             ingestion + memory time series, and the
                             read-only source catalogue. Overwritten each
                             engine run so the companion always reads
                             current state. Built by the Mac (it reads
                             SQLite + the connector registry); this module
                             only persists it.

All writes degrade gracefully: every push returns ``True``/``False`` and
never raises, so a sync failure never breaks the pipeline.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import structlog

from memory_core.timeparse import now_iso_z

log = structlog.get_logger()

# iCloud Drive's on-disk root on macOS; ``Estormi`` is the default vault.
_ICLOUD_DRIVE = Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
_DEFAULT_VAULT = _ICLOUD_DRIVE / "Estormi"

# Guards `_ensure_folder_icon` so the costly resource-fork write only runs
# once per process — flipped True before the attempt so a failure (e.g.
# AppKit missing on Linux CI) doesn't trigger retries.
_icon_applied: bool = False


def vault_dir() -> Path:
    """Resolve the vault directory — the ``ESTORMI_VAULT_DIR`` override when
    set, otherwise the ``Estormi`` folder in the user's iCloud Drive."""
    override = os.environ.get("ESTORMI_VAULT_DIR")
    return Path(override).expanduser() if override else _DEFAULT_VAULT


# Retention budget for briefing narration audio. The dated briefing JSON is tiny
# and kept forever (the local quill trains on the full history — see the
# quill-retrain-vault-retention design), so only the heavy ``.m4a`` narrations
# are bounded: once they exceed this budget the oldest are pruned, newest kept.
# Tunable via ``ESTORMI_VAULT_MAX_AUDIO_MB``; 0 disables pruning.
_DEFAULT_AUDIO_CAP_MB = 500


def _audio_cap_bytes() -> int:
    """The briefing-audio retention budget in bytes (0 = unlimited)."""
    raw = os.environ.get("ESTORMI_VAULT_MAX_AUDIO_MB", "").strip()
    try:
        mb = int(raw) if raw else _DEFAULT_AUDIO_CAP_MB
    except ValueError:
        mb = _DEFAULT_AUDIO_CAP_MB
    return max(0, mb) * 1024 * 1024


def _enforce_audio_cap(d: Path) -> None:
    """Prune oldest briefing narration audio until the vault's ``.m4a`` footprint
    fits the configured cap. Briefing JSON is never touched — only the heavy
    audio is bounded. Oldest-first by the ISO date in the filename
    (``briefings/<date>.m4a``), so the freshest narrations always survive. A
    zero/disabled cap is a no-op. Best-effort; never raises.
    """
    cap = _audio_cap_bytes()
    if cap <= 0:
        return
    try:
        audio_dir = d / "briefings"
        # ISO date stems sort chronologically as plain strings → oldest first.
        files = sorted(audio_dir.glob("*.m4a"), key=lambda p: p.name)
        sizes = {p: p.stat().st_size for p in files}
        total = sum(sizes.values())
        if total <= cap:
            return
        freed = 0
        for p in files:  # oldest first
            if total - freed <= cap:
                break
            try:
                p.unlink()
                freed += sizes[p]
                log.info("vault audio cap: pruned %s (%d bytes)", p.name, sizes[p])
            except OSError:
                log.warning("vault audio cap: could not prune %s", p.name)
        if freed:
            log.info(
                "vault audio cap: freed %d bytes (audio now ~%d / cap %d)",
                freed,
                total - freed,
                cap,
            )
    except Exception:  # pragma: no cover — pruning must never break a write
        log.exception("vault audio cap enforcement failed")


def _atomic_write_json(path: Path, payload: Any) -> None:
    """Write ``payload`` to ``path`` atomically — temp file then rename — so
    the companion never observes a half-written file. Raises on failure.

    The temp name carries the PID so two processes writing the same target
    (e.g. the in-process briefing engine and a manually-launched ``make
    daily-dag``) can't clobber each other's ``.tmp`` and produce a
    ``FileNotFoundError`` on rename or a torn write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# A briefing file is named exactly ``YYYY-MM-DD.json``. Filtering on this stem
# keeps stray .json (editor/agent backups, exports, sidecar files) from being
# mistaken for briefings — a single ``2026-06-22.foo.bak.json`` once produced
# phantom duplicate entries and a 404 on the canonical date.
_BRIEFING_STEM_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _briefing_files(briefings_dir: Path) -> list[Path]:
    """Date-stamped briefing files (``YYYY-MM-DD.json``), newest first."""
    if not briefings_dir.is_dir():
        return []
    return sorted(
        (p for p in briefings_dir.glob("*.json") if _BRIEFING_STEM_RE.match(p.stem)),
        key=lambda x: x.stem,
        reverse=True,
    )


def _rebuild_manifest(d: Path) -> None:
    """Rewrite ``manifest.json`` from the current vault contents.

    The manifest lets the companion detect new data cheaply: one small file
    carries the freshest ``generatedAt`` and the list of available briefings.
    """
    briefings_dir = d / "briefings"
    briefing_dates = [p.stem for p in _briefing_files(briefings_dir)]
    _atomic_write_json(
        d / "manifest.json",
        {
            "generatedAt": now_iso_z(),
            "briefings": briefing_dates,
            "hasEnginesHistory": (d / "engines_history.json").is_file(),
            "hasMetrics": (d / "metrics.json").is_file(),
        },
    )


def _ensure_folder_icon(d: Path) -> None:
    """Stamp the Estormi app icon onto the vault folder so users can spot
    it in Finder and the iOS Files browser. Runs at most once per process
    and skips silently when the folder is already branded."""
    global _icon_applied
    if _icon_applied:
        return
    _icon_applied = True
    try:
        from .macos_folder_icon import find_app_icon, set_folder_icon

        # The hidden Icon\r file is NSWorkspace.setIcon's marker — its
        # presence means the folder is already branded; re-stamping would
        # just churn iCloud Drive.
        if (d / "Icon\r").exists():
            return
        icon = find_app_icon()
        if icon is None:
            return
        set_folder_icon(d, icon)
    except Exception:  # pragma: no cover — purely cosmetic
        log.exception("vault folder icon: failed to apply")


def _push(rel_path: str, payload: dict[str, Any]) -> bool:
    """Write one vault file and refresh the manifest. Never raises."""
    try:
        d = vault_dir()
        _atomic_write_json(d / rel_path, payload)
        _rebuild_manifest(d)
        _ensure_folder_icon(d)
        log.info("vault write: %s", rel_path)
        return True
    except Exception:  # pragma: no cover — defensive
        log.exception("vault write failed: %s", rel_path)
        return False


def push_briefing(briefing: dict[str, Any], notify: bool = True) -> bool:
    """Write one daily briefing as ``briefings/<date>.json`` (dated history).

    On a successful write, fires a best-effort APNs alert to the iOS companion
    — unless ``notify=False`` (a follow-up write of an already-announced
    briefing, e.g. the health refresh swapping in the regenerated audio).

    Returns True on success, False on any failure. Never raises.
    """
    name = str(briefing.get("date") or briefing.get("id") or "briefing")
    # Sanitise the filename component exactly as the sibling read/write/delete
    # helpers do: a stray '/' or '\' (or '..') in the date must not let the
    # write escape the briefings/ dir. The date is Mac-composed (ISO), so this
    # is defense-in-depth, but the four helpers must agree.
    safe = name.replace("/", "_").replace("\\", "_").strip() or "briefing"
    ok = _push(f"briefings/{safe}.json", briefing)
    if ok and notify:
        _notify_new_briefing(safe)
    return ok


def write_briefing_audio(date: str, data: bytes) -> bool:
    """Write a briefing's narration as ``briefings/<date>.m4a`` (binary, atomic).

    Paired with :func:`push_briefing`: the Mac synthesizes the audio (Voxtral —
    see ``memory_core/tts_local.py``), writes it here, then pushes the briefing
    JSON carrying ``audioPath: "briefings/<date>.m4a"``. Writing the audio first
    means the APNs "ready" alert (fired from ``push_briefing``) only goes out
    once the file the companion will play already exists. Never raises.
    """
    try:
        safe_date = date.replace("/", "_").replace("\\", "_").strip()
        if not safe_date:
            return False
        path = vault_dir() / "briefings" / f"{safe_date}.m4a"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            tmp.write_bytes(data)
            tmp.replace(path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        log.info("vault write: briefings/%s.m4a (%d bytes)", safe_date, len(data))
        # New audio just landed — enforce the retention budget (prunes oldest
        # narrations only; the dated JSON history is kept in full).
        _enforce_audio_cap(vault_dir())
        return True
    except Exception:  # pragma: no cover — defensive
        log.exception("vault write failed: briefings/%s.m4a", date)
        return False


def _notify_new_briefing(date: str) -> None:
    """Ring the iOS companion about a fresh briefing. Tries the CloudKit
    doorbell first (Apple delivers the banner — no push key on the Mac, see
    ``cloudkit_doorbell``); falls back to the direct APNs path otherwise.
    Never both: two channels would mean two banners. A silent no-op when
    neither is configured. Never raises."""
    try:
        from .apns_push import send_alert
        from .cloudkit_doorbell import send_doorbell

        title = "New briefing"
        body = f"Your briefing for {date} is ready to read."
        if send_doorbell(title, body, date):
            log.info("doorbell rang for %s — apns skipped", date)
            return
        send_alert(title, body)
    except Exception:  # pragma: no cover — a push failure must not break sync
        log.exception("briefing notification failed")


def notify_briefing_updated(date: str) -> None:
    """Nudge the iOS companion that an already-delivered briefing was *edited* on
    the Mac, so re-opening the app surfaces the change (the companion re-reads the
    vault on every foreground and on a 60 s poll — see RootView.swift).

    Uses the direct APNs banner, NOT the CloudKit doorbell: the doorbell's text
    is fixed to "new briefing" (it would mislead for an edit) and writing a new
    doorbell record would re-fire the "new briefing" subscription. A silent no-op
    when APNs isn't configured (e.g. a Store build with no push key) — the
    companion's own refresh then picks the edit up on its own. Never raises."""
    try:
        from .apns_push import send_alert

        send_alert("Briefing updated", f"Your {date} briefing was edited.")
    except Exception:  # pragma: no cover — a push failure must not break the edit
        log.exception("briefing-updated notification failed")


def read_briefing(date: str) -> dict[str, Any] | None:
    """Read one daily briefing JSON from the vault, or ``None`` if absent.

    The vault is the authoritative store of the assembled briefing body —
    the SQLite ``chunks`` table only holds the briefing sliced into
    overlapping windows for search.
    """
    try:
        safe_date = date.replace("/", "_").replace("\\", "_").strip()
        if not safe_date:
            return None
        target = vault_dir() / "briefings" / f"{safe_date}.json"
        if not target.is_file():
            return None
        return json.loads(target.read_text(encoding="utf-8"))
    except Exception:  # pragma: no cover — defensive
        log.exception("vault read failed: briefings/%s.json", date)
        return None


def list_briefings() -> list[dict[str, Any]]:
    """List the briefings on disk, newest first.

    Returns ``{date, title, generatedAt, sourceCount, videoCount, articleCount}``
    per file, skipping anything that fails to parse. Returns an empty list if
    the vault folder doesn't exist.
    """
    briefings_dir = vault_dir() / "briefings"
    out: list[dict[str, Any]] = []
    for p in _briefing_files(briefings_dir):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append(
            {
                "date": data.get("date") or p.stem,
                "title": data.get("title") or f"Briefing — {p.stem}",
                "generatedAt": data.get("generatedAt"),
                "sourceCount": data.get("sourceCount"),
                "videoCount": data.get("videoCount"),
                "articleCount": data.get("articleCount"),
            }
        )
    return out


def delete_briefing(date: str) -> bool:
    """Remove ``briefings/<date>.json`` from the vault and refresh the manifest.

    Mirrors a content-level delete to the iOS companion: the file vanishes
    from iCloud Drive and the manifest no longer lists that date, so on the
    next foreground refresh the iOS app drops the briefing from its list.

    Returns True when the file was deleted or was already absent — both
    leave the vault in the requested state. Returns False only on a real
    I/O failure. Never raises.
    """
    try:
        safe_date = date.replace("/", "_").replace("\\", "_").strip()
        if not safe_date:
            return True
        d = vault_dir()
        target = d / "briefings" / f"{safe_date}.json"
        if target.is_file():
            target.unlink()
            log.info("vault delete: briefings/%s.json", safe_date)
        # Drop the narration audio alongside the JSON, if present.
        audio = d / "briefings" / f"{safe_date}.m4a"
        audio.unlink(missing_ok=True)
        # Rebuild the manifest so the listed dates reflect reality, even if
        # the file was already absent.
        if d.is_dir():
            _rebuild_manifest(d)
        return True
    except Exception:  # pragma: no cover — defensive
        log.exception("vault delete failed: briefings/%s.json", date)
        return False


#: How many past runs the companion keeps. Each entry is a few hundred bytes;
#: 500 is ~150 KB worst-case and covers a few weeks of activity. Older runs
#: drop off as new ones arrive.
_HISTORY_MAX_RUNS = 500

#: How many per-run log files the companion keeps under ``engine-logs/``. Full
#: logs are heavy (up to ~200 KB each), so only the most recent runs carry one;
#: older files are pruned as new ones arrive. The run record's ``logId`` points
#: at the file, and the companion fetches it on demand when the user taps a row.
_ENGINE_LOG_MAX_FILES = 10


def push_engine_run(record: dict[str, Any]) -> bool:
    """Append one engine-run record to the rolling ``engines_history.json``.

    The companion reads this file to plot per-engine progress over time —
    duration, success/failure, and a handful of counters captured at the end
    of each run (e.g. ``chunks_added`` for ingestion, ``briefings_total`` for
    briefing). Old runs are trimmed so the file stays bounded.

    Schema (top-level)::

        {
          "version": 1,
          "generatedAt": "<iso utc>",
          "runs": [
            {"engine": "...", "startedAt": "...", "endedAt": "...",
             "durationMs": int, "status": "ok"|"failed",
             "counters": {...engine-specific},
             "logId": "ingestion-20260525T120000Z",  // present when this run's
                                       // full log was captured to
                                       // engine-logs/<logId>.log (recent runs)
             "vaultSyncFailed": true}  // only present when the engine
                                       // succeeded but its vault snapshot did not
            ...
          ]
        }

    Returns True on success, False on any failure. Never raises.
    """
    try:
        d = vault_dir()
        path = d / "engines_history.json"
        runs: list[dict[str, Any]] = []
        if path.is_file():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(existing, dict):
                    raw = existing.get("runs")
                    if isinstance(raw, list):
                        runs = [r for r in raw if isinstance(r, dict)]
            except Exception:
                # A corrupt or partial file shouldn't lose the new record —
                # start fresh rather than abandon the write.
                log.exception("vault history: previous file unreadable; resetting")
                runs = []
        runs.append(record)
        if len(runs) > _HISTORY_MAX_RUNS:
            runs = runs[-_HISTORY_MAX_RUNS:]
        _atomic_write_json(
            path,
            {"version": 1, "generatedAt": now_iso_z(), "runs": runs},
        )
        _rebuild_manifest(d)
        _ensure_folder_icon(d)
        log.info("vault write: engines_history.json (+1 run, total=%d)", len(runs))
        return True
    except Exception:  # pragma: no cover — defensive
        log.exception("vault write failed: engines_history.json")
        return False


def push_engine_log(run_id: str, text: str) -> bool:
    """Write one run's full log to ``engine-logs/<run_id>.log``.

    Paired with :func:`push_engine_run`: the run record stores a ``logId``
    pointing here, and the companion fetches this file on demand when the user
    opens a run in the log modal. Keeping the (potentially large) log out of the
    rolling history index keeps that file small. Only the most recent
    ``_ENGINE_LOG_MAX_FILES`` files are retained — older ones are pruned so the
    directory stays bounded.

    ``run_id`` is a filesystem-safe slug (engine + start instant); any path
    separators are stripped defensively. Returns True on success, False on any
    failure. Never raises.
    """
    try:
        safe_id = run_id.replace("/", "_").replace("\\", "_").strip()
        if not safe_id:
            return False
        d = vault_dir()
        logs_dir = d / "engine-logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        path = logs_dir / f"{safe_id}.log"
        # PID-namespaced temp so concurrent writers (app + manual daily-dag)
        # can't clobber each other's .tmp — matches _atomic_write_json.
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
        # Prune to the most recent files by modification time, with the name as
        # a tiebreaker so coarse-resolution filesystems prune deterministically
        # (run ids are timestamp-based, so name order tracks recency).
        existing = sorted(
            logs_dir.glob("*.log"),
            key=lambda p: (p.stat().st_mtime, p.name),
            reverse=True,
        )
        for stale in existing[_ENGINE_LOG_MAX_FILES:]:
            stale.unlink(missing_ok=True)
        log.info("vault write: engine-logs/%s.log", safe_id)
        return True
    except Exception:  # pragma: no cover — defensive
        log.exception("vault write failed: engine-logs/%s.log", run_id)
        return False


def push_vault_metrics(metrics: dict[str, Any]) -> bool:
    """Persist the companion metrics snapshot as ``metrics.json``.

    The Mac assembles the snapshot — total chunk count, per-source
    composition, the daily ingestion + cumulative memory time series, and
    the read-only source catalogue — and hands the finished dict here. We
    only write it: the SQLite + connector-registry reads that build it live
    in ``mcp-server`` (see ``server/jobs.py``), keeping this module a pure
    file writer.

    The snapshot is overwritten each engine run so the companion always
    reads current state. Returns True on success, False on any failure.
    Never raises.
    """
    return _push("metrics.json", metrics)


def clear_vault() -> bool:
    """Remove every companion-facing data file from the vault.

    Deletes the rolling ``engines_history.json``, the ``metrics.json``
    snapshot, the per-run ``engine-logs/`` files, and the whole ``briefings/``
    history, then rewrites ``manifest.json`` so the companion sees an empty,
    current vault rather than stale data the Mac has already wiped from its
    database. The folder itself and its Finder/Files icon (``Icon\r``) are
    preserved so the vault stays branded and the user's iCloud sync target
    doesn't churn.

    Called by the full data reset so "delete everything" actually reaches the
    companion. Returns True on success, False on any failure. Never raises.
    """
    try:
        d = vault_dir()
        if not d.exists():
            return True
        (d / "engines_history.json").unlink(missing_ok=True)
        (d / "metrics.json").unlink(missing_ok=True)
        briefings_dir = d / "briefings"
        if briefings_dir.is_dir():
            for p in briefings_dir.glob("*.json"):
                p.unlink(missing_ok=True)
            for p in briefings_dir.glob("*.m4a"):
                p.unlink(missing_ok=True)
        logs_dir = d / "engine-logs"
        if logs_dir.is_dir():
            for p in logs_dir.glob("*.log"):
                p.unlink(missing_ok=True)
        _rebuild_manifest(d)
        log.info("vault cleared")
        return True
    except Exception:  # pragma: no cover — defensive
        log.exception("vault clear failed")
        return False

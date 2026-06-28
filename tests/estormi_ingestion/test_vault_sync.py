"""Unit tests for estormi_ingestion.shared.delivery.vault_sync."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from estormi_ingestion.shared.delivery import vault_sync

pytestmark = pytest.mark.unit


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the vault at a throwaway directory for the duration of a test."""
    d = tmp_path / "Estormi"
    monkeypatch.setenv("ESTORMI_VAULT_DIR", str(d))
    return d


# ---------------------------------------------------------------------------
# push_briefing
# ---------------------------------------------------------------------------


def test_push_briefing_writes_dated_file(vault: Path) -> None:
    briefing = {
        "id": "briefing-2026-05-21",
        "date": "2026-05-21",
        "title": "Briefing — 2026-05-21",
        "htmlBody": "<h1>Test</h1>",
        "sourceCount": 12,
        "videoCount": 3,
        "generatedAt": "2026-05-22T07:00:00Z",
    }
    assert vault_sync.push_briefing(briefing) is True

    written = vault / "briefings" / "2026-05-21.json"
    assert written.is_file()
    assert json.loads(written.read_text(encoding="utf-8")) == briefing


def test_push_briefing_updates_manifest(vault: Path) -> None:
    vault_sync.push_briefing({"date": "2026-05-21", "htmlBody": "<p>x</p>"})
    manifest = json.loads((vault / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["briefings"] == ["2026-05-21"]
    assert manifest["generatedAt"]


def test_push_briefing_keeps_dated_history(vault: Path) -> None:
    vault_sync.push_briefing({"date": "2026-05-20", "htmlBody": "<p>a</p>"})
    vault_sync.push_briefing({"date": "2026-05-21", "htmlBody": "<p>b</p>"})
    manifest = json.loads((vault / "manifest.json").read_text(encoding="utf-8"))
    # Newest first.
    assert manifest["briefings"] == ["2026-05-21", "2026-05-20"]


def test_push_briefing_sanitises_path_separators(vault: Path) -> None:
    # A stray '/' or '..' in the date must not let the write escape briefings/
    # (parity with read/write-audio/delete, which all sanitise the same way).
    assert vault_sync.push_briefing({"date": "../../evil", "htmlBody": "<p>x</p>"}) is True
    # No file written outside the vault's briefings/ dir.
    assert not (vault.parent / "evil.json").exists()
    assert not (vault / "evil.json").exists()
    # The separators collapse to underscores, staying inside briefings/.
    assert (vault / "briefings" / ".._.._evil.json").is_file()


# ---------------------------------------------------------------------------
# _notify_new_briefing — doorbell first, APNs fallback, never both
# ---------------------------------------------------------------------------


def test_notify_skips_apns_when_doorbell_rings(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alerts: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "estormi_ingestion.shared.delivery.cloudkit_doorbell.send_doorbell", lambda t, b, d: True
    )
    monkeypatch.setattr(
        "estormi_ingestion.shared.delivery.apns_push.send_alert",
        lambda t, b: alerts.append((t, b)) or 1,
    )
    vault_sync._notify_new_briefing("2026-06-12")
    assert alerts == []  # one channel, one banner


def test_notify_falls_back_to_apns_when_doorbell_silent(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rings: list[str] = []
    alerts: list[tuple[str, str]] = []

    def doorbell(title: str, body: str, date: str) -> bool:
        rings.append(date)
        return False

    monkeypatch.setattr(
        "estormi_ingestion.shared.delivery.cloudkit_doorbell.send_doorbell", doorbell
    )
    monkeypatch.setattr(
        "estormi_ingestion.shared.delivery.apns_push.send_alert",
        lambda t, b: alerts.append((t, b)) or 1,
    )
    vault_sync._notify_new_briefing("2026-06-12")
    assert rings == ["2026-06-12"]  # doorbell tried first
    assert alerts == [("New briefing", "Your briefing for 2026-06-12 is ready to read.")]


def test_notify_never_raises(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object) -> bool:
        raise RuntimeError("doorbell exploded")

    monkeypatch.setattr("estormi_ingestion.shared.delivery.cloudkit_doorbell.send_doorbell", boom)
    vault_sync._notify_new_briefing("2026-06-12")  # must not propagate


# ---------------------------------------------------------------------------
# notify_briefing_updated — the edit nudge: APNs "updated" banner, no doorbell
# ---------------------------------------------------------------------------


def test_notify_updated_uses_apns_not_doorbell(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alerts: list[tuple[str, str]] = []
    rings: list[str] = []
    monkeypatch.setattr(
        "estormi_ingestion.shared.delivery.cloudkit_doorbell.send_doorbell",
        lambda t, b, d: rings.append(d) or True,
    )
    monkeypatch.setattr(
        "estormi_ingestion.shared.delivery.apns_push.send_alert",
        lambda t, b: alerts.append((t, b)) or 1,
    )
    vault_sync.notify_briefing_updated("2026-06-12")
    assert rings == []  # the edit nudge never rings the "new briefing" doorbell
    assert alerts == [("Briefing updated", "Your 2026-06-12 briefing was edited.")]


def test_notify_updated_never_raises(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object) -> int:
        raise RuntimeError("apns exploded")

    monkeypatch.setattr("estormi_ingestion.shared.delivery.apns_push.send_alert", boom)
    vault_sync.notify_briefing_updated("2026-06-12")  # must not propagate


# ---------------------------------------------------------------------------
# push_engine_run
# ---------------------------------------------------------------------------


def _run(engine: str = "ingestion", **counters: object) -> dict[str, object]:
    return {
        "engine": engine,
        "startedAt": "2026-05-25T12:00:00Z",
        "endedAt": "2026-05-25T12:01:00Z",
        "durationMs": 60_000,
        "status": "ok",
        "counters": dict(counters),
    }


def test_push_engine_run_creates_rolling_file(vault: Path) -> None:
    assert vault_sync.push_engine_run(_run(chunks_added=5)) is True
    written = json.loads((vault / "engines_history.json").read_text(encoding="utf-8"))
    assert written["version"] == 1
    assert len(written["runs"]) == 1
    assert written["runs"][0]["engine"] == "ingestion"
    assert written["runs"][0]["counters"]["chunks_added"] == 5
    # Manifest now flags history presence.
    manifest = json.loads((vault / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["hasEnginesHistory"] is True


def test_push_engine_run_appends_in_order(vault: Path) -> None:
    vault_sync.push_engine_run(_run(engine="ingestion"))
    vault_sync.push_engine_run(_run(engine="briefing"))
    runs = json.loads((vault / "engines_history.json").read_text(encoding="utf-8"))["runs"]
    assert [r["engine"] for r in runs] == ["ingestion", "briefing"]


def test_push_engine_run_trims_to_max(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Shrink the cap so the test stays cheap.
    monkeypatch.setattr(vault_sync, "_HISTORY_MAX_RUNS", 3)
    for i in range(5):
        vault_sync.push_engine_run(_run(idx=i))
    runs = json.loads((vault / "engines_history.json").read_text(encoding="utf-8"))["runs"]
    # Oldest two dropped — the newest three survive in arrival order.
    assert [r["counters"]["idx"] for r in runs] == [2, 3, 4]


def test_push_engine_run_recovers_from_corrupt_file(vault: Path) -> None:
    vault.mkdir(parents=True)
    (vault / "engines_history.json").write_text("not json {{{")
    assert vault_sync.push_engine_run(_run(engine="briefing")) is True
    runs = json.loads((vault / "engines_history.json").read_text(encoding="utf-8"))["runs"]
    assert len(runs) == 1
    assert runs[0]["engine"] == "briefing"


def test_push_leaves_no_temp_files(vault: Path) -> None:
    vault_sync.push_briefing({"date": "2026-05-21", "htmlBody": "<p>x</p>"})
    assert list(vault.rglob("*.tmp")) == []


def test_push_returns_false_on_unwritable_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A regular file stands where the vault directory's parent should be, so
    # mkdir(parents=True) fails — the push must swallow it and report False.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setenv("ESTORMI_VAULT_DIR", str(blocker / "vault"))

    assert vault_sync.push_briefing({"date": "2026-05-21"}) is False


# ---------------------------------------------------------------------------
# push_engine_log
# ---------------------------------------------------------------------------


def test_push_engine_log_writes_file(vault: Path) -> None:
    assert vault_sync.push_engine_log("ingestion-20260525T120000Z", "line one\nline two") is True
    path = vault / "engine-logs" / "ingestion-20260525T120000Z.log"
    assert path.is_file()
    assert path.read_text(encoding="utf-8") == "line one\nline two"


def test_push_engine_log_prunes_to_max(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vault_sync, "_ENGINE_LOG_MAX_FILES", 3)
    for i in range(5):
        vault_sync.push_engine_log(f"ingestion-{i:02d}", f"run {i}")
    kept = sorted(p.stem for p in (vault / "engine-logs").glob("*.log"))
    # Oldest two pruned; the newest three survive.
    assert kept == ["ingestion-02", "ingestion-03", "ingestion-04"]


def test_push_engine_log_rejects_blank_id(vault: Path) -> None:
    assert vault_sync.push_engine_log("   ", "body") is False
    assert not (vault / "engine-logs").exists()


def test_push_engine_log_leaves_no_temp_files(vault: Path) -> None:
    vault_sync.push_engine_log("ingestion-20260525T120000Z", "body")
    assert list(vault.rglob("*.tmp")) == []


# ---------------------------------------------------------------------------
# push_vault_metrics
# ---------------------------------------------------------------------------


def test_push_vault_metrics_writes_snapshot(vault: Path) -> None:
    metrics = {
        "version": 1,
        "totalChunks": 42,
        "bySource": {"notes": 30, "mail": 12},
        "sources": [{"name": "notes", "chunks": 30}],
    }
    assert vault_sync.push_vault_metrics(metrics) is True
    written = json.loads((vault / "metrics.json").read_text(encoding="utf-8"))
    assert written == metrics
    # Manifest flags the snapshot's presence so the reader can skip a miss.
    manifest = json.loads((vault / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["hasMetrics"] is True


def test_push_vault_metrics_overwrites(vault: Path) -> None:
    vault_sync.push_vault_metrics({"totalChunks": 1})
    vault_sync.push_vault_metrics({"totalChunks": 2})
    written = json.loads((vault / "metrics.json").read_text(encoding="utf-8"))
    # Overwritten, not appended — the snapshot is always current state.
    assert written == {"totalChunks": 2}


# ---------------------------------------------------------------------------
# clear_vault
# ---------------------------------------------------------------------------


def test_clear_vault_removes_snapshots_and_briefings(vault: Path) -> None:
    vault_sync.push_engine_run(_run(engine="ingestion"))
    vault_sync.push_engine_log("ingestion-20260521T020000Z", "some run output")
    vault_sync.push_vault_metrics({"totalChunks": 7})
    vault_sync.push_briefing({"date": "2026-05-21", "htmlBody": "<p>x</p>"})
    assert (vault / "engines_history.json").is_file()
    assert (vault / "engine-logs" / "ingestion-20260521T020000Z.log").is_file()
    assert (vault / "metrics.json").is_file()
    assert (vault / "briefings" / "2026-05-21.json").is_file()

    assert vault_sync.clear_vault() is True

    assert not (vault / "engines_history.json").exists()
    assert not (vault / "metrics.json").exists()
    assert list((vault / "briefings").glob("*.json")) == []
    assert list((vault / "engine-logs").glob("*.log")) == []
    # The manifest is rebuilt to reflect the now-empty vault.
    manifest = json.loads((vault / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["briefings"] == []
    assert manifest["hasEnginesHistory"] is False
    assert manifest["hasMetrics"] is False


def test_clear_vault_preserves_folder_icon(vault: Path) -> None:
    vault_sync.push_briefing({"date": "2026-05-21", "htmlBody": "<p>x</p>"})
    icon = vault / "Icon\r"
    icon.write_bytes(b"")  # the Finder/Files branding marker

    assert vault_sync.clear_vault() is True
    assert icon.exists()  # branding survives a clear


def test_clear_vault_missing_dir_is_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ESTORMI_VAULT_DIR", str(tmp_path / "never-created"))
    # Nothing to clear — succeeds without creating the folder.
    assert vault_sync.clear_vault() is True
    assert not (tmp_path / "never-created").exists()


# ---------------------------------------------------------------------------
# write_briefing_audio (Voxtral narration .m4a)
# ---------------------------------------------------------------------------


def test_write_briefing_audio_writes_binary(vault: Path) -> None:
    data = b"\x00\x01fake-m4a-bytes\xff"
    assert vault_sync.write_briefing_audio("2026-05-21", data) is True
    written = vault / "briefings" / "2026-05-21.m4a"
    assert written.is_file()
    assert written.read_bytes() == data


def test_write_briefing_audio_rejects_blank_date(vault: Path) -> None:
    assert vault_sync.write_briefing_audio("   ", b"x") is False


def test_write_briefing_audio_leaves_no_temp_files(vault: Path) -> None:
    vault_sync.write_briefing_audio("2026-05-21", b"x")
    assert list((vault / "briefings").glob("*.tmp")) == []


def test_delete_briefing_removes_audio(vault: Path) -> None:
    vault_sync.push_briefing({"date": "2026-05-21", "htmlBody": "<p>x</p>"})
    vault_sync.write_briefing_audio("2026-05-21", b"audio")
    assert (vault / "briefings" / "2026-05-21.m4a").is_file()

    assert vault_sync.delete_briefing("2026-05-21") is True
    assert not (vault / "briefings" / "2026-05-21.json").exists()
    assert not (vault / "briefings" / "2026-05-21.m4a").exists()


def test_clear_vault_removes_audio(vault: Path) -> None:
    vault_sync.push_briefing({"date": "2026-05-21", "htmlBody": "<p>x</p>"})
    vault_sync.write_briefing_audio("2026-05-21", b"audio")

    assert vault_sync.clear_vault() is True
    assert list((vault / "briefings").glob("*.m4a")) == []


# ---------------------------------------------------------------------------
# _atomic_write_json (sweep 3 D4: process-unique temp + cleanup on failure)
# ---------------------------------------------------------------------------
#
# Pre-fix ``_atomic_write_json`` used a fixed ``<name>.tmp`` temp file, so two
# processes writing the same vault target (e.g. the in-process briefing engine
# and a manually-launched ``make daily-dag``) could clobber each other's temp
# and yield a FileNotFoundError on rename or a torn write. The temp name is now
# process-unique, and a failed write cleans up its temp instead of leaving a
# stale ``.tmp`` behind.


def test_atomic_write_roundtrips(tmp_path: Path) -> None:
    target = tmp_path / "manifest.json"
    vault_sync._atomic_write_json(target, {"a": 1, "b": [1, 2]})
    assert json.loads(target.read_text()) == {"a": 1, "b": [1, 2]}


def test_atomic_write_temp_name_is_process_unique(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    target = tmp_path / "manifest.json"
    seen: dict = {}
    orig = Path.write_text

    def spy(self, *a, **k):
        seen["name"] = self.name
        return orig(self, *a, **k)

    monkeypatch.setattr(Path, "write_text", spy)
    vault_sync._atomic_write_json(target, {"x": 1})

    # Pre-fix the temp was the shared "manifest.json.tmp"; now it carries the PID
    # so concurrent writers can't collide.
    assert seen["name"] != "manifest.json.tmp"
    assert str(os.getpid()) in seen["name"]


def test_atomic_write_failed_rename_leaves_no_stale_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "manifest.json"

    def boom(self, *a, **k):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(Path, "replace", boom)
    with pytest.raises(OSError):
        vault_sync._atomic_write_json(target, {"x": 1})

    # Pre-fix a stale "<name>.tmp" was left behind; now the temp is cleaned up.
    assert list(tmp_path.glob("*.tmp")) == []
    assert not target.exists()


# ---------------------------------------------------------------------------
# read_briefing / list_briefings (the read path)
# ---------------------------------------------------------------------------


def test_read_briefing_roundtrips_a_written_briefing(vault: Path) -> None:
    briefing = {"date": "2026-05-21", "title": "T", "htmlBody": "<p>hi</p>"}
    vault_sync.push_briefing(briefing)
    assert vault_sync.read_briefing("2026-05-21") == briefing


def test_read_briefing_returns_none_when_absent(vault: Path) -> None:
    assert vault_sync.read_briefing("2099-01-01") is None


def test_read_briefing_returns_none_for_blank_date(vault: Path) -> None:
    assert vault_sync.read_briefing("   ") is None


def test_read_briefing_rejects_path_traversal(vault: Path) -> None:
    """A '../'-laden date must not escape briefings/ to read an arbitrary file.

    Plant a secret OUTSIDE the vault and try to reach it via a traversal date.
    The separators collapse to underscores, so the resolved target stays inside
    briefings/ and no such file exists — read returns None, secret unread.
    """
    secret = vault.parent / "secret.json"
    secret.write_text(json.dumps({"password": "hunter2"}), encoding="utf-8")

    # Classic traversal payloads. Each must yield None, never the secret.
    for evil in ("../secret", "../../secret", "..\\..\\secret", "/etc/passwd"):
        assert vault_sync.read_briefing(evil) is None

    # The only file the sanitised name could ever address is inside briefings/.
    assert not (vault / "briefings" / "secret.json").exists()


def test_list_briefings_empty_when_no_vault(vault: Path) -> None:
    # vault dir was never created.
    assert vault_sync.list_briefings() == []


def test_list_briefings_returns_newest_first(vault: Path) -> None:
    vault_sync.push_briefing(
        {"date": "2026-05-20", "title": "Older", "htmlBody": "<p>a</p>", "sourceCount": 1}
    )
    vault_sync.push_briefing(
        {"date": "2026-05-22", "title": "Newer", "htmlBody": "<p>b</p>", "sourceCount": 2}
    )
    listed = vault_sync.list_briefings()
    assert [b["date"] for b in listed] == ["2026-05-22", "2026-05-20"]
    assert listed[0]["title"] == "Newer"
    assert listed[0]["sourceCount"] == 2


def test_list_briefings_skips_unparseable_files(vault: Path) -> None:
    vault_sync.push_briefing({"date": "2026-05-21", "title": "Good", "htmlBody": "<p>x</p>"})
    # Plant a corrupt JSON file alongside the good one.
    (vault / "briefings" / "2026-05-19.json").write_text("{not json", encoding="utf-8")
    listed = vault_sync.list_briefings()
    assert [b["date"] for b in listed] == ["2026-05-21"]


# ---------------------------------------------------------------------------
# audio retention cap — bound .m4a footprint, keep all JSON
# ---------------------------------------------------------------------------


def test_audio_cap_bytes_reads_env_and_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ESTORMI_VAULT_MAX_AUDIO_MB", "2")
    assert vault_sync._audio_cap_bytes() == 2 * 1024 * 1024
    # Garbage and unset both fall back to the default.
    monkeypatch.setenv("ESTORMI_VAULT_MAX_AUDIO_MB", "garbage")
    assert vault_sync._audio_cap_bytes() == vault_sync._DEFAULT_AUDIO_CAP_MB * 1024 * 1024
    monkeypatch.delenv("ESTORMI_VAULT_MAX_AUDIO_MB", raising=False)
    assert vault_sync._audio_cap_bytes() == vault_sync._DEFAULT_AUDIO_CAP_MB * 1024 * 1024


def test_enforce_audio_cap_prunes_oldest_keeps_json(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio_dir = vault / "briefings"
    audio_dir.mkdir(parents=True, exist_ok=True)
    for day in ("2026-06-01", "2026-06-02", "2026-06-03"):
        (audio_dir / f"{day}.m4a").write_bytes(b"x" * 50)
        (audio_dir / f"{day}.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(vault_sync, "_audio_cap_bytes", lambda: 100)  # fits 2 of 3

    vault_sync._enforce_audio_cap(vault)

    # Oldest .m4a pruned until under cap; freshest kept.
    assert sorted(p.name for p in audio_dir.glob("*.m4a")) == [
        "2026-06-02.m4a",
        "2026-06-03.m4a",
    ]
    # Every JSON survives regardless of the audio budget.
    assert sorted(p.name for p in audio_dir.glob("*.json")) == [
        "2026-06-01.json",
        "2026-06-02.json",
        "2026-06-03.json",
    ]


def test_enforce_audio_cap_disabled_keeps_all(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    audio_dir = vault / "briefings"
    audio_dir.mkdir(parents=True, exist_ok=True)
    for day in ("2026-06-01", "2026-06-02"):
        (audio_dir / f"{day}.m4a").write_bytes(b"x" * 50)
    monkeypatch.setattr(vault_sync, "_audio_cap_bytes", lambda: 0)  # disabled

    vault_sync._enforce_audio_cap(vault)

    assert len(list(audio_dir.glob("*.m4a"))) == 2


def test_write_audio_enforces_cap(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The cap runs as a side effect of writing new narration audio."""
    monkeypatch.setattr(vault_sync, "_audio_cap_bytes", lambda: 100)  # fits 2 × 50 bytes
    for day in ("2026-06-01", "2026-06-02", "2026-06-03"):
        assert vault_sync.write_briefing_audio(day, b"x" * 50) is True
    remaining = sorted(p.name for p in (vault / "briefings").glob("*.m4a"))
    assert remaining == ["2026-06-02.m4a", "2026-06-03.m4a"]


def test_manifest_and_list_ignore_non_dated_json(vault: Path) -> None:
    """Stray .json (a foo.bak.json backup, a notes.json sidecar) must never be
    mistaken for a briefing — only YYYY-MM-DD.json files count. Regression: a
    backup file left in briefings/ produced phantom duplicate entries and a 404
    on the canonical date."""
    vault_sync.push_briefing({"date": "2026-05-21", "htmlBody": "<p>real</p>"})
    bdir = vault / "briefings"
    (bdir / "2026-05-21.foo.bak.json").write_text(
        json.dumps({"date": "2026-05-21", "htmlBody": "<p>backup</p>"})
    )
    (bdir / "notes.json").write_text(json.dumps({"date": "x"}))
    vault_sync._rebuild_manifest(vault)

    manifest = json.loads((vault / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["briefings"] == ["2026-05-21"]  # no .bak / notes phantoms
    assert [b["date"] for b in vault_sync.list_briefings()] == ["2026-05-21"]

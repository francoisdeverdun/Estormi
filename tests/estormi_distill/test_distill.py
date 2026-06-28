"""Distillation engine — archive harvest, dataset shaping, verdict plumbing."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from estormi_distill import dataset, references, trainer
from estormi_distill.paths import read_status, refs_dir, write_status

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("ESTORMI_DATA_DIR", str(tmp_path))
    yield tmp_path


@pytest.fixture
def chunk_db(tmp_path):
    db = tmp_path / "estormi.db"
    with sqlite3.connect(db) as conn:
        # Mirrors the REAL schema's relevant columns — chunk text is NOT in
        # SQLite (it lives in Qdrant; day_facts fetches it over HTTP).
        conn.execute(
            "CREATE TABLE chunks (source TEXT, title TEXT, "
            "date_ts TEXT, end_date_ts TEXT, corpus TEXT)"
        )
        conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO settings VALUES ('briefing_user_context', 'Tech lead à Clichy.')")
        for day, corpus in [
            ("2026-06-10", "world"),
            ("2026-06-10", "personal"),
            ("2026-06-11", "world"),
            ("2026-06-11", "personal"),
            ("2026-06-09", "world"),  # world only → not coverable
        ]:
            conn.execute(
                "INSERT INTO chunks VALUES ('gcal', 'Daily', "
                f"'{day}T09:45:00+02:00', '{day}T10:00:00+02:00', '{corpus}')"
            )
    return db


# ── status file ───────────────────────────────────────────────────────────────


def test_status_roundtrip_and_corruption():
    assert read_status() == {}
    write_status(phase="harvest", refs={"have": 1})
    write_status(phase="dataset")
    status = read_status()
    assert status["phase"] == "dataset"
    assert status["refs"] == {"have": 1}  # merged, not replaced
    assert "updatedAt" in status


# ── archive harvest ─────────────────────────────────────────────────────────


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """A redirected vault with a ``briefings/`` folder to drop fixtures into."""
    root = tmp_path / "vault"
    (root / "briefings").mkdir(parents=True)
    monkeypatch.setenv("ESTORMI_VAULT_DIR", str(root))
    return root


def _write_briefing(vault: Path, day: str, **extra) -> None:
    payload = {"date": day, "htmlBody": f"<h1>{day}</h1>", **extra}
    (vault / "briefings" / f"{day}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_harvest_archive_mirrors_vault_and_stamps_edits(vault):
    """Every vault briefing is mirrored into refs; a hand-edited one (carrying
    ``editedAt``) is stamped 'user-edited', the rest 'archive'."""
    _write_briefing(vault, "2026-06-10")
    _write_briefing(vault, "2026-06-11", editedAt="2026-06-11T08:00:00Z")
    _write_briefing(vault, "2026-06-12", htmlBody="")  # empty body → skipped

    assert references.harvest_archive() == 2
    refs = references.existing_references()
    assert set(refs) == {"2026-06-10", "2026-06-11"}
    assert refs["2026-06-10"]["model"] == "archive"
    assert refs["2026-06-11"]["model"] == "user-edited"


def test_harvest_archive_drops_stale_refs(vault):
    """A reference whose briefing no longer exists in the vault is removed so
    the next dataset never trains on a stale day."""
    refs_dir().mkdir(parents=True)
    (refs_dir() / "2026-01-01.json").write_text(
        json.dumps({"date": "2026-01-01", "htmlBody": "<h1>old</h1>", "referenceModel": "archive"})
    )
    _write_briefing(vault, "2026-06-10")

    assert references.harvest_archive() == 1
    assert set(references.existing_references()) == {"2026-06-10"}


def test_register_edited_reference_is_picked_up():
    """A user-edited briefing becomes the highest-quality reference, stamped
    'user-edited' so the next retrain (and the mixed-stock readout) sees it."""
    payload = references.register_edited_reference("2026-06-11", "<h1>edited</h1>")
    assert payload["referenceModel"] == "user-edited"
    assert references.existing_references()["2026-06-11"]["model"] == "user-edited"


# ── dataset ───────────────────────────────────────────────────────────────────

_REF_HTML = (
    "<h1>Briefing</h1><p>Une journée pivotée sur le daily.</p>"
    "<h2>✦ Forme du jour</h2><p>Récupération 81 % — vise la soirée pour courir, "
    "le créneau est large et la journée dense avant 18 h.</p>"
    "<h2>📅 Ma journée</h2><p>09:45–10:00 · Daily</p>"
    "<p>Le daily ouvre la journée — prépare le point budget avant, la suite ne "
    "laisse aucune fenêtre et la décision doit partir avant midi.</p>"
    "<p>À ne pas oublier : courses</p>"
    "<li>La BCE relève ses taux. → Impact: ton crédit à Clichy se renchérit. "
    "[SOURCE: Le Monde]</li>"
)


def test_dataset_builds_pairs_and_holds_out_days(chunk_db, monkeypatch):
    monkeypatch.setattr(dataset, "_whoop_text", lambda day: "Recovery 81% Sleep 8h05")
    refs_dir().mkdir(parents=True)
    for day in ("2026-06-10", "2026-06-11"):
        (refs_dir() / f"{day}.json").write_text(
            json.dumps({"htmlBody": _REF_HTML, "referenceModel": "archive"})
        )
    counters = dataset.build_dataset(chunk_db)
    assert counters["days"] == 2
    assert counters["train"] >= 3 and counters["valid"] >= 3  # one day each (tiny stock)
    assert counters["models"] == {"archive": 2}
    train_lines = (Path(dataset.dataset_dir()) / "train.jsonl").read_text().splitlines()
    sample = json.loads(train_lines[0])
    assert sample["messages"][0]["role"] == "user"
    assert "09:45–10:00 Daily" in sample["messages"][0]["content"]  # the day's own facts


def test_pairs_drops_grounded_stages_when_factless():
    """An old archived briefing whose source chunks were pruned has no facts:
    its lede/readiness/writer prompts would be empty sentinels, so only the
    impact pairs (item + profile come from the HTML) survive."""
    grounded = dataset.pairs_for_reference(
        _REF_HTML, "09:45 Daily", "courses", "Recovery 81%", "Tech lead", facts_present=True
    )
    factless = dataset.pairs_for_reference(
        _REF_HTML,
        dataset._NO_TIMELINE,
        dataset._NO_REMINDERS,
        dataset._NO_WHOOP,
        "Tech lead",
        facts_present=False,
    )
    assert 0 < len(factless) < len(grounded)
    assert all("Impact" in prompt for prompt, _ in factless)  # impact-shaped only


def test_held_out_days_deterministic_spread():
    days = [f"2026-06-{d:02d}" for d in range(1, 13)]
    held = dataset.held_out_days(days)
    assert held == dataset.held_out_days(list(reversed(days)))  # order-independent
    assert 1 <= len(held) <= 3
    assert held < set(days)


# ── trainer plumbing ──────────────────────────────────────────────────────────


def test_tooling_not_ready_in_clean_env():
    tools = trainer.tooling()
    assert tools["ready"] is False  # no venv in the isolated workspace
    assert tools["python"] == ""


def test_bootstrap_rejects_non_apple_silicon(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    monkeypatch.setattr(
        trainer.os, "uname", lambda: SimpleNamespace(sysname="Linux", machine="x86_64")
    )
    reason = asyncio.run(trainer.bootstrap_tooling())
    assert reason == "distillation tooling is Apple-Silicon-only (MLX)"


def test_bootstrap_events_yields_error_off_apple_silicon(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    monkeypatch.setattr(
        trainer.os, "uname", lambda: SimpleNamespace(sysname="Linux", machine="x86_64")
    )

    async def collect():
        return [ev async for ev in trainer.bootstrap_events()]

    # The generator short-circuits before touching disk/network on a non-arm64 box.
    assert asyncio.run(collect()) == [
        {"status": "error", "message": "distillation tooling is Apple-Silicon-only (MLX)"}
    ]


def test_distill_dir_env_override(monkeypatch, tmp_path):
    from estormi_distill import paths

    monkeypatch.setenv("ESTORMI_DISTILL_DIR", str(tmp_path / "ssd"))
    assert paths.distill_dir() == tmp_path / "ssd"
    assert paths.work_dir() == tmp_path / "ssd" / "work"
    assert trainer.tools_dir() == tmp_path / "ssd" / "tools"  # tools follows the workspace


def test_distill_dir_follows_data_dir(monkeypatch):
    """No standalone workspace knob anymore — distill derives from the single
    root storage location (the data dir)."""
    from estormi_distill import paths
    from memory_core.settings import resolve_data_dir

    monkeypatch.delenv("ESTORMI_DISTILL_DIR", raising=False)
    assert paths.distill_dir() == Path(resolve_data_dir()) / "distill"


def test_vault_briefing_count_filters_blank_dates(monkeypatch):
    monkeypatch.setattr(
        "estormi_ingestion.shared.delivery.vault_sync.list_briefings",
        lambda: [{"date": "2026-06-01"}, {"date": ""}, {"date": "2026-06-02"}],
    )
    assert references.vault_briefing_count() == 2


def test_val_loss_parsing():
    text = "Iter 1: Val loss 2.740, Val took 17s\nIter 40: Val loss 0.487, x\n"
    assert trainer._parse_val_losses(text) == [[1, 2.740], [40, 0.487]]


def test_train_progress_parses_last_cycle():
    """Live advancement reads only the most recent training cycle (the log is
    append-mode across runs) and folds the periodic evals into the ETA."""
    from estormi_distill.paths import work_dir

    log = (
        "Starting training..., iters: 285\n"  # a prior, abandoned cycle
        "Iter 1: Val loss 2.710, Val took 158.0s\n"
        "Iter 10: Train loss 1.387, Learning Rate 5.000e-05, It/sec 0.072, Tokens/sec 57.0\n"
        "Starting training..., iters: 285\n"  # the live cycle
        "Iter 1: Val loss 2.700, Val took 100.0s\n"
        "Iter 40: Val loss 1.900, Val took 100.0s\n"
        "Iter 50: Train loss 1.100, Learning Rate 5.000e-05, It/sec 0.100, Tokens/sec 60.0\n"
    )
    work = work_dir()
    work.mkdir(parents=True, exist_ok=True)
    (work / "train.log").write_text(log)
    # eta = (285-50)/0.1 + 100s × (285//40 − 50//40) = 2350 + 600 = 2950
    assert trainer.train_progress() == {
        "iter": 50,
        "totalIters": 285,
        "trainLoss": 1.1,
        "valLoss": 1.9,
        "itPerSec": 0.1,
        "etaSeconds": 2950,
    }


def test_train_progress_none_without_log():
    assert trainer.train_progress() is None


def test_progress_line_for_the_engine_log():
    """The advancement line mirrored into the distill log: train loss wins,
    falls back to val loss, and the ETA renders in minutes when known."""
    assert (
        trainer._progress_line(
            {"iter": 50, "totalIters": 285, "trainLoss": 1.1, "valLoss": 1.9, "etaSeconds": 2950}
        )
        == "training · iter 50/285 · train loss 1.100 · ~49 min left"
    )
    # No train loss yet (initial val) → val loss, no ETA.
    assert (
        trainer._progress_line(
            {"iter": 1, "totalIters": 285, "trainLoss": None, "valLoss": 2.71, "etaSeconds": None}
        )
        == "training · iter 1/285 · val loss 2.710"
    )


# ── best-checkpoint selection + contamination-free val curve ──────────────────


def test_best_iter_picks_min_loss_earliest_on_tie():
    assert trainer._best_iter([[1, 2.7], [80, 0.55], [200, 0.59], [120, 0.55]]) == 80
    assert trainer._best_iter([]) is None


def test_select_best_checkpoint_promotes_then_noop_on_final():
    from estormi_distill.paths import adapters_dir

    ad = adapters_dir()
    ad.mkdir(parents=True)
    (ad / "adapters.safetensors").write_bytes(b"FINAL")
    (ad / "0000080_adapters.safetensors").write_bytes(b"BEST80")
    (ad / "0000200_adapters.safetensors").write_bytes(b"CKPT200")
    # iter 80 is the held-out minimum → its checkpoint is promoted over the final.
    losses = [[1, 2.7], [80, 0.55], [120, 0.60], [200, 0.59], [285, 0.62]]
    assert trainer._select_best_checkpoint(ad, losses) == 80
    assert (ad / "adapters.safetensors").read_bytes() == b"BEST80"
    # When the best iteration IS the final (no numbered file) the final stays put.
    (ad / "adapters.safetensors").write_bytes(b"FINAL2")
    assert trainer._select_best_checkpoint(ad, [[40, 0.7], [285, 0.4]]) is None
    assert (ad / "adapters.safetensors").read_bytes() == b"FINAL2"


def test_parse_val_losses_uses_last_run_only():
    """The append-mode train.log interleaves restarts; only the last cycle counts."""
    log = (
        "Starting training..., iters: 285\n"  # an OOM-killed prior attempt
        "Iter 1: Val loss 2.710, Val took 158s\n"
        "Iter 40: Val loss 0.607, x\n"
        "Starting training..., iters: 285\n"  # the run that completed
        "Iter 1: Val loss 2.700, x\n"
        "Iter 80: Val loss 0.558, x\n"
        "Iter 285: Val loss 0.596, x\n"
    )
    assert trainer._parse_val_losses(log) == [[1, 2.700], [80, 0.558], [285, 0.596]]


# ── resume guard ──────────────────────────────────────────────────────────────


def test_resume_plan_resumes_same_dataset_else_fresh():
    from estormi_distill.paths import adapters_dir, dataset_dir

    ds = dataset_dir()
    ds.mkdir(parents=True)
    (ds / "train.jsonl").write_text('{"x": 1}\n')
    ad = adapters_dir()
    ad.mkdir(parents=True)
    # No marker yet → fresh run.
    assert trainer._resume_plan(ad) is None
    # Marker signature matches the current dataset + a checkpoint exists → resume.
    (ad / "0000040_adapters.safetensors").write_bytes(b"x")
    (ad / trainer._TRAIN_MARKER).write_text(json.dumps({"signature": trainer._dataset_signature()}))
    assert trainer._resume_plan(ad) == ad / "0000040_adapters.safetensors"
    # New briefings change the dataset bytes → signature mismatch → fresh.
    (ds / "train.jsonl").write_text('{"x": 2}\n')
    assert trainer._resume_plan(ad) is None
    # A completed run clears the marker so it is never resumed.
    trainer.clear_train_marker()
    assert not (ad / trainer._TRAIN_MARKER).exists()


# ── multi-stage held-out eval ────────────────────────────────────────────────


def test_stage_of_prompt_maps_each_instruction():
    assert dataset.stage_of_prompt(dataset.LEDE_INSTR.format(tl="x")) == "lede"
    assert dataset.stage_of_prompt(dataset.READINESS_INSTR.format(whoop="x", tl="y")) == "readiness"
    assert dataset.stage_of_prompt(dataset.WRITER_INSTR.format(tl="x", rem="y")) == "writer"
    assert dataset.stage_of_prompt(dataset.IMPACT_INSTR.format(profile="x", item="y")) == "impact"
    assert dataset.stage_of_prompt("something else") == "other"


def test_collect_eval_prompts_groups_and_caps_per_stage():
    from collections import Counter

    from estormi_distill.paths import dataset_dir

    ds = dataset_dir()
    ds.mkdir(parents=True)

    def mk(prompt: str) -> str:
        return json.dumps(
            {
                "messages": [
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": "x"},
                ]
            }
        )

    lines = [mk(dataset.LEDE_INSTR.format(tl=f"09:00 Daily {i}")) for i in range(6)]
    lines.append(mk(dataset.READINESS_INSTR.format(whoop="Récup 80%", tl="09:00 Daily")))
    lines.append(mk(dataset.WRITER_INSTR.format(tl="09:00 Daily", rem="courses")))
    lines.append(
        mk(dataset.IMPACT_INSTR.format(profile="Tech lead", item="La BCE relève ses taux"))
    )
    (ds / "valid.jsonl").write_text("\n".join(lines))

    counts = Counter(stage for stage, _ in trainer._collect_eval_prompts())
    assert counts["lede"] == trainer._EVAL_PER_STAGE  # capped at 4 of the 6
    assert counts["readiness"] == 1 and counts["writer"] == 1 and counts["impact"] == 1


def test_build_verdict_counts_per_stage_and_non_regression_pass():
    pairs = [("lede", "p"), ("writer", "p")]
    base = ["", "N'hésite pas à préparer le budget."]  # empty lede + coach-speak writer → 0 clean
    tuned = [
        "Réunion à 9h avec Diego, pivot sur le Data Lake.",
        "Le daily ouvre la journée, prépare le budget avant midi.",
    ]  # both clean → 2
    v = trainer._build_verdict(pairs, base, tuned, "gguf")
    assert v["baseClean"] == 0 and v["tunedClean"] == 2 and v["pass"] is True
    assert v["artifact"] == "gguf" and v["prompts"] == 2
    assert v["stages"]["lede"] == {"prompts": 1, "baseClean": 0, "tunedClean": 1}
    # A regression (tuned worse than base) fails the gate.
    assert (
        trainer._build_verdict([("lede", "p")], ["Réunion à 9h avec Diego."], [""], "adapter")[
            "pass"
        ]
        is False
    )


# ── dataset up-weighting of hand-corrected days ──────────────────────────────


def test_dataset_upweights_user_edited_days(chunk_db, monkeypatch):
    monkeypatch.setattr(dataset, "_whoop_text", lambda day: "Recovery 81%")
    refs_dir().mkdir(parents=True)

    def write_refs(first_day_model: str) -> None:
        # 06-11 is the held-out (last) day; 06-09 + 06-10 train.
        for day, model in [
            ("2026-06-09", first_day_model),
            ("2026-06-10", "archive"),
            ("2026-06-11", "archive"),
        ]:
            (refs_dir() / f"{day}.json").write_text(
                json.dumps({"htmlBody": _REF_HTML, "referenceModel": model})
            )

    write_refs("user-edited")
    edited = dataset.build_dataset(chunk_db)
    write_refs("archive")
    plain = dataset.build_dataset(chunk_db)

    assert edited["editedRepeat"] == 3
    assert edited["models"] == {"user-edited": 1, "archive": 2}
    # The hand-corrected day's pairs are repeated ×3 vs ×1 → +2× one day's pairs.
    assert edited["train"] > plain["train"]
    assert (edited["train"] - plain["train"]) % 2 == 0


def test_is_transient_gpu_fault_detects_collateral_and_oom():
    """A retry-worthy crash is a GPU fault (collateral or memory), not a bug."""
    assert trainer._is_transient_gpu_fault(
        "[METAL] Command buffer execution failed: Discarded (victim of GPU error/recovery) "
        "(00000005:kIOGPUCommandBufferCallbackErrorInnocentVictim)"
    )
    assert trainer._is_transient_gpu_fault(
        "[METAL] Command buffer execution failed: Insufficient Memory "
        "(00000008:kIOGPUCommandBufferCallbackErrorOutOfMemory)"
    )
    # A genuine error (bad data, CLI misuse) is NOT retried.
    assert not trainer._is_transient_gpu_fault("ValueError: dataset path not found")
    assert not trainer._is_transient_gpu_fault("")

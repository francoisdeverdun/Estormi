"""Entrypoint: the distillation engine's phase loop.

    python -m estormi_distill.run_distill

Launched by the server's queue (``server/launchers/distill.py``) under the
engine mutex. The chain is long (the train+fuse phase alone is ~30-60 min),
so it must never starve the scheduled engines: at every phase
boundary it asks the server whether anyone is waiting for the engine slot
and, if so, exits with ``YIELD_EXIT_CODE`` — the launcher re-enqueues it at
the back of the queue and the on-disk checkpoints (references, dataset,
adapter) make the resume free.

Exit codes: 0 done · 75 yielded (re-enqueue) · 1 failed.
"""

from __future__ import annotations

import asyncio
import logging
import sys

import httpx

from estormi_distill import dataset, references, trainer
from estormi_distill.paths import (
    MIN_FREE_GB,
    read_status,
    work_dir,
    write_status,
)
from estormi_distill.references import MIN_BRIEFINGS
from estormi_ingestion.shared.config import mcp_url

logging.basicConfig(
    level=logging.INFO,
    format="[distill] %(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stderr)],
)
log = logging.getLogger("distill")

YIELD_EXIT_CODE = 75  # EX_TEMPFAIL — the launcher re-enqueues on this code


async def _someone_waiting() -> bool:
    """True when another engine is queued for the slot this process holds."""
    base = mcp_url()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{base}/api/jobs/state")
            queue = resp.json().get("queue") or []
            return any(e.get("kind") != "distill" for e in queue)
    except Exception:  # noqa: BLE001 — unreachable server: keep working
        return False


async def _yield_if_needed(phase: str) -> None:
    if await _someone_waiting():
        write_status(phase="yielded", yieldedDuring=phase)
        log.info("yielding the engine slot during %s — will resume", phase)
        sys.exit(YIELD_EXIT_CODE)


async def run() -> int:
    # ⓪ tooling — self-bootstrap the MLX toolchain on first use so the feature
    # is available from the card without a terminal. Idempotent; skipped once
    # ready, so it only pays the ~1 GB install once.
    if not trainer.tooling()["ready"]:
        await _yield_if_needed("setup")
        write_status(phase="setup", error="")
        reason = await trainer.bootstrap_tooling()
        if reason is not None:
            write_status(phase="failed", error=reason)
            return 1

    # ① harvest — mirror the user's own briefing archive into the refs
    # workspace. The corpus is the user's curated briefings (external edits land
    # in the vault JSON we read), so there is no cloud composition and nothing
    # to seed the exemplar bank with here — the briefing-edit endpoint already
    # seeds exemplars from genuine human corrections.
    await _yield_if_needed("harvest")
    write_status(phase="harvest", error="")
    have = references.harvest_archive()
    write_status(phase="harvest", refs={"have": have})
    if have < MIN_BRIEFINGS:
        write_status(
            phase="failed",
            error=f"too few briefings to train on (need ≥{MIN_BRIEFINGS})",
        )
        return 1

    # ② dataset.
    await _yield_if_needed("dataset")
    write_status(phase="dataset")
    counters = dataset.build_dataset()
    write_status(dataset=counters)
    if counters["train"] < 20:
        write_status(phase="failed", error=f"dataset too small ({counters['train']} pairs)")
        return 1

    # ③ train — the RAM-exclusive phase; the disk check covers the fuse too.
    await _yield_if_needed("train")
    if trainer.free_gb(work_dir()) < MIN_FREE_GB:
        write_status(
            phase="failed",
            error=f"need ≥{MIN_FREE_GB} GB free for train+fuse (work dir: {work_dir()})",
        )
        return 1
    write_status(phase="train")
    training = await trainer.train(counters["train"])
    if training is None:
        write_status(phase="failed", error="training failed — see distill/work/train.log")
        return 1
    write_status(training=training)

    # ④ cheap pre-fuse gate on the adapter — a clear regression skips the
    #    expensive fuse. ``None`` means it couldn't run; the GGUF gate below is
    #    authoritative either way.
    write_status(phase="eval")
    verdict = await trainer.evaluate()
    if verdict is not None and not verdict.get("pass"):
        write_status(
            phase="rejected",
            verdict=verdict,
            error="adapter regressed on held-out days — not installed",
        )
        return 1

    # ⑤ fuse → Q4_K_M GGUF, in the work dir — not installed yet.
    await _yield_if_needed("fuse")
    write_status(phase="fuse")
    gguf = await trainer.fuse_to_gguf()
    if gguf is None:
        write_status(phase="failed", error="fuse/convert failed — see distill/work/*.log")
        return 1

    # ⑥ authoritative held-out eval on the GGUF that actually ships (quantization
    #    included); fall back to the adapter verdict when the GGUF backend is
    #    unavailable. A regression — or no verdict at all — keeps the previous quill.
    write_status(phase="eval")
    verdict = await trainer.evaluate_gguf(gguf) or verdict
    write_status(verdict=verdict)
    if verdict is None or not verdict.get("pass"):
        write_status(phase="rejected", error="the quill regressed on held-out days — not installed")
        return 1

    # ⑦ install the verified tier (the preset upgrade is automatic from here).
    write_status(phase="fuse")
    installed = trainer.install_gguf(gguf)
    if installed is None:
        write_status(phase="failed", error="install failed — see distill/work/*.log")
        return 1
    trainer.clear_train_marker()
    from memory_core.timeparse import now_iso_z  # noqa: PLC0415

    write_status(phase="done", installed=installed, lastTrainedAt=now_iso_z(), error="")
    log.info("distillation complete — %s", installed)
    return 0


def main() -> int:
    try:
        return asyncio.run(run())
    except SystemExit as exc:  # the yield path
        return int(exc.code or 0)
    except Exception:  # noqa: BLE001
        log.exception("distillation failed")
        write_status(phase="failed", error="unexpected failure — see the engine log")
        return 1


if __name__ == "__main__":
    sys.exit(main())


# Public surface for ``python -m estormi_distill.run_distill``: the entrypoints
# (main/run) plus read_status / YIELD_EXIT_CODE — the launcher mirrors the yield
# exit code by value (see server/launchers/distill.py) rather than importing it.
__all__ = ["read_status", "YIELD_EXIT_CODE", "main", "run"]

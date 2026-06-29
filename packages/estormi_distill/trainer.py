"""Phases ④⑤⑥ — QLoRA training, held-out eval, fuse → GGUF → install.

All heavy lifting runs in subprocesses against the distillation tooling (an
MLX venv that ``bootstrap_tooling`` installs on first use — never the app's
own runtime, which must stay lean) — except the GGUF eval, which runs in the
app runtime so it serves the model exactly as the briefing engine does. Every
step is checkpointed on disk so a yielded/killed engine resumes where it
stopped (training warm-starts from the latest checkpoint), training promotes
the best held-out checkpoint over the final one, and the installed tier is
only ever replaced after the fused GGUF — the artifact that ships — clears the
held-out gate across every briefing stage.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import sys
from pathlib import Path

from estormi_distill.paths import MIN_FREE_GB, adapters_dir, dataset_dir, distill_dir, work_dir
from memory_core.settings import resolve_data_dir  # the SFT GGUF installs under the data dir

log = logging.getLogger("distill")

# The MLX base the adapter trains on, and the tier filename it installs as —
# must match memory_core.llm_local's ministral3-14b-estormi catalog entry.
BASE_MODEL = os.getenv("ESTORMI_DISTILL_BASE", "mlx-community/Ministral-3-14B-Instruct-2512-4bit")
INSTALLED_GGUF = "Ministral-3-14B-Estormi-SFT-Q4_K_M.gguf"

# Training shape: ~3 epochs at batch 1, capped; lr tuned down from the
# feasibility smoke (1e-4 memorized hard by iteration 80 on a tiny set).
_LEARNING_RATE = "5e-5"
_MAX_ITERS = 500
_LORA_LAYERS = "8"
# Checkpoint + held-out eval cadence. Saving on the same beat we evaluate means
# every iteration we have a val loss for also has an adapter on disk, so
# best-checkpoint selection can pick the one that actually generalised best
# rather than the (overfit) last iteration.
# Held-out eval cadence (the expensive ~130 s val pauses) — also the
# granularity of best-checkpoint selection. Checkpoints save more often
# (_SAVE_EVERY) so a transient GPU fault loses at most a few iters before a
# retry warm-starts from the latest one; _SAVE_EVERY divides _EVAL_EVERY so
# every evaluated iteration still has a saved adapter to promote.
_EVAL_EVERY = 40
_SAVE_EVERY = 20
# Retry budget for transient GPU faults. A 14B QLoRA on a 16 GB Mac shares the
# GPU with the live app's WebView and the rest of the desktop, so its command
# buffer can be discarded as an "innocent victim" of another process's GPU
# error/recovery (or hit momentary memory pressure). Those faults are
# transient, so re-run — warm-started from the latest checkpoint — rather than
# failing the whole distillation; a short backoff lets the spike subside.
_MAX_TRAIN_ATTEMPTS = 3
_RETRY_BACKOFF_S = 20
_TRANSIENT_GPU_FAULT = re.compile(
    r"Insufficient Memory|GPU error/recovery|InnocentVictim|Command buffer execution failed"
)
# Memory budget — a 14B QLoRA must coexist with the running app (Tauri WebView +
# server) inside a 16 GB Mac's ~10.7 GB Metal "wired" limit. `--grad-checkpoint`
# trades ~20-30% train speed to recompute activations instead of holding them
# (the difference between an ~11.8 GB peak that OOMs and one that fits). The seq
# cap is set from the data, not guessed: the longest harvested pair is ~460
# tokens, so 1024 is 2× headroom with zero truncation while shrinking the
# activation footprint further.
_MAX_SEQ_LEN = "1024"
_GRAD_CHECKPOINT = True
_TRAIN_TIMEOUT_S = 3 * 3600
_FUSE_TIMEOUT_S = 2 * 3600


def tools_dir() -> Path:
    override = os.getenv("ESTORMI_DISTILL_TOOLS", "").strip()
    return Path(override) if override else distill_dir() / "tools"


def tooling() -> dict:
    """What the distillation chain needs and whether it is present.

    Served by the status API so the card can show its state; when not ready, the
    chain's phase ⓪ (``bootstrap_tooling``) installs it on the next run.
    """
    venv_python = tools_dir() / "venv" / "bin" / "python"
    mlx = tools_dir() / "venv" / "bin" / "mlx_lm"
    quantize = shutil.which("llama-quantize") or (
        "/opt/homebrew/bin/llama-quantize"
        if Path("/opt/homebrew/bin/llama-quantize").exists()
        else ""
    )
    convert = tools_dir() / "llama.cpp" / "convert_hf_to_gguf.py"
    return {
        "python": str(venv_python) if venv_python.exists() else "",
        "mlx_lm": str(mlx) if mlx.exists() else "",
        "quantize": quantize,
        "convert": str(convert) if convert.exists() else "",
        "ready": bool(venv_python.exists() and mlx.exists() and quantize),
    }


def free_gb(path: Path) -> float:
    usage = shutil.disk_usage(path)
    return usage.free / 2**30


def _tool_env() -> dict:
    return {**os.environ, "HF_HOME": str(tools_dir() / "hf")}


async def _run_tool(cmd: list[str], timeout: float, log_name: str) -> int:
    """Run one tooling subprocess, its output teed to ``work/<log_name>``."""
    work_dir().mkdir(parents=True, exist_ok=True)
    log_path = work_dir() / log_name
    with open(log_path, "ab", buffering=0) as out:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=out, stderr=asyncio.subprocess.STDOUT, env=_tool_env()
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            log.warning("tool timed out: %s (see %s)", cmd[0], log_path)
            return -1
    return proc.returncode if proc.returncode is not None else -1


_SETUP_TIMEOUT_S = 30 * 60  # the pip wheels (torch ≈ ~1 GB) dominate this phase


async def bootstrap_events():
    """Install the MLX toolchain, yielding progress events for the card's stream.

    Mirrors ``scripts/setup_distill.sh`` so the Download button can install it
    without a terminal: a python venv with mlx-lm + GGUF deps, a shallow
    ``llama.cpp`` clone for the converter, and the ``llama-quantize`` binary.
    Idempotent — present steps are skipped. Each event is a dict; terminal events
    carry ``status='done'`` or ``status='error'`` (plus a ``message``). The deps
    (~1 GB) live under the data dir, never in the app's own runtime.
    """
    uname = os.uname()
    if (uname.sysname, uname.machine) != ("Darwin", "arm64"):
        yield {"status": "error", "message": "distillation tooling is Apple-Silicon-only (MLX)"}
        return
    tools = tools_dir()
    tools.mkdir(parents=True, exist_ok=True)
    venv_python = tools / "venv" / "bin" / "python"
    if not venv_python.exists():
        yield {"message": "Creating the MLX venv…", "progress": 5}
        # This interpreter (the bundled 3.12 in prod, the dev .venv otherwise) —
        # never a stray ``python3`` on PATH, which can be an old Xcode 3.9 that
        # mlx-lm won't run on.
        base_python = sys.executable
        if await _run_tool([base_python, "-m", "venv", str(tools / "venv")], 300, "setup.log"):
            yield {
                "status": "error",
                "message": "could not create the MLX venv (distill/work/setup.log)",
            }
            return
    yield {"message": "Installing MLX wheels (~1 GB, a few minutes)…", "progress": 25}
    # Pin mlx-lm to the version the app runtime locks (requirements.lock) so the
    # ``mlx_lm.lora`` / ``mlx_lm.fuse`` CLI flags this trainer passes stay valid —
    # an unpinned ``--upgrade`` could pull a release that renames a flag and make
    # training exit nonzero with a generic failure. The other wheels here are not
    # flag-sensitive.
    if await _run_tool(
        [
            str(venv_python),
            "-m",
            "pip",
            "install",
            "-q",
            "mlx-lm==0.31.3",
            "gguf",
            "sentencepiece",
            "torch",
        ],
        _SETUP_TIMEOUT_S,
        "setup.log",
    ):
        yield {
            "status": "error",
            "message": "could not install the MLX wheels — check the network (distill/work/setup.log)",
        }
        return
    if not (tools / "llama.cpp" / "convert_hf_to_gguf.py").exists():
        yield {"message": "Cloning the llama.cpp converter…", "progress": 85}
        shutil.rmtree(tools / "llama.cpp", ignore_errors=True)
        if await _run_tool(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "https://github.com/ggml-org/llama.cpp",
                str(tools / "llama.cpp"),
            ],
            900,
            "setup.log",
        ):
            yield {
                "status": "error",
                "message": "could not clone llama.cpp — check the network (distill/work/setup.log)",
            }
            return
    if not (shutil.which("llama-quantize") or Path("/opt/homebrew/bin/llama-quantize").exists()):
        brew = shutil.which("brew")
        if not brew:
            yield {
                "status": "error",
                "message": "llama-quantize missing and Homebrew not found — install Homebrew, then retry",
            }
            return
        yield {"message": "Installing llama-quantize via Homebrew…", "progress": 92}
        if await _run_tool([brew, "install", "llama.cpp"], 1800, "setup.log"):
            yield {
                "status": "error",
                "message": "could not install llama-quantize via Homebrew (distill/work/setup.log)",
            }
            return
    if tooling()["ready"]:
        yield {"message": "✓ Toolchain ready", "progress": 100, "status": "done"}
    else:
        yield {"status": "error", "message": "tooling still incomplete after setup"}


async def bootstrap_tooling() -> str | None:
    """Run :func:`bootstrap_events` to completion (engine phase ⓪ / scheduled path).

    Returns ``None`` on success, else the first error message. The card's Download
    button streams :func:`bootstrap_events` directly for live progress instead.
    """
    async for event in bootstrap_events():
        if event.get("status") == "error":
            return str(event.get("message") or "setup failed")
    return None


_PROGRESS_LOG_EVERY_S = 30  # cadence at which advancement is mirrored to the engine log


def _progress_line(p: dict) -> str:
    """Format one :func:`train_progress` snapshot as an engine-log line."""
    if p["trainLoss"] is not None:
        loss = f"train loss {p['trainLoss']:.3f}"
    elif p["valLoss"] is not None:
        loss = f"val loss {p['valLoss']:.3f}"
    else:
        loss = "warming up"
    eta = f" · ~{round(p['etaSeconds'] / 60)} min left" if p["etaSeconds"] else ""
    return f"training · iter {p['iter']}/{p['totalIters'] or '?'} · {loss}{eta}"


async def _log_train_progress(running: asyncio.Task) -> None:
    """Mirror the trainer's per-iteration advancement into the engine log while
    ``running`` is in flight, so the distill log view can track the task.

    mlx_lm only reports every few iterations, so emit one line per *new*
    iteration rather than on every poll. Best-effort: never let a parse hiccup
    take down the training it is only narrating.
    """
    seen = -1
    while not running.done():
        await asyncio.sleep(_PROGRESS_LOG_EVERY_S)
        try:
            p = train_progress()
        except Exception:  # noqa: BLE001 — narration must never fail the run
            continue
        if not p or p["iter"] == seen:
            continue
        seen = p["iter"]
        log.info("%s", _progress_line(p))


_TRAIN_MARKER = "train_marker.json"  # in adapters_dir; fingerprints the in-flight run
_CKPT_GLOB = "[0-9]" * 7 + "_adapters.safetensors"  # mlx_lm numbered checkpoints


def _dataset_signature() -> str:
    """Content fingerprint of the built training set (``train.jsonl``).

    A *content* hash, not size+mtime: ``run_distill`` rebuilds the dataset on
    every (re)entry, so an interrupted run that resumes regenerates an identical
    file — same hash → resume is allowed. New briefings change the bytes →
    different hash → a fresh run. Empty when the file is missing.
    """
    try:
        return hashlib.sha256((dataset_dir() / "train.jsonl").read_bytes()).hexdigest()
    except OSError:
        return ""


def _latest_checkpoint(adapters: Path) -> Path | None:
    cks = sorted(adapters.glob(_CKPT_GLOB))
    return cks[-1] if cks else None


def _resume_plan(adapters: Path) -> Path | None:
    """Latest checkpoint to warm-start from, or ``None`` to train fresh.

    Resumes only an *interrupted* run on the *same* dataset: the marker records
    the dataset signature when a fresh run begins and is cleared on a completed
    install, so a new dataset (new briefings) or a finished run always starts
    fresh — never warm-starts from a stale or foreign adapter.
    """
    sig = _dataset_signature()
    if not sig:
        return None
    try:
        prev = json.loads((adapters / _TRAIN_MARKER).read_text()).get("signature")
    except Exception:  # noqa: BLE001 — absent/corrupt marker = fresh run
        return None
    ckpt = _latest_checkpoint(adapters)
    return ckpt if (ckpt is not None and prev == sig) else None


def clear_train_marker() -> None:
    """Drop the in-flight marker so a *completed* run is never resumed."""
    (adapters_dir() / _TRAIN_MARKER).unlink(missing_ok=True)


def _best_iter(val_losses: list[list[float]]) -> int | None:
    """Iteration with the lowest held-out loss (ties → earliest)."""
    if not val_losses:
        return None
    return int(min(val_losses, key=lambda iv: (iv[1], iv[0]))[0])


def _select_best_checkpoint(adapters: Path, val_losses: list[list[float]]) -> int | None:
    """Promote the lowest-val-loss checkpoint to ``adapters.safetensors`` so the
    fuse ships the adapter that generalised best, not the (often overfit) last
    iteration. No-op — returns ``None`` — when the best iteration *is* the final
    one (saved unnumbered) or its checkpoint file is missing.
    """
    best = _best_iter(val_losses)
    if best is None:
        return None
    ckpt = adapters / f"{best:07d}_adapters.safetensors"
    if not ckpt.exists():
        return None  # best iteration is the final adapter — already in place
    shutil.copyfile(ckpt, adapters / "adapters.safetensors")
    return best


def _is_transient_gpu_fault(log_tail: str) -> bool:
    """Whether a failed training attempt is worth retrying: the GPU faulted —
    often as collateral of another process's error on a shared 16 GB GPU — rather
    than a bug in our run (bad args, dataset). Read from the train.log tail."""
    return bool(_TRANSIENT_GPU_FAULT.search(log_tail))


async def train(train_count: int) -> dict | None:
    """QLoRA-train the adapter on the workspace dataset. None on failure.

    Each attempt warm-starts from the latest on-disk checkpoint, so a cross-run
    resume (the engine yielded and came back on the same dataset) and an in-run
    retry after a transient GPU fault both continue rather than restart from
    iter 1. After training, the best held-out checkpoint is promoted over the
    final one.
    """
    tools = tooling()
    if not tools["ready"]:
        log.error("training tooling missing after bootstrap — see distill/work/setup.log")
        return None
    iters = min(_MAX_ITERS, max(60, 3 * train_count))
    adapters = adapters_dir()
    adapters.mkdir(parents=True, exist_ok=True)
    if _resume_plan(adapters) is None:
        # Fresh run: clear any prior adapter so a past run's weights never leak in.
        for stale in adapters.glob("*adapters.safetensors"):
            stale.unlink(missing_ok=True)
    (adapters / _TRAIN_MARKER).write_text(
        json.dumps({"signature": _dataset_signature(), "iters": iters})
    )
    base_cmd = [
        tools["mlx_lm"],
        "lora",
        "--model",
        BASE_MODEL,
        "--train",
        "--data",
        str(dataset_dir()),
        "--batch-size",
        "1",
        "--num-layers",
        _LORA_LAYERS,
        "--iters",
        str(iters),
        "--learning-rate",
        _LEARNING_RATE,
        "--adapter-path",
        str(adapters),
        "--max-seq-length",
        _MAX_SEQ_LEN,
        "--steps-per-eval",
        str(_EVAL_EVERY),
        "--save-every",
        str(_SAVE_EVERY),
    ]
    if _GRAD_CHECKPOINT:
        base_cmd.append("--grad-checkpoint")

    rc = -1
    attempt = 0
    for attempt in range(1, _MAX_TRAIN_ATTEMPTS + 1):
        if attempt > 1:
            await asyncio.sleep(_RETRY_BACKOFF_S)  # let a transient GPU spike subside
        ckpt = _latest_checkpoint(adapters)  # warm-start from the newest checkpoint, if any
        cmd = base_cmd + (["--resume-adapter-file", str(ckpt)] if ckpt else [])
        log.info(
            "training · %d iters on %d pairs · attempt %d/%d%s (loading model…)",
            iters,
            train_count,
            attempt,
            _MAX_TRAIN_ATTEMPTS,
            f" · warm-start {ckpt.name}" if ckpt else "",
        )
        # Run the training subprocess while a sibling task narrates its advancement
        # into the engine log (the distill log view is where a run is tracked).
        running = asyncio.create_task(_run_tool(cmd, _TRAIN_TIMEOUT_S, "train.log"))
        reporter = asyncio.create_task(_log_train_progress(running))
        try:
            rc = await running
        finally:
            reporter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reporter
        if rc == 0 and (adapters / "adapters.safetensors").exists():
            break
        tail = (work_dir() / "train.log").read_text(errors="replace")[-4000:]
        if attempt < _MAX_TRAIN_ATTEMPTS and _is_transient_gpu_fault(tail):
            log.warning(
                "transient GPU fault (rc=%s) on attempt %d/%d — retrying, warm-starting from "
                "the latest checkpoint",
                rc,
                attempt,
                _MAX_TRAIN_ATTEMPTS,
            )
            continue
        log.error("training failed (rc=%s)", rc)
        return None

    losses = _parse_val_losses((work_dir() / "train.log").read_text(errors="replace"))
    chosen = _select_best_checkpoint(adapters, losses)
    if chosen is not None:
        log.info("kept the best held-out checkpoint (iter %d) over the final adapter", chosen)
    return {
        "iters": iters,
        "valLosses": losses,
        "chosenIter": chosen,
        "attempts": attempt,
    }


_VAL_LOSS_RE = re.compile(r"Iter (\d+): Val loss ([\d.]+)")


def _parse_val_losses(log_text: str) -> list[list[float]]:
    """Val-loss curve of the LAST training cycle only.

    ``train.log`` is append-mode across yields/restarts, so the raw file
    interleaves the (Iter 1, Iter 40 …) ladders of every prior attempt. Slice to
    the final ``Starting training`` marker — the same isolation
    :func:`train_progress` does — before parsing, so the persisted curve is one
    run, not a contaminated union of all of them.
    """
    marks = list(_ITERS_TOTAL_RE.finditer(log_text))
    if marks:
        log_text = log_text[marks[-1].start() :]
    return [[int(m.group(1)), float(m.group(2))] for m in _VAL_LOSS_RE.finditer(log_text)]


# Live advancement, parsed from the same train.log the trainer tees. mlx_lm
# prints one Train-loss line every report step, a Val-loss line every
# steps-per-eval, and announces the iteration target up front.
_ITERS_TOTAL_RE = re.compile(r"Starting training\.\.\., iters: (\d+)")
_ITER_TRAIN_RE = re.compile(r"Iter (\d+): Train loss ([\d.]+).*?It/sec ([\d.]+)")
_ITER_VAL_RE = re.compile(r"Iter (\d+): Val loss ([\d.]+)")
_VAL_TOOK_RE = re.compile(r"Val took ([\d.]+)s")
_STEPS_PER_EVAL = _EVAL_EVERY  # mirrors the --steps-per-eval passed in train()


def train_progress(tail_bytes: int = 1 << 16) -> dict | None:
    """Live training advancement from the tail of ``work/train.log``.

    Returns the latest iteration, the run's iteration target, recent train/val
    losses and an ETA for the **train phase** (the long pole), or ``None`` when
    no training has been logged yet. The log is append-mode across runs, so only
    the most recent training cycle is considered. The ETA folds in the periodic
    held-out evals (each pauses training for ~``Val took``s), which otherwise
    make a raw iters/sec extrapolation read optimistic.
    """
    log_path = work_dir() / "train.log"
    try:
        with open(log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - tail_bytes))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    starts = list(_ITERS_TOTAL_RE.finditer(text))
    total = int(starts[-1].group(1)) if starts else None
    if starts:  # ignore a prior cycle's lines bleeding in from the tail window
        text = text[starts[-1].start() :]
    train_hits = list(_ITER_TRAIN_RE.finditer(text))
    val_hits = list(_ITER_VAL_RE.finditer(text))
    if not train_hits and not val_hits:
        return None
    iter_now = 0
    train_loss = it_per_sec = None
    if train_hits:
        last = train_hits[-1]
        iter_now = int(last.group(1))
        train_loss = float(last.group(2))
        it_per_sec = float(last.group(3))
    val_loss = float(val_hits[-1].group(2)) if val_hits else None
    iter_now = max(iter_now, int(val_hits[-1].group(1)) if val_hits else 0)
    took = _VAL_TOOK_RE.findall(text)
    val_secs = float(took[-1]) if took else None
    eta = None
    if total and it_per_sec and iter_now < total:
        remaining = (total - iter_now) / it_per_sec
        if val_secs:  # held-out evals still ahead of us pause training
            remaining += val_secs * (total // _STEPS_PER_EVAL - iter_now // _STEPS_PER_EVAL)
        eta = round(remaining)
    return {
        "iter": iter_now,
        "totalIters": total,
        "trainLoss": train_loss,
        "valLoss": val_loss,
        "itPerSec": it_per_sec,
        "etaSeconds": eta,
    }


# Held-out generation eval. Two backends share the prompt set and the scoring:
# the MLX base + LoRA adapter (fast, pre-fuse pre-gate) and the fused Q4_K_M
# GGUF that actually ships (authoritative, post-fuse). Each loads its model ONCE
# in a subprocess and generates every held-out prompt — per-prompt CLI calls
# would reload the multi-GB model each time. Scoring is a per-stage
# degeneration floor (``vision_lint.stage_issues``): a candidate must come back
# with at least as many lint-clean outputs as the base. Style is what the
# adapter is FOR — the floor only guards against collapse, it does not score taste.
_EVAL_PER_STAGE = 4  # held-out prompts evaluated per stage (lede/readiness/writer/impact)

_ADAPTER_EVAL_SCRIPT = r"""
import json, sys
from mlx_lm import load, generate
prompts = json.load(open(sys.argv[1]))
adapter = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] != "-" else None
model, tokenizer = load(sys.argv[3], adapter_path=adapter)
out = []
for p in prompts:
    text = tokenizer.apply_chat_template(
        [{"role": "user", "content": p}], add_generation_prompt=True
    )
    out.append(generate(model, tokenizer, prompt=text, max_tokens=160))
print(json.dumps(out, ensure_ascii=False))
"""

# Runs in the APP runtime (llama-cpp-python), not the MLX venv: it serves the
# GGUF with the same decode contract as the briefing engine — the model's own
# embedded chat template (chat_format=None), greedy — so the verdict reflects
# the artifact that ships, quantization included. The context window is a fixed
# 4096 (vs the briefing engine's larger _LLM_LADDER), ample for the short
# held-out eval prompts.
_GGUF_EVAL_SCRIPT = r"""
import json, sys
from llama_cpp import Llama
prompts = json.load(open(sys.argv[1]))
llm = Llama(model_path=sys.argv[2], n_ctx=4096, n_gpu_layers=-1, n_batch=512, verbose=False)
out = []
for p in prompts:
    r = llm.create_chat_completion(
        messages=[{"role": "user", "content": p}], max_tokens=160, temperature=0.0, seed=0
    )
    out.append(r["choices"][0]["message"]["content"])
print(json.dumps(out, ensure_ascii=False))
"""


def _collect_eval_prompts() -> list[tuple[str, str]]:
    """Held-out ``(stage, prompt)`` pairs from valid.jsonl, capped per stage."""
    from estormi_distill.dataset import stage_of_prompt  # noqa: PLC0415

    valid_path = dataset_dir() / "valid.jsonl"
    if not valid_path.exists():
        return []
    per_stage: dict[str, int] = {}
    pairs: list[tuple[str, str]] = []
    for line in valid_path.read_text().splitlines():
        try:
            prompt = json.loads(line)["messages"][0]["content"]
        except (json.JSONDecodeError, KeyError, IndexError):
            continue
        stage = stage_of_prompt(prompt)
        if per_stage.get(stage, 0) >= _EVAL_PER_STAGE:
            continue
        per_stage[stage] = per_stage.get(stage, 0) + 1
        pairs.append((stage, prompt))
    return pairs


def _clean(text: str) -> str:
    """Strip a markdown bold/quote wrapper a model sometimes adds, before lint."""
    return (text or "").strip().strip("*").strip()


def _build_verdict(
    pairs: list[tuple[str, str]], base: list[str], tuned: list[str], artifact: str
) -> dict:
    """Per-stage lint-clean counts → the verdict dict the status file carries.

    ``pass`` is non-regression (tuned clean ≥ base clean): the gate keeps a
    fine-tune from shipping degenerate, it does not certify it is *better*.
    """
    from estormi_briefing.lint.vision_lint import stage_issues  # noqa: PLC0415

    stages: dict[str, dict] = {}
    base_clean = tuned_clean = 0
    for (stage, _), b, t in zip(pairs, base, tuned):
        s = stages.setdefault(stage, {"prompts": 0, "baseClean": 0, "tunedClean": 0})
        s["prompts"] += 1
        if not stage_issues(stage, _clean(b)):
            s["baseClean"] += 1
            base_clean += 1
        if not stage_issues(stage, _clean(t)):
            s["tunedClean"] += 1
            tuned_clean += 1
    return {
        "prompts": len(pairs),
        "baseClean": base_clean,
        "tunedClean": tuned_clean,
        "artifact": artifact,
        "stages": stages,
        "samples": {"base": base[:2], "tuned": tuned[:2]},
        "pass": tuned_clean >= base_clean,
    }


def _write_eval_inputs(
    pairs: list[tuple[str, str]], script_text: str, name: str
) -> tuple[Path, Path]:
    work = work_dir()
    work.mkdir(parents=True, exist_ok=True)
    prompts_file = work / "eval_prompts.json"
    prompts_file.write_text(json.dumps([p for _, p in pairs], ensure_ascii=False))
    script = work / name
    script.write_text(script_text)
    return prompts_file, script


async def _generate(
    python: str, script: Path, prompts_file: Path, *model_args: str
) -> list[str] | None:
    """Run one generation subprocess; its last stdout line is the JSON array."""
    proc = await asyncio.create_subprocess_exec(
        python,
        str(script),
        str(prompts_file),
        *model_args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=_tool_env(),
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30 * 60)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(stdout.decode().strip().splitlines()[-1])
    except Exception:  # noqa: BLE001
        return None


async def evaluate() -> dict | None:
    """Cheap pre-fuse gate: MLX base vs base+adapter on every held-out stage.

    ``None`` when the tooling or the valid set is unavailable.
    """
    tools = tooling()
    pairs = _collect_eval_prompts()
    if not tools["ready"] or not pairs:
        return None
    prompts_file, script = _write_eval_inputs(pairs, _ADAPTER_EVAL_SCRIPT, "eval_generate.py")
    base = await _generate(tools["python"], script, prompts_file, "-", BASE_MODEL)
    tuned = await _generate(tools["python"], script, prompts_file, str(adapters_dir()), BASE_MODEL)
    if base is None or tuned is None:
        return None
    return _build_verdict(pairs, base, tuned, "adapter")


async def evaluate_gguf(tuned_gguf: str) -> dict | None:
    """Authoritative gate: A/B the stock Ministral GGUF against the fused tuned
    GGUF over the SAME llama-cpp serving path the briefing engine uses (embedded
    chat template, quantization included), across every held-out stage.

    ``None`` when the base GGUF or the llama-cpp runtime is unavailable — the
    caller then falls back to the cheaper adapter verdict. The two models are
    generated sequentially, one resident at a time, so it is safe on a 16 GB box.
    """
    from memory_core.llm_local import model_file_path  # noqa: PLC0415

    pairs = _collect_eval_prompts()
    # A/B against the model the briefing ACTUALLY uses: once a distilled quill is
    # installed, the briefing prefers it, so the non-regression gate must compare
    # against THAT — not the stock base — or a retrain worse than the live quill
    # but still ≥ stock would silently demote it. Fall back to stock on the first
    # run (before any Estormi-SFT quill exists).
    base_gguf = model_file_path("ministral3-14b-estormi")
    if not Path(base_gguf).exists():
        base_gguf = model_file_path("ministral3-14b")
    if not pairs or not Path(base_gguf).exists() or not Path(tuned_gguf).exists():
        return None
    prompts_file, script = _write_eval_inputs(pairs, _GGUF_EVAL_SCRIPT, "eval_gguf.py")
    base = await _generate(sys.executable, script, prompts_file, base_gguf)
    if base is None:
        return None
    tuned = await _generate(sys.executable, script, prompts_file, tuned_gguf)
    if tuned is None:
        return None
    return _build_verdict(pairs, base, tuned, "gguf")


async def fuse_to_gguf() -> str | None:
    """Fuse adapter → F16 GGUF → Q4_K_M, in the work dir. NOT yet installed.

    Returns the path to the quantized GGUF (so the eval can A/B the artifact
    that will actually ship before :func:`install_gguf` moves it into place), or
    None on any failure. The heavy F16/fused intermediates are reclaimed here so
    the GGUF eval that follows has the RAM and disk to load two 14B models.
    """
    tools = tooling()
    work = work_dir()
    if free_gb(work) < MIN_FREE_GB:
        log.error("fuse needs ≥%d GB free in %s", MIN_FREE_GB, work)
        return None
    fused = work / "fused-mlx"
    f16 = work / "Ministral-3-14B-Estormi-SFT-F16.gguf"
    quantized = work / INSTALLED_GGUF
    for stale in (fused, f16, quantized):
        if stale.is_dir():
            shutil.rmtree(stale, ignore_errors=True)
        elif stale.exists():
            stale.unlink()

    rc = await _run_tool(
        [
            tools["mlx_lm"],
            "fuse",
            "--model",
            BASE_MODEL,
            "--adapter-path",
            str(adapters_dir()),
            "--save-path",
            str(fused),
            "--dequantize",
            "--export-gguf",
            "--gguf-path",
            str(f16),
        ],
        _FUSE_TIMEOUT_S,
        "fuse.log",
    )
    if rc != 0 or not f16.exists():
        # Fallback: plain dequantized fuse, then llama.cpp's converter.
        log.info("direct GGUF export unavailable — converting via llama.cpp")
        rc = await _run_tool(
            [
                tools["mlx_lm"],
                "fuse",
                "--model",
                BASE_MODEL,
                "--adapter-path",
                str(adapters_dir()),
                "--save-path",
                str(fused),
                "--dequantize",
            ],
            _FUSE_TIMEOUT_S,
            "fuse.log",
        )
        if rc != 0:
            return None
        if not tools["convert"]:
            log.error("convert_hf_to_gguf.py missing — rerun scripts/setup_distill.sh")
            return None
        rc = await _run_tool(
            [
                tools["python"],
                tools["convert"],
                str(fused),
                "--outfile",
                str(f16),
                "--outtype",
                "f16",
            ],
            _FUSE_TIMEOUT_S,
            "convert.log",
        )
        if rc != 0 or not f16.exists():
            return None

    rc = await _run_tool(
        [tools["quantize"], str(f16), str(quantized), "Q4_K_M"], _FUSE_TIMEOUT_S, "quantize.log"
    )
    if rc != 0 or not quantized.exists():
        return None
    with open(quantized, "rb") as f:
        if f.read(4) != b"GGUF":
            log.error("quantized output is not a GGUF")
            return None
    # Reclaim the heavy intermediates now (train/fuse logs stay for diagnosis);
    # the GGUF eval that follows needs the RAM/disk back.
    shutil.rmtree(fused, ignore_errors=True)
    f16.unlink(missing_ok=True)
    return str(quantized)


def install_gguf(quantized: str) -> str | None:
    """Move the fused-and-verified GGUF into the models dir as the local tier.

    Called only after the held-out eval clears the artifact. The previous
    installed model survives as ``.prev`` for manual recovery (rename it back by
    hand). Returns the installed path, or None (existing tier left untouched).
    """
    src = Path(quantized)
    if not src.exists():
        return None
    dest = Path(resolve_data_dir()) / "models" / INSTALLED_GGUF
    dest.parent.mkdir(parents=True, exist_ok=True)
    if free_gb(dest.parent) < src.stat().st_size / 2**30 + 1:
        log.error("not enough space in the models dir for the fused tier")
        return None
    if dest.exists() or dest.is_symlink():
        prev = dest.with_suffix(dest.suffix + ".prev")
        prev.unlink(missing_ok=True)
        dest.rename(prev)
    shutil.move(str(src), dest)
    log.info("installed %s", dest)
    return str(dest)

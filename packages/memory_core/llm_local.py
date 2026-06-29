"""In-process LLM inference via llama-cpp-python (Metal-accelerated)."""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

import structlog

_log = structlog.get_logger(__name__)
_llm = None
_llm_lock = asyncio.Lock()
# Serialises actual inference. llama.cpp is NOT thread-safe for concurrent
# decode on a single context/model: the briefing summarises sources
# concurrently (asyncio.gather under a semaphore), and each call hops onto a
# worker thread via asyncio.to_thread. Two decodes racing on the shared model
# corrupt the KV-cache / Metal buffers and crash the process
# (SIGSEGV in llama_kv_cache / "tensor buffer not set" / SIGABRT in set_rows).
# One in-flight decode at a time — the single-context model can't parallelise
# anyway, so this costs nothing and is the difference between working and
# crashing on every local-provider briefing.
_infer_lock = asyncio.Lock()
# The {n_ctx, n_gpu_layers} rung the model actually loaded with — set by
# _load_with_fallback so callers can size their work to the context window.
_loaded_config: dict | None = None
# The tier the resident model was loaded for. Lets a caller that names a
# different tier (per-stage routing in the briefing's two-quills mode) swap
# models in-process: unload + load costs ~15-30s from the page cache, far
# cheaper than a second resident model that a 16 GB Mac cannot hold anyway.
_loaded_tier: str | None = None


# Public surface. Names not listed here (``_``-prefixed helpers) are internal,
# though the test suite reaches a few directly via ``llm_local._name``.
__all__ = [
    "MODEL_CATALOG",
    "DEFAULT_TIER",
    "set_settings_conn_provider",
    "selected_tier_for",
    "engine_roles",
    "role_default_tier",
    "model_file_path",
    "loaded_config",
    "get_llm",
    "chat_completion",
    "is_loaded",
    "unload",
    "download_model",
]

_MODEL_FILES = {
    # Ministral-3-14B-Instruct (Mistral, Dec 2025) — strong French, real
    # cross-source synthesis, no thinking mode (Instruct variant). It won the
    # local bench-off (Q4_K_M ~7.7 GB; fits a 16 GB Mac). Qwen2.5-14B and
    # Qwen3-14B were dropped: Qwen2.5 only listed the agenda, Qwen3 reasoned
    # well but ballooned and truncated.
    "ministral3-14b": "Ministral-3-14B-Instruct-2512-Q4_K_M.gguf",  # ≥ 16 GB tier
    # Gemma-4-12B-Instruct (Google, Jun 2026) — challenger to Ministral 3 in
    # the local bench-off: reported above Gemma 3 27B, native system-prompt
    # support. Licensed under Google's Gemma Terms of Use (a custom, non-OSI
    # license with use restrictions — NOT Apache 2.0); the user accepts those
    # terms when they choose to download this tier. Needs llama.cpp with the
    # gemma4 arch (llama-cpp-python ≥ 0.3.28).
    "gemma4-12b": "gemma-4-12b-it-Q4_K_M.gguf",  # ≥ 16 GB tier
    # Locally-fused QLoRA distillation of the tier above onto cloud-briefing
    # style — produced on-device, never downloadable (see MODEL_CATALOG).
    "ministral3-14b-estormi": "Ministral-3-14B-Estormi-SFT-Q4_K_M.gguf",
}

# HuggingFace repos to pull each tier's GGUF from.
_MODEL_REPOS = {
    "ministral3-14b": "mistralai/Ministral-3-14B-Instruct-2512-GGUF",  # official Mistral GGUF
    # Unsloth mirror: Google's own GGUF repo is gated behind a HF login, which
    # the tokenless download path can't pass.
    "gemma4-12b": "unsloth/gemma-4-12b-it-GGUF",
}

# Display + download metadata for every tier — the single source the model API
# serves to the Maintenance UI so it can list each model, show install state,
# and offer downloads. ``expected_bytes`` is the Q4_K_M GGUF size on the tier's
# ``_MODEL_REPOS`` mirror (drives the download progress estimate). Insertion
# order is the order the UI renders them in. Keys MUST stay in sync with
# ``_MODEL_FILES`` / ``_MODEL_REPOS``.
#
# ``revision`` pins each downloadable tier to an IMMUTABLE HuggingFace commit
# (not the mutable ``main`` ref) and ``sha256`` is the file's content digest,
# verified after download (see ``_stream_gguf``) — the same integrity discipline
# the bundled python-standalone tarball gets. Re-pin both together on a model
# bump; fetch via the HF API:
#   curl -s https://huggingface.co/api/models/<repo>            # -> .sha (commit)
#   curl -s https://huggingface.co/api/models/<repo>/tree/<sha>?recursive=1  # -> .lfs.oid per file
MODEL_CATALOG: dict[str, dict] = {
    "ministral3-14b": {
        "label": "Ministral 3 14B",
        "family": "Mistral",
        "min_ram_gb": 16,
        "expected_bytes": 8_239_593_024,  # actual Q4_K_M GGUF size (~7.7 GiB)
        "revision": "74fac473c43357d7fb2671713608183cc72496d0",
        "sha256": "824e0f3373e69b84f2cae46fdcb9bd1ebc6ab3bfc7acc125d818b7b8178cc613",
    },
    "gemma4-12b": {
        "label": "Gemma 4 12B",
        "family": "Google",
        "min_ram_gb": 16,
        "expected_bytes": 7_121_860_000,  # actual Q4_K_M GGUF size (~6.6 GiB)
        "revision": "3249fa54d5efa384afc552cc6700ad091efd5c39",
        "sha256": "43fec98c5102b1c446b4ddd0a9439f1db3a2e1f2e0b8cd143ce1ea619a9403d6",
    },
    # Locally-distilled prose quill: the stock Ministral fused with a QLoRA
    # style adapter trained on cloud-composed briefings (see the "Style
    # distillation" section of docs/architecture/briefing-generation.md).
    # ``local_only``: the GGUF is produced ON this machine — there is no
    # download source, so the catalog hides it until the file exists and the
    # download endpoint refuses it.
    "ministral3-14b-estormi": {
        "label": "Ministral 3 14B · Estormi SFT",
        "family": "Mistral",
        "min_ram_gb": 16,
        "expected_bytes": 8_239_593_024,  # ~ the stock Q4_K_M footprint
        "local_only": True,
    },
}

# LLM load configs, heaviest first. Loading walks DOWN this ladder: the
# starting rung comes from the machine's RAM (see ``resource_guard``); any
# rung that fails to allocate falls
# through to the next. n_ctx never drops below ~10k — the briefing composition
# prompt can run to several thousand tokens and needs headroom for the reply.
# Lighter rungs first trim the KV cache (n_ctx), then move
# layers off the Metal GPU (n_gpu_layers) to cut sustained GPU load.
_LLM_LADDER = [
    {"n_ctx": 16384, "n_gpu_layers": -1},
    {"n_ctx": 13312, "n_gpu_layers": -1},
    {"n_ctx": 11264, "n_gpu_layers": -1},
    {"n_ctx": 11264, "n_gpu_layers": 30},
    {"n_ctx": 10240, "n_gpu_layers": 18},
]


DEFAULT_TIER = "ministral3-14b"

# Local-LLM model selection. Only one engine still drives the in-process
# llama-cpp model: the Briefing engine's ``local`` provider (the alternative
# provider — Claude CLI — shells out instead). The role abstraction is
# kept (a single ``briefing`` role) so the Maintenance model-picker and
# ``api/model.py`` keep one stable shape. Callers without a role (the sidecar
# itself, tests) fall through to the briefing model.
_ROLE_SETTING_KEY = {
    "briefing": "briefing_model_tier",
}
_ROLE_DEFAULT_TIER = {
    # Ministral 3 14B stays the default: it won the original bench-off on a
    # 16 GB Mac. Gemma 4 12B is offered as a challenger tier; it only becomes
    # the default if it wins a briefing bench-off.
    "briefing": "ministral3-14b",
}


def _engine_role() -> str:
    """Engine role for this process — always ``briefing``.

    Read from ``ESTORMI_ENGINE_ROLE``; no launcher currently sets it, so this
    resolves to ``briefing`` in practice. The hook is kept so a second engine
    role can be introduced without reshaping the model API. An unset /
    unrecognised value (the sidecar itself, tests) resolves to ``briefing``,
    the sole local-LLM role.
    """
    role = os.getenv("ESTORMI_ENGINE_ROLE", "").strip().lower()
    return role if role in _ROLE_SETTING_KEY else "briefing"


# Optional injection point for the live server's shared aiosqlite connection.
# ``estormi_server/storage/tools.py`` registers ``tools.sqlite_conn`` here at import time so
# tier-setting reads reuse the live connection (and, in the test suite, the
# in-memory DB wired into ``tools._db``). When unset — e.g. the briefing worker
# subprocess, which never imports ``tools`` — ``_read_tier_setting`` falls back
# to a one-shot read-only connection on the settings DB file. This inversion
# keeps ``memory_core`` (the pure bottom layer) from reaching up into the
# server/app layer; see the contract in tests/contract/test_import_linter_layers.py.
_settings_conn_provider = None


def set_settings_conn_provider(provider) -> None:
    """Register a ``() -> aiosqlite.Connection`` factory for tier-setting reads.

    Called by the server layer (``tools.py``) so reads reuse the shared live
    connection instead of opening a fresh one. Optional: when unset, reads fall
    back to a read-only connection on ``memory_core.settings.DB_PATH``.
    """
    global _settings_conn_provider
    _settings_conn_provider = provider


async def _read_tier_setting(key: str) -> str | None:
    """Read a ``*_model_tier`` settings value, or ``None`` if unset/invalid.

    Works from both the sidecar (where the server has registered its shared
    connection via :func:`set_settings_conn_provider`) AND the briefing worker
    subprocess where it hasn't — there we fall back to a one-shot RO connection
    on the DB file. Without that, a user who picked a model in Maintenance still
    got the default loaded by the worker engine.
    """
    try:
        if _settings_conn_provider is None:
            raise RuntimeError("no shared settings connection registered")
        db = _settings_conn_provider()
        cursor = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cursor.fetchone()
        await cursor.close()
        if row and row[0] and row[0] in _MODEL_FILES:
            return row[0]
    except Exception:
        try:
            import aiosqlite  # noqa: PLC0415

            from memory_core.dag_state import db_path  # noqa: PLC0415

            async with aiosqlite.connect(f"file:{db_path()}?mode=ro", uri=True) as conn:
                async with conn.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
                    row = await cur.fetchone()
                    if row and row[0] and row[0] in _MODEL_FILES:
                        return row[0]
        except Exception:
            pass  # best-effort: fall back to default below
    return None


async def selected_tier_for(role: str) -> str:
    """Tier id chosen for ``role`` in Maintenance (or its default).

    Role-explicit variant of :func:`_selected_tier` for callers that know the
    engine they're reporting on (the model API serves the engine's selection
    from the sidecar, where ``ESTORMI_ENGINE_ROLE`` isn't set).
    """
    if role not in _ROLE_SETTING_KEY:
        role = "briefing"
    chosen = await _read_tier_setting(_ROLE_SETTING_KEY[role])
    return chosen or _ROLE_DEFAULT_TIER[role]


async def _selected_tier() -> str:
    """Tier id chosen for *this engine* in Maintenance (or its default).

    The settings key and default both depend on the process's engine role —
    see :data:`_ROLE_SETTING_KEY` / :data:`_ROLE_DEFAULT_TIER`.
    """
    return await selected_tier_for(_engine_role())


def engine_roles() -> tuple[str, ...]:
    """The engine roles that carry their own model selection."""
    return tuple(_ROLE_SETTING_KEY)


def role_default_tier(role: str) -> str:
    """Default tier for ``role`` when its settings key is unset."""
    return _ROLE_DEFAULT_TIER[role]


def model_file_path(tier: str) -> str:
    """Canonical on-disk GGUF path for ``tier``.

    Used by the catalog endpoint to report per-tier install state.
    """
    from memory_core import settings  # noqa: PLC0415

    data_dir = settings.resolve_data_dir()
    filename = _MODEL_FILES.get(tier, _MODEL_FILES[DEFAULT_TIER])
    return os.path.join(data_dir, "models", filename)


async def _model_path(tier: str | None = None) -> str:
    """Return the on-disk GGUF path for the selected tier.

    ``tier`` argument lets the download endpoint resolve a path without going
    through the settings round-trip. When omitted, ``_selected_tier()`` reads
    ``settings.briefing_model_tier`` so model loading honours whatever the
    user picked in Maintenance.
    """
    from memory_core import settings  # noqa: PLC0415

    data_dir = settings.resolve_data_dir()
    chosen = tier or await _selected_tier()
    filename = _MODEL_FILES.get(chosen, _MODEL_FILES[DEFAULT_TIER])
    return os.path.join(data_dir, "models", filename)


def _start_rung() -> int:
    """Ladder index to begin loading at — from the machine's RAM tier."""
    from memory_core import resource_guard  # noqa: PLC0415

    ram = resource_guard.total_ram_gb()
    base = 0 if ram >= 32 else (1 if ram >= 16 else 3)
    return min(base, len(_LLM_LADDER) - 1)


# Per-tier load options. Ministral uses the standard transformer cache and
# runs without ``flash_attn`` (kept per-tier so a future model that benefits
# from it can opt in). Chat format must match the model's tokenizer template —
# feeding a Mistral model with the wrong turn markers was the cause of the
# all-"unrelated" judgment regression seen on an earlier model audit.
# ``n_batch`` raises the prompt-processing batch from llama.cpp's 512 default:
# briefing prompts run to several thousand tokens and prefill dominates the
# per-call latency on Apple Silicon, so bigger batches are a straight win.
_TIER_LOAD_OPTS: dict[str, dict] = {
    # Ministral 3: use the GGUF's OWN embedded chat template (chat_format=None)
    # — newer Mistral template; forcing the legacy "mistral-instruct" markers
    # reproduced the all-"unrelated" tokenizer-mismatch regression on Nemo.
    "ministral3-14b": {"chat_format": None, "flash_attn": False, "n_batch": 1024},
    # Gemma 4: embedded template (the gemma4 turn markers are NOT a
    # llama-cpp-python built-in chat_format). flash_attn is REQUIRED for usable
    # decode speed — 7.4 tok/s vs 2.3 without it on an M4 (Metal), measured
    # warm at n_ctx=13312.
    "gemma4-12b": {"chat_format": None, "flash_attn": True, "n_batch": 1024},
    # The fused SFT model IS a Ministral 3 — same template, same options.
    "ministral3-14b-estormi": {"chat_format": None, "flash_attn": False, "n_batch": 1024},
}

# Tiers whose thinking mode must be disabled for the briefing — a reasoning
# model would wrap the answer in <think>…</think> and balloon latency over the
# ~15 LLM calls a briefing makes. Empty today (Ministral 3 is the Instruct,
# non-reasoning variant); kept wired for any future reasoning tier. The
# ``/no_think`` soft switch in the final user turn turns it off; any residual
# block is stripped from the reply.
_NO_THINK_TIERS: set[str] = set()
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _load_with_fallback(llama_cls, model_path: str, tier: str = DEFAULT_TIER):
    """Load the model, walking down the config ladder on allocation failure.

    Begins at the rung chosen from this machine's total RAM (see
    ``_start_rung``) and steps to a lighter rung (smaller context, then fewer
    GPU layers) every time a load raises — so the model always negotiates a
    config that fits.

    ``tier`` selects the chat-format + ``flash_attn`` defaults appropriate for
    the active model. Defaults to ``DEFAULT_TIER`` so older code paths (tests,
    scripts that don't pass a tier) keep a sane fallback.
    """
    global _loaded_config
    from memory_core import resource_guard  # noqa: PLC0415

    opts = _TIER_LOAD_OPTS.get(tier, _TIER_LOAD_OPTS[DEFAULT_TIER])
    ram, start = resource_guard.total_ram_gb(), _start_rung()
    top = len(_LLM_LADDER) - 1
    last_err: Exception | None = None
    for idx in range(start, len(_LLM_LADDER)):
        cfg = _LLM_LADDER[idx]
        try:
            llm = llama_cls(
                model_path=model_path,
                n_ctx=cfg["n_ctx"],
                n_gpu_layers=cfg["n_gpu_layers"],
                flash_attn=opts["flash_attn"],
                chat_format=opts["chat_format"],
                n_batch=opts.get("n_batch", 512),
                verbose=False,
            )
            model_name = Path(model_path).name
            msg = (
                f"loaded {tier} ({model_name}) — rung {idx}/{top}, n_ctx={cfg['n_ctx']}, "
                f"n_gpu_layers={cfg['n_gpu_layers']} (ram≈{ram:.0f}GB)"
            )
            _log.info(
                "llm.loaded",
                model=model_name,
                tier=tier,
                rung=idx,
                top=top,
                n_ctx=cfg["n_ctx"],
                n_gpu_layers=cfg["n_gpu_layers"],
                ram_gb=ram,
            )
            resource_guard.governor_log(f"LLM {msg}")
            _loaded_config = dict(cfg)
            return llm
        except Exception as e:  # noqa: BLE001 — any alloc/runtime failure → lighter rung
            last_err = e
            note = f"load failed at rung {idx} ({cfg}): {e} — trying a lighter config"
            _log.warning("llm.load_failed", rung=idx, cfg=cfg, error=str(e))
            resource_guard.governor_log(f"LLM {note}")
    raise RuntimeError(f"LLM failed to load at every config rung: {last_err}")


def loaded_config() -> dict | None:
    """The {n_ctx, n_gpu_layers} the model loaded with — ``None`` when unloaded."""
    return _loaded_config


async def get_llm(tier: str | None = None):
    """The resident model, loading (or swapping to) ``tier`` when needed.

    ``tier`` must be a catalog id; anything else (None, a cloud model id a
    caller passed through) resolves to the user's selected tier. When the
    resident model was loaded for a DIFFERENT tier, it is unloaded and the
    requested one loaded in its place — two 14B-class GGUFs cannot co-reside
    on a 16 GB Mac, so per-stage routing swaps instead.
    """
    global _llm, _loaded_tier
    target = tier if tier in _MODEL_FILES else await _selected_tier()
    if _llm is None or _loaded_tier != target:
        async with _llm_lock:
            if _llm is None or _loaded_tier != target:
                # Optional native dep (Apple-Silicon build); may be absent in the
                # CI typecheck env, so don't fail import resolution there.
                from llama_cpp import Llama  # pyright: ignore[reportMissingImports]

                if _llm is not None:
                    _log.info("llm.swap", from_tier=_loaded_tier, to_tier=target)
                    _llm = None  # release before loading — both cannot fit
                model_path = await _model_path(target)
                _llm = await asyncio.to_thread(_load_with_fallback, Llama, model_path, target)
                _loaded_tier = target
    return _llm


def _is_decode_error(exc: BaseException) -> bool:
    """``True`` if ``exc`` is a llama.cpp decode failure (e.g. ``llama_decode returned -3``)."""
    return "llama_decode returned" in str(exc)


# Context-fit guard. Chat-template markup the tokenizer estimate below doesn't
# see, plus a little slack — reserved out of n_ctx before sizing the reply.
_CTX_OVERHEAD_TOKENS = 96
# Never shrink a reply budget below this: a floor under which the long
# composition passes (day-vision, narration) cannot end cleanly.
_MIN_REPLY_TOKENS = 512
_TRIM_MARKER = "\n[... context trimmed to fit the local model's window ...]\n"


def _fit_to_context(llm_obj, messages: list[dict], max_tokens: int) -> tuple[list[dict], int]:
    """Make ``messages`` + reply fit ``llm_obj``'s context window.

    llama.cpp hard-errors when prompt + reply exceed ``n_ctx`` — which, for the
    briefing, silently costs the whole section the call was composing. Prefer
    degrading gracefully: first shrink the reply budget (down to
    ``_MIN_REPLY_TOKENS``), then cut the MIDDLE out of the longest message —
    briefing prompts carry instructions at the top and bottom and bulk data in
    between, so the middle is the least costly cut. Token counts estimate from
    the raw contents (template markup is covered by ``_CTX_OVERHEAD_TOKENS``).
    """
    n_ctx = llm_obj.n_ctx()

    def _count(msgs: list[dict]) -> int:
        text = "\n".join(str(m.get("content") or "") for m in msgs)
        return len(llm_obj.tokenize(text.encode("utf-8"), add_bos=False, special=False))

    prompt_tokens = _count(messages)
    if prompt_tokens + max_tokens + _CTX_OVERHEAD_TOKENS <= n_ctx:
        return messages, max_tokens

    # The reply floor never exceeds what the caller asked for — reserving 512
    # tokens for a 140-token request would over-trim the prompt for nothing.
    floor = min(_MIN_REPLY_TOKENS, max_tokens)
    budget = max(floor, n_ctx - prompt_tokens - _CTX_OVERHEAD_TOKENS)
    if prompt_tokens + budget + _CTX_OVERHEAD_TOKENS <= n_ctx:
        _log.warning(
            "llm.reply_budget_shrunk",
            prompt_tokens=prompt_tokens,
            requested=max_tokens,
            granted=budget,
            n_ctx=n_ctx,
        )
        return messages, budget

    # Prompt alone is too big — cut the middle of the longest content until it
    # fits (each pass recounts, so two passes almost always suffice).
    msgs = [dict(m) for m in messages]
    target = n_ctx - floor - _CTX_OVERHEAD_TOKENS
    for _ in range(4):
        prompt_tokens = _count(msgs)
        if prompt_tokens <= target:
            break
        idx = max(range(len(msgs)), key=lambda i: len(str(msgs[i].get("content") or "")))
        content = str(msgs[idx].get("content") or "")
        # Chars to drop, scaled by the token overshoot plus 10% margin.
        drop = int(len(content) * min(0.9, (prompt_tokens - target) / prompt_tokens + 0.1))
        keep_head = int((len(content) - drop) * 0.6)
        keep_tail = len(content) - drop - keep_head
        msgs[idx]["content"] = content[:keep_head] + _TRIM_MARKER + content[-keep_tail:]
    _log.warning(
        "llm.prompt_trimmed",
        prompt_tokens=_count(msgs),
        n_ctx=n_ctx,
        reply_budget=floor,
    )
    return msgs, floor


async def chat_completion(
    messages: list[dict],
    max_tokens: int = 1024,
    temperature: float = 0.0,
    timeout: float = 300.0,
    response_format: dict | None = None,
    gbnf_grammar: str | None = None,
    tier: str | None = None,
) -> str:
    """One serialized chat completion on the shared local model.

    ``response_format`` is llama.cpp's structured-output knob — pass
    ``{"type": "json_object", "schema": {...}}`` to constrain decoding to a
    JSON schema (grammar-enforced), which a 14B model needs where a cloud
    model can be trusted to emit the shape from instructions alone.
    ``gbnf_grammar`` constrains free-text output to a raw GBNF grammar
    instead (e.g. the day-vision's labelled-sections shape); mutually
    exclusive with ``response_format``, which would override it.
    ``tier`` pins the call to a specific catalog model (per-stage routing);
    omitted or unknown, the user's selected tier applies.
    """
    global _llm, _loaded_config

    # Disable thinking for tiers that reason by default (Qwen3): append the
    # ``/no_think`` soft switch to the final user turn so the model answers
    # directly. The residual <think> block (if any) is stripped from the reply.
    # While ``_NO_THINK_TIERS`` is empty (no reasoning tier ships) this stays
    # False without the per-call settings round-trip ``_selected_tier`` costs.
    no_think = bool(_NO_THINK_TIERS) and await _selected_tier() in _NO_THINK_TIERS
    if no_think and messages and messages[-1].get("role") == "user":
        last = messages[-1]
        messages = [*messages[:-1], {**last, "content": f"{last.get('content', '')}\n/no_think"}]

    # NOTE: asyncio.wait_for cancels the *awaiting* coroutine on timeout, but
    # the underlying llama.cpp inference call runs in a worker thread that
    # cannot be interrupted — it keeps running to completion in the
    # background, holding GPU/CPU until done. The timeout therefore bounds how
    # long the caller waits, not how long inference actually runs. This is an
    # accepted limitation: llama.cpp exposes no cancellation hook.
    def _call(llm_obj):
        grammar = None
        if gbnf_grammar is not None:
            from llama_cpp import (
                LlamaGrammar,  # pyright: ignore[reportMissingImports]  # noqa: PLC0415
            )

            grammar = LlamaGrammar.from_string(gbnf_grammar, verbose=False)
        return llm_obj.create_chat_completion(
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            response_format=response_format,
            grammar=grammar,
        )

    # Hold the inference lock across the whole decode (and its reset-retry) so
    # only one decode touches the shared model at a time — see _infer_lock.
    #
    # The model reference is resolved INSIDE the lock, never before it. A prior
    # caller that timed out leaves its decode running in an un-cancellable
    # worker thread and nulls ``_llm`` while still holding this lock; resolving
    # ``llm`` here (after that caller releases the lock) means a queued caller
    # picks up a FRESH model via ``get_llm`` rather than the orphaned context
    # the dead decode is still mutating — which would otherwise race it and
    # corrupt the shared KV cache. Fetching before the lock reintroduced exactly
    # that race.
    async with _infer_lock:
        llm = await get_llm(tier)
        messages, max_tokens = _fit_to_context(llm, messages, max_tokens)
        try:
            result = await asyncio.wait_for(asyncio.to_thread(_call, llm), timeout=timeout)
        except Exception as exc:
            if isinstance(exc, asyncio.TimeoutError):
                # The decode keeps running in its worker thread (cannot be
                # cancelled, see NOTE above), so reusing this context would race
                # the orphaned in-flight decode and crash. Drop our reference so
                # the next caller loads a FRESH model; the orphan thread keeps the
                # old model alive via its closure until it finishes (acceptable).
                async with _llm_lock:
                    _llm = None
                    _loaded_config = None
                    _set_loaded_tier(None)
                raise
            if not _is_decode_error(exc):
                raise
            # llama.cpp's KV cache is left in an inconsistent state after a -3
            # failure, so every subsequent call cascades the same error. Reset the
            # session and retry once; if that also fails, unload so the next caller
            # loads a fresh model (possibly at a lighter rung if pressure persists).
            _log.warning("llm.decode_failed_retrying", error=str(exc))
            try:
                await asyncio.to_thread(llm.reset)
                result = await asyncio.wait_for(asyncio.to_thread(_call, llm), timeout=timeout)
            except Exception:
                async with _llm_lock:
                    _llm = None
                    _loaded_config = None
                    _set_loaded_tier(None)
                raise
    choice = result["choices"][0]
    if choice.get("finish_reason") == "length":
        # The reply hit max_tokens and is cut mid-thought — the silent killer
        # of long composition passes (day-vision, narration). Surface it loudly
        # so an under-budgeted caller is visible in the run log.
        _log.warning("llm.truncated", max_tokens=max_tokens)
    content = choice["message"]["content"]
    # Strip any residual reasoning block (thinking-mode tiers, see _NO_THINK_TIERS).
    return _THINK_BLOCK_RE.sub("", content).strip() if no_think else content


async def is_loaded() -> bool:
    return _llm is not None


def _set_loaded_tier(value: str | None) -> None:
    """Assign ``_loaded_tier`` from code paths that hold ``_llm_lock``.

    A tiny setter (not a bare assignment) so the inner functions don't each
    need their own ``global`` declaration for a second variable.
    """
    global _loaded_tier
    _loaded_tier = value


async def unload() -> None:
    global _llm, _loaded_config
    async with _llm_lock:
        _llm = None
        _loaded_config = None
        _set_loaded_tier(None)


# Stream-download tuning. A read that delivers no bytes for this many seconds
# is treated as a stalled CDN connection — the failure mode that hung
# ``hf_hub_download`` indefinitely (socket open, byte count frozen): the read
# raises and the retry loop resumes from the bytes already on disk via a HTTP
# Range request. ``_DL_MAX_STALLS`` bounds the resume attempts.
_DL_READ_TIMEOUT = 30.0
_DL_MAX_STALLS = 500
_GGUF_MAGIC = b"GGUF"


def _stream_gguf(url: str, part: Path, final: Path, expected_sha256: str | None = None) -> None:
    """Resumable, stall-proof GGUF download: ``url`` → ``part`` → atomic ``final``.

    Replaces ``hf_hub_download`` for the weight files. That call had no
    mid-stream read timeout, so a stalled CDN socket hung forever at a fixed
    byte count (observed on a Qwen pull). Here a read that stalls past
    ``_DL_READ_TIMEOUT`` raises, and the loop resumes from the bytes already on
    disk with a Range request — the same tactic as ``curl -C - --speed-time``.
    Runs in a worker thread (sync httpx); progress is observable by watching
    ``part`` grow, which is what the model-download SSE endpoint reports.
    """
    import time  # noqa: PLC0415

    import httpx  # noqa: PLC0415

    timeout = httpx.Timeout(_DL_READ_TIMEOUT, connect=20.0)
    # Learn the total size up front so completion is unambiguous (HF's CDN
    # serves Content-Length on HEAD). 0 = unknown; we then trust a clean EOF.
    total = 0
    try:
        head = httpx.head(url, follow_redirects=True, timeout=20.0)
        total = int(head.headers.get("content-length") or 0)
    except Exception:  # noqa: BLE001 — HEAD is best-effort; fall back to EOF
        total = 0

    stalls = 0
    while True:
        have = part.stat().st_size if part.exists() else 0
        if total and have >= total:
            break
        before, completed = have, False
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            with httpx.stream(
                "GET", url, headers=headers, follow_redirects=True, timeout=timeout
            ) as r:
                if r.status_code == 416:  # range past EOF → already complete
                    break
                mode = "ab"
                if r.status_code == 200:  # server ignored Range — restart clean
                    mode, have = "wb", 0
                    total = total or int(r.headers.get("content-length") or 0)
                elif r.status_code != 206:
                    r.raise_for_status()
                with open(part, mode) as f:
                    for chunk in r.iter_bytes(1024 * 1024):
                        f.write(chunk)
                completed = True
        except (httpx.TimeoutException, httpx.TransportError):
            completed = False
        after = part.stat().st_size if part.exists() else 0
        if completed and (not total or after >= total):
            break
        if after <= before:  # no forward progress this pass — count toward stall budget
            stalls += 1
            if stalls > _DL_MAX_STALLS:
                raise RuntimeError(f"download stalled at {after}/{total or '?'} bytes")
            time.sleep(2)
            continue
        stalls = 0

    size = part.stat().st_size if part.exists() else 0
    if size <= 0 or (total and size < total):
        raise RuntimeError(f"download incomplete: {size}/{total} bytes")
    with open(part, "rb") as f:
        if f.read(4) != _GGUF_MAGIC:
            raise RuntimeError("downloaded file is not a valid GGUF model")
    # Content-pin: verify the full SHA256 against the digest committed in
    # MODEL_CATALOG. The magic check only proves "a GGUF"; this proves it's the
    # EXACT weights we pinned, so a force-pushed branch or a compromised mirror
    # can't slip a different (maliciously-tuned) model past. A mismatch deletes
    # the .part so a retry re-downloads clean instead of resuming the bad bytes.
    if expected_sha256:
        import hashlib  # noqa: PLC0415

        h = hashlib.sha256()
        with open(part, "rb") as f:
            for blk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(blk)
        actual = h.hexdigest()
        if actual != expected_sha256:
            part.unlink(missing_ok=True)
            raise RuntimeError(f"model digest mismatch: expected {expected_sha256}, got {actual}")
    os.replace(part, final)


async def download_model(tier: str | None = None) -> str:
    """Download the model GGUF for ``tier`` (default: the user's current tier).

    The tier id maps to a filename + HuggingFace repo via the module-level
    ``_MODEL_FILES`` / ``_MODEL_REPOS`` catalog, so a new tier only needs to be
    added in those two dicts (kept in sync with ``MODEL_CATALOG``).

    Streams via :func:`_stream_gguf` rather than ``hf_hub_download`` so a
    stalled CDN connection can't hang the download forever and progress is
    observable on the ``.part`` file.
    """
    chosen = tier or await _selected_tier()
    path = Path(await _model_path(chosen))
    if path.exists():
        return str(path)
    if MODEL_CATALOG.get(chosen, {}).get("local_only"):
        # Produced on this machine (fused QLoRA distillation) — there is no
        # download source, and falling through to the DEFAULT repo would
        # silently install the WRONG weights under this tier's filename.
        raise RuntimeError(f"tier {chosen!r} is local-only — no download source")
    path.parent.mkdir(parents=True, exist_ok=True)
    repo_id = _MODEL_REPOS.get(chosen, _MODEL_REPOS[DEFAULT_TIER])
    meta = MODEL_CATALOG.get(chosen, {})
    # Pin to the immutable commit + verify the content digest (see MODEL_CATALOG).
    # Unknown/unpinned tiers fall back to ``main`` with no digest check — the
    # pre-pin behaviour — rather than failing the download.
    revision = meta.get("revision") or "main"
    expected_sha256 = meta.get("sha256")
    url = f"https://huggingface.co/{repo_id}/resolve/{revision}/{path.name}"
    part = path.with_name(path.name + ".part")
    await asyncio.to_thread(_stream_gguf, url, part, path, expected_sha256)
    return str(path)

"""LLM dispatch for the briefing engine — provider switching + claude-CLI retry.

Two providers compose a briefing (``knowledge_llm_provider``):

* ``local`` — the in-process llama-cpp model (``memory_core.llm_local``).
* ``claude-cli`` — shells out to the Claude Code CLI, piping the prompt over
  stdin so it never appears in ``argv`` and a bounded retry rides out a
  transient API overload.

``runtime`` wraps :func:`_llm_call_dispatch` in ``_llm_call`` to fold the
prompt/output char counts into the per-run metric recorder; this module owns
only the dispatch + CLI plumbing.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import structlog

log = structlog.get_logger()

_CLAUDE_SEARCH_PATHS = [
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
    str(Path.home() / ".local/bin/claude"),
]

# claude CLI subprocess: per-attempt timeout (s), attempt count, and the linear
# backoff base (s) between attempts. The product TIMEOUT × ATTEMPTS bounds the
# worst-case wall time a single prompt can spend; the default 300×2 keeps the
# historical 600s ceiling while giving a hung call one retry. All overridable
# via env so a slow box can widen the budget without a code change.
_CLI_TIMEOUT = int(os.getenv("BRIEFING_LLM_TIMEOUT", "300"))
_CLI_ATTEMPTS = max(1, int(os.getenv("BRIEFING_LLM_ATTEMPTS", "2")))
_CLI_RETRY_BACKOFF = float(os.getenv("BRIEFING_LLM_RETRY_BACKOFF", "5"))

# Cheap default model for the briefing-swarm side passes (structured extractor
# pre-pass and the post-generation critic). Overridable via the
# ``briefing_extractor_model`` / ``briefing_critic_model`` settings.
_HAIKU_MODEL = "claude-haiku-4-5-20251001"


def _claude_bin() -> str:
    """Resolve the claude CLI binary, checking known locations before PATH."""
    for p in _CLAUDE_SEARCH_PATHS:
        if Path(p).exists():
            return p

    claude_dir = Path.home() / "Library/Application Support/Claude"
    for subdir in ("claude-code-vm", "claude-code"):
        parent = claude_dir / subdir
        if parent.is_dir():
            for candidate in sorted(parent.iterdir(), reverse=True):
                binary = candidate / "claude"
                if binary.exists():
                    return str(binary)

    found = shutil.which("claude")
    if found:
        return found
    raise FileNotFoundError("claude CLI not found. Install Claude Code from https://claude.ai/code")


async def _llm_call_dispatch(
    prompt: str,
    provider: str,
    model: str,
    *,
    max_tokens: int | None = None,
    temperature: float | None = None,
    json_schema: dict | None = None,
    gbnf_grammar: str | None = None,
    timeout: float | None = None,
    stage: str = "",
) -> str:
    """Dispatch one prompt to the configured provider.

    The keyword options shape the LOCAL decode only — a 14B GGUF needs its
    reply budget, sampling and (for structured passes) a grammar set per
    task, where the Claude CLI needs none of it: cloud models size their own
    replies and emit the asked-for shape reliably, and the CLI exposes no
    such knobs anyway. ``json_schema`` constrains JSON passes;
    ``gbnf_grammar`` constrains free-text shapes (they are mutually
    exclusive — a schema would override the raw grammar).

    ``stage`` names the editorial pass making the call ("writer", "lede",
    "news_synthesis", …). For the local provider it keys the per-tier decode
    profile AND the per-stage tier routing (two-quills mode) — see
    ``decode_profiles``. Inert for claude-cli.
    """
    if provider == "local":
        from estormi_briefing.llm.decode_profiles import apply_profile, stage_tier
        from memory_core.llm_local import chat_completion

        # In local mode ``model`` IS the selected catalog tier (the
        # orchestrator and the stage harness both resolve it before binding).
        tier = stage_tier(stage, model)
        prompt, max_tokens = apply_profile(tier, stage, prompt, max_tokens)
        return await chat_completion(
            [{"role": "user", "content": prompt}],
            max_tokens=max_tokens if max_tokens is not None else 1024,
            temperature=temperature if temperature is not None else 0.0,
            timeout=timeout if timeout is not None else 300.0,
            response_format=(
                {"type": "json_object", "schema": json_schema} if json_schema else None
            ),
            gbnf_grammar=gbnf_grammar,
            tier=tier,
        )

    if provider == "claude-cli":
        # Pipe the prompt via stdin (`-p -` reads from stdin) so it never
        # appears in argv — both for `ps` privacy and to avoid the ARG_MAX
        # cap on very large transcripts.
        binary = _claude_bin()
        cmd = [binary, "--model", model, "-p", "-"] if model else [binary, "-p", "-"]
        # A transient API overload can make the CLI hang for the full timeout
        # rather than failing fast; a single such blip used to drop a whole
        # source (or the day-vision) silently. Retry within a bounded budget so
        # a brief blip is ridden out, while ``_CLI_TIMEOUT × _CLI_ATTEMPTS``
        # still caps how long one prompt can freeze the engine.
        # Run on a worker thread: ``subprocess.run`` is blocking, and blocking
        # the event loop would both stall the hosting server and serialise the
        # concurrent per-source calls the orchestrator now fans out.
        last_exc: Exception | None = None
        for attempt in range(1, _CLI_ATTEMPTS + 1):
            try:
                r = await asyncio.to_thread(
                    subprocess.run,
                    cmd,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=_CLI_TIMEOUT,
                )
                return r.stdout.strip()
            except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
                last_exc = exc
                if attempt < _CLI_ATTEMPTS:
                    backoff = _CLI_RETRY_BACKOFF * attempt
                    log.warning(
                        "claude CLI attempt %d/%d failed (%s); retrying in %.0fs",
                        attempt,
                        _CLI_ATTEMPTS,
                        type(exc).__name__,
                        backoff,
                    )
                    await asyncio.sleep(backoff)
        # Attempts exhausted — re-raise so the caller logs and degrades the
        # section rather than the whole briefing.
        assert last_exc is not None  # loop runs ≥1 time, so this is always set
        raise last_exc

    raise ValueError(f"Unknown LLM provider: {provider!r}")

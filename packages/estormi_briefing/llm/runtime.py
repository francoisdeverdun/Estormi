"""Shared run-scoped state for the briefing engine.

The orchestrator (``run_briefing``) and the helper modules (``prompts``,
``synthesis``, ``day_vision``, ``delivery``, …) share a little common state:
the output language, the user's display names, the user-authored profile
(``user_context``), the per-run metric recorder, and the metric-aware LLM call
built on it. Keeping it here lets those modules stay decoupled from one another
while ``run()`` owns the lifecycle: it calls :func:`refresh` at the top of
every invocation (and rebinds ``_run_metrics``) so a Manage-modal flip of the
env vars or settings takes effect on the next run without a server restart.

Readers MUST access the values through the module (``runtime.language``,
``runtime._llm_call``) rather than importing the names, so they observe the
value the orchestrator rebinds each run instead of the import-time snapshot —
and so the test suite has a single patch target per symbol.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import aiosqlite

from estormi_briefing.llm.llm_dispatch import _llm_call_dispatch

if TYPE_CHECKING:
    from estormi_briefing.llm.metrics import _BriefingMetrics

# Output language for the composed briefing — interpolated into each prompt's
# "write in X" directive. The app ships French-only (no language selector), so
# this is French; ``refresh()`` (re)sets it from the ``briefing_language``
# setting, which defaults to French.
language: str = "French"

# Display names the user answers to. Used to relabel WhatsApp conversations so
# the user's own name never becomes a conversation label.
user_display_names: set[str] = set()

# The user's own free-text profile — who they are, the people in their life,
# their work, and what they expect from the briefing. Trusted background (the
# user authored it) injected into every synthesis prompt so the briefing can
# resolve who a name is, judge what matters, and meet the user's expectations.
# ``refresh()`` sets it from the ``briefing_user_context`` setting.
user_context: str = ""

# The user's partner's display name, if known. Used by the post-generation
# critic to spot partner-owned calendar events that the briefing wrongly
# presented as the user's obligation. Empty when unknown; set from the
# ``ESTORMI_PARTNER_NAME`` env var (``.env``), which is loaded once at server
# startup — changing it requires a server restart. (There is no Manage-modal
# field for it; the live-editable free-text field is ``user_context`` below.)
partner_name: str = os.getenv("ESTORMI_PARTNER_NAME", "").strip()

# Per-run metric accumulator (defined in ``metrics``). Rebound by the
# orchestrator at the top of every ``run()`` and persisted in its ``finally``
# block. It lives at module scope rather than as a threaded argument because
# the LLM-call wrapper sits behind every prompt template and would otherwise
# need plumbing through every helper.
_run_metrics: _BriefingMetrics | None = None


async def _llm_call(
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
    """LLM dispatcher. Counts prompt + output chars into the briefing recorder
    so the pulse can chart approximate input/output tokens per run.

    The keyword options tune the local provider's decode per task (reply
    budget, sampling, JSON-schema or GBNF grammar, wall-clock budget); they
    are inert for claude-cli. ``stage`` names the calling pass — the local
    dispatch keys per-tier decode profiles and two-quills routing on it.
    """
    output = await _llm_call_dispatch(
        prompt,
        provider,
        model,
        max_tokens=max_tokens,
        temperature=temperature,
        json_schema=json_schema,
        gbnf_grammar=gbnf_grammar,
        timeout=timeout,
        stage=stage,
    )
    if _run_metrics is not None:
        _run_metrics.record_llm(prompt, output)
    return output


async def _get_setting(db: aiosqlite.Connection, key: str, default: str = "") -> str:
    cur = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = await cur.fetchone()
    await cur.close()
    return row[0] if row else default


async def _set_setting(db: aiosqlite.Connection, key: str, value: str) -> None:
    await db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    await db.commit()


# Maps the ``briefing_language`` code to the language name interpolated into each
# prompt's "write in {{ language }}" output directive. The app ships French-only
# (no UI selector; French is the default), so this resolves to French; the
# ``en`` entry is retained so an English edition can be re-enabled without
# re-plumbing. Unknown codes fall back to French.
LANGUAGES: dict[str, str] = {"en": "English", "fr": "French"}


def load_user_display_names() -> set[str]:
    """Read ESTORMI_USER_DISPLAY_NAMES from the env into a name set."""
    return {
        name.strip()
        for name in os.getenv("ESTORMI_USER_DISPLAY_NAMES", "").split(",")
        if name.strip()
    }


def refresh(language_code: str, profile: str = "") -> None:
    """Re-derive the run-scoped state from the env + the run's settings.

    Called once at the top of every ``run()`` so the per-run ``profile`` (the
    ``briefing_user_context`` setting, the live-editable Manage-modal field)
    takes effect without restarting the server; module-import-time reads would
    freeze it forever. The env-derived values (``partner_name``,
    ``user_display_names``) come from ``.env`` and are frozen after startup —
    re-reading ``os.getenv`` here is harmless but a change to them still needs a
    server restart.
    """
    global language, user_display_names, user_context, partner_name
    language = LANGUAGES.get((language_code or "").strip().lower(), "French")
    user_display_names = load_user_display_names()
    user_context = (profile or "").strip()
    partner_name = os.getenv("ESTORMI_PARTNER_NAME", "").strip()

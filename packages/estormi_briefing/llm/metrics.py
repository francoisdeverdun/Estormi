"""Per-run metric accumulator for the briefing engine.

One ``_BriefingMetrics`` instance is created at the top of every ``run()`` and
threaded behind every LLM call (via the module-level holder in
``runtime``) so the BriefingPulse can chart timing, approximate token
cost, section composition and item counts. Recorder failures must never break
a run — ``persist`` swallows its own errors.
"""

from __future__ import annotations

import json
import time as _time
from datetime import datetime, timezone

import aiosqlite
import structlog

log = structlog.get_logger()

# Token-equivalent approximation. The claude CLI exposes no usage counters; the
# recorder accumulates input/output character counts and divides by this to
# surface an order-of-magnitude token figure in the pulse. 4 chars/token is the
# long-standing OpenAI rule-of-thumb and is good enough for the trend chart.
_CHARS_PER_TOKEN = 4


class _BriefingMetrics:
    """Per-run metric accumulator.

    Captures everything the ``briefing_runs`` table holds: timing, model,
    approximate input/output tokens (from char counts; see
    ``_CHARS_PER_TOKEN``), section composition and item counts. ``persist``
    writes them in a single INSERT.
    """

    def __init__(self, model: str, provider: str) -> None:
        self.started_wall: float = _time.time()
        self.started_iso: str = datetime.now(timezone.utc).isoformat()
        self.model: str = model
        self.provider: str = provider
        self.tokens_in_chars: int = 0
        self.tokens_out_chars: int = 0
        self.sections: dict[str, int] = {}
        self.items_considered: int = 0
        self.items_included: int = 0

    def record_llm(self, prompt: str, output: str) -> None:
        self.tokens_in_chars += len(prompt or "")
        self.tokens_out_chars += len(output or "")

    def set_section(self, name: str, count: int) -> None:
        self.sections[name] = int(count)

    async def persist(self, db: aiosqlite.Connection, *, status: str, summary: str) -> None:
        finished_wall = _time.time()
        finished_iso = datetime.now(timezone.utc).isoformat()
        duration_ms = int(round((finished_wall - self.started_wall) * 1000))
        tokens_in = self.tokens_in_chars // _CHARS_PER_TOKEN
        tokens_out = self.tokens_out_chars // _CHARS_PER_TOKEN
        model_label = (
            f"{self.provider}/{self.model}"
            if self.provider and self.model
            else (self.model or self.provider)
        )
        try:
            await db.execute(
                """
                INSERT INTO briefing_runs (
                    started_at, finished_at, status, duration_ms, model,
                    tokens_in, tokens_out, sections_json,
                    items_considered, items_included,
                    summary
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.started_iso,
                    finished_iso,
                    status,
                    duration_ms,
                    model_label,
                    int(tokens_in),
                    int(tokens_out),
                    json.dumps(self.sections, ensure_ascii=False),
                    int(self.items_considered),
                    int(self.items_included),
                    (summary or "")[:1000],
                ),
            )
            await db.commit()
        except Exception as exc:  # noqa: BLE001
            # Recorder failures must never break a briefing run.
            log.warning("Failed to persist briefing_runs row: %s", exc)

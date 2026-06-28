"""Stage harness — run ONE briefing stage against live data, read-only.

The full local pipeline takes ~30-60 min; iterating on a single stage must
not. Each subcommand exercises exactly one stage with the smallest honest
input set, reads settings/data from the live DB + MCP server, and NEVER
writes to the vault, the settings, or the continuity state.

Usage (from the repo root, server running):
    .venv/bin/python -m estormi_briefing.stage_harness timeline
    .venv/bin/python -m estormi_briefing.stage_harness advice
    .venv/bin/python -m estormi_briefing.stage_harness readiness   # 1 LLM call
    .venv/bin/python -m estormi_briefing.stage_harness lede        # plan + N ledes
    .venv/bin/python -m estormi_briefing.stage_harness profile     # 1 LLM call, no write
    .venv/bin/python -m estormi_briefing.stage_harness vision      # full compose (~5-10 min)

Options: --date YYYY-MM-DD (back-fill), --bestof N, --model TIER.

A/B bench: ``--ab`` runs an LLM stage once per catalog tier (Ministral,
Gemma — the model swaps in-process) and prints the outputs side by side
with timing, so routing decisions rest on measurements, not intuition.
``--routing two-quills`` (or a JSON map) activates per-stage tier routing
for the run — the way to exercise the two-quills composition off-line.

Exemplar bank: ``harvest --from <vault-json-path> --label fable`` extracts
style exemplars (lede, readiness, impact lines, writer prose) from a
composed briefing — typically a cloud-generated ``.bak`` — into the
data-dir bank (``briefing_exemplars.json``) that the composition prompts
inject as style anchors. The bank carries personal data: it lives in the
data dir and must never enter the repo.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

import aiosqlite

from estormi_briefing.compose.build_daily_note import _t
from estormi_briefing.compose.composer import (
    _write_lede,
    _write_readiness,
    build_registry,
    plan_schema,
)
from estormi_briefing.compose.continuity import callbacks as continuity_callbacks
from estormi_briefing.compose.continuity import load_state as load_continuity_state
from estormi_briefing.compose.prompts import _assemble_vision_rows
from estormi_briefing.compose.timeline import free_slots, timeline_html
from estormi_briefing.compose.user_profile import merge_profile, propose_observations, split_profile
from estormi_briefing.day.day import LOCAL_TZ
from estormi_briefing.day.day_context import _fetch_daily_actions, _fetch_health_chunks
from estormi_briefing.day.day_load import choose_advice, day_features, parse_whoop
from estormi_briefing.day.day_vision import (
    _fetch_today_located_events,
    _fetch_workout_notes,
    _generate_day_vision,
)
from estormi_briefing.lint.vision_lint import lint_vision
from estormi_briefing.llm import runtime
from estormi_briefing.llm.bestof import TimeBudget
from estormi_briefing.llm.runtime import _get_setting
from estormi_briefing.run_briefing import DB_PATH
from memory_core.prompt_templates import render as render_prompt  # noqa: E402
from memory_core.sanitizer import sanitize_chunk
from memory_core.settings import resolve_data_dir


def _llm(provider: str, model: str):
    return lambda p, **kw: runtime._llm_call(p, provider, model, **kw)


async def _setup(args) -> tuple[str, str, str, str]:
    """Read run settings (language, profile, provider, model) from the live DB."""
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    lang = await _get_setting(db, "briefing_language", "fr")
    user_context = await _get_setting(db, "briefing_user_context", "")
    provider = await _get_setting(db, "knowledge_llm_provider", "local")
    model = args.model
    if not model:
        if provider == "local":
            from memory_core.llm_local import selected_tier_for  # noqa: PLC0415

            model = await selected_tier_for("briefing")
        else:
            model = await _get_setting(db, "knowledge_llm_model", "claude-sonnet-4-6")
    await db.close()
    runtime.refresh(lang, user_context)
    return lang.strip().lower(), user_context, provider, model


async def _stage_timeline(day, lang, *_):
    events, _loc = await _fetch_today_located_events(day)
    slots = free_slots(events, day)
    for e in events:
        print(
            f"  {e['start'].astimezone(LOCAL_TZ):%H:%M}–{e['end'].astimezone(LOCAL_TZ):%H:%M}  {e['title']}"
        )
    for s in slots:
        print(
            f"  [slot] {s['start'].astimezone(LOCAL_TZ):%H:%M}–{s['end'].astimezone(LOCAL_TZ):%H:%M} ({s['minutes']} min)"
        )
    labels = {"free_slot": _t(lang, "free_slot"), "all_day": _t(lang, "all_day")}
    print("\n" + timeline_html(events, slots, labels))


async def _stage_advice(day, lang, *_):
    events, _loc = await _fetch_today_located_events(day)
    health = await _fetch_health_chunks(day)
    notes = await _fetch_workout_notes()
    snapshot = parse_whoop([str(c.get("text") or "") for c in health])
    features = day_features(events, free_slots(events, day))
    advice = choose_advice(
        snapshot,
        features,
        [{"title": str(n.get("title") or ""), "text": str(n.get("text") or "")} for n in notes],
        [],
        "",
        "fr" if lang == "fr" else "en",
    )
    print("snapshot:", json.dumps(snapshot, ensure_ascii=False))
    print("features:", json.dumps(features, ensure_ascii=False))
    print("advice:  ", json.dumps(advice, ensure_ascii=False, indent=2))


async def _stage_readiness(day, lang, provider, model, args):
    health = await _fetch_health_chunks(day)
    events, _loc = await _fetch_today_located_events(day)
    notes = await _fetch_workout_notes()
    advice = choose_advice(
        parse_whoop([str(c.get("text") or "") for c in health]),
        day_features(events, free_slots(events, day)),
        [{"title": str(n.get("title") or ""), "text": str(n.get("text") or "")} for n in notes],
        [],
        "",
        "fr" if lang == "fr" else "en",
    )
    rows = {
        "health_rows": [
            {"when_label": c.get("when_label") or "", "text": str(c.get("text") or "")}
            for c in health
        ]
    }
    t0 = datetime.now()
    line = await _write_readiness(_llm(provider, model), rows, runtime.language, advice=advice)
    print(f"({(datetime.now() - t0).total_seconds():.0f}s)")
    print("advice facts:", json.dumps((advice or {}).get("facts", []), ensure_ascii=False))
    print(line or "(omitted)")


async def _stage_lede(day, lang, provider, model, args):
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    actions = await _fetch_daily_actions(db, day)
    await db.close()
    rows = _assemble_vision_rows(
        day.isoformat(),
        actions.get("calendar") or [],
        actions.get("reminders") or [],
        [],
        [],
        [],
        [],
        {},
        local_mode=True,
    )
    registry = build_registry(rows, day.isoformat())
    if not registry:
        print("empty registry — nothing on the day")
        return
    llm = _llm(provider, model)
    plan_raw = await llm(
        render_prompt(
            "briefing_plan",
            date_str=day.isoformat(),
            day_anchor="",
            user_context=runtime.user_context,
            registry=registry,
            chained=[],
            critic_feedback="",
            language=runtime.language,
        ),
        json_schema=plan_schema([e["id"] for e in registry]),
        max_tokens=1100,
        temperature=0.1,
        timeout=480.0,
    )
    plan = json.loads(plan_raw)
    threads = [t for t in plan.get("myday_threads") or [] if t.get("ids")]
    print("plan threads:", json.dumps(threads, ensure_ascii=False, indent=2))
    by_id = {e["id"]: e for e in registry}
    t0 = datetime.now()
    lede = await _write_lede(
        llm,
        threads,
        by_id,
        registry,
        day.isoformat(),
        "",
        language=runtime.language,
        n_candidates=args.bestof,
        budget=TimeBudget(0),
    )
    print(f"\nLEDE ({(datetime.now() - t0).total_seconds():.0f}s): {lede}")


async def _stage_profile(day, lang, provider, model, args):
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    current = await _get_setting(db, "briefing_user_context", "")
    await db.close()
    user_part, auto_part = split_profile(current)
    from estormi_briefing.io.mcp_io import _fetch_around_mcp  # noqa: PLC0415

    chunks = await _fetch_around_mcp(
        {"date": day.isoformat(), "window_days": 7, "corpus": "personal", "limit": 150},
        timeout=20.0,
    )
    signals, seen = [], set()
    for c in chunks:
        line = sanitize_chunk(
            " ".join(f"{c.get('title') or ''} {(c.get('text') or '')[:140]}".split())
        )
        key = line.lower()[:60]
        if not line or key in seen:
            continue
        seen.add(key)
        signals.append(f"[{c.get('source') or '?'}] {line[:180]}")
        if len(signals) >= 60:
            break
    print(f"{len(signals)} signal(s)")
    obs = await propose_observations(
        _llm(provider, model), user_part, auto_part, signals, language=runtime.language
    )
    print("observations:", json.dumps(obs, ensure_ascii=False, indent=2))
    print("\n— merged preview (NOT written) —\n")
    print(merge_profile(user_part, obs, "fr" if lang == "fr" else "en"))


async def _stage_vision(day, lang, provider, model, args):
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    actions = await _fetch_daily_actions(db, day)
    await db.close()
    t0 = datetime.now()
    vision, rows = await _generate_day_vision(
        day.isoformat(),
        actions,
        provider,
        model,
        use_composer=True,
        bestof_n=args.bestof,
        budget=TimeBudget(0),
    )
    print(
        f"\n===== VISION ({len(vision)} chars, {(datetime.now() - t0).total_seconds():.0f}s) =====\n"
    )
    print(vision)
    print("\n===== CONTINUITY (read-only) =====")
    state = load_continuity_state(Path(resolve_data_dir()))
    print("state:", json.dumps(state, ensure_ascii=False))
    print(
        "callbacks:",
        continuity_callbacks(state, day.isoformat(), actions.get("calendar") or [], lang),
    )
    print("\n===== LINT =====")
    issues = lint_vision(vision, language=runtime.language)
    if not issues:
        print("clean — no structural issues")
    for i in issues:
        print(f"- {i['type']}: {i['excerpt'][:160]}")


async def _cmd_harvest(args) -> None:
    """Harvest style exemplars from a composed briefing into the data-dir bank."""
    from estormi_briefing.compose.exemplars import add_exemplars, harvest_exemplars, load_bank

    payload = json.loads(Path(args.src).read_text())
    label = args.label or Path(args.src).name
    harvested = harvest_exemplars(payload.get("htmlBody") or "")
    for stage, texts in harvested.items():
        added = add_exemplars(stage, texts, label)
        print(f"  {stage:<10} {len(texts)} found, {added} new")
    bank = load_bank()
    total = sum(len(v) for v in bank.values())
    print(f"bank: {total} exemplar(s) across {len(bank)} stage(s) (data dir)")


_STAGES = {
    "timeline": _stage_timeline,
    "advice": _stage_advice,
    "readiness": _stage_readiness,
    "lede": _stage_lede,
    "profile": _stage_profile,
    "vision": _stage_vision,
}
# Stages that call an LLM — the only ones --ab makes sense for.
_AB_STAGES = ("readiness", "lede", "profile", "vision")


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stage", choices=sorted([*_STAGES, "harvest"]))
    ap.add_argument("--date", default="", help="YYYY-MM-DD (defaults to today)")
    ap.add_argument("--bestof", type=int, default=2)
    ap.add_argument("--model", default="", help="override the model tier")
    ap.add_argument(
        "--ab",
        action="store_true",
        help="run the stage once per catalog tier and print outputs side by side",
    )
    ap.add_argument(
        "--routing",
        default="",
        help='per-stage tier routing: "two-quills" or a JSON {stage: tier} map',
    )
    ap.add_argument("--from", dest="src", default="", help="harvest: path to a briefing JSON")
    ap.add_argument("--label", default="", help="harvest: provenance label (e.g. fable)")
    args = ap.parse_args()

    if args.stage == "harvest":
        if not args.src:
            ap.error("harvest requires --from <briefing json path>")
        await _cmd_harvest(args)
        return

    day = datetime.fromisoformat(args.date).date() if args.date else datetime.now(LOCAL_TZ).date()
    lang, _ctx, provider, model = await _setup(args)
    if args.routing:
        from estormi_briefing.llm.decode_profiles import set_stage_routing  # noqa: PLC0415

        set_stage_routing(args.routing)
    fn = _STAGES[args.stage]
    if args.stage in ("timeline", "advice"):
        print(f"[stage={args.stage} day={day} lang={lang}]")
        await fn(day, lang)
        return
    if args.ab and args.stage in _AB_STAGES and provider == "local":
        from memory_core.llm_local import MODEL_CATALOG  # noqa: PLC0415

        for tier in MODEL_CATALOG:
            print(f"\n{'═' * 24} {tier} {'═' * 24}")
            t0 = datetime.now()
            await fn(day, lang, provider, tier, args)
            print(f"[{tier}: {(datetime.now() - t0).total_seconds():.0f}s total]")
        return
    print(f"[stage={args.stage} day={day} provider={provider} model={model} lang={lang}]")
    await fn(day, lang, provider, model, args)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

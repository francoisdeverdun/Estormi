# Prompts

This folder hosts two distinct prompt libraries with different audiences:

* `prompts/companion/*.md` — **companion prompts** for the human user. Each
  markdown file is a long-form prompt the Claude CLI runs against the local MCP
  server to produce structured syntheses (weekly review, decision log, project
  context, …). See the table below for the full list.
* `prompts/llm/*.j2` — **LLM templates** used inside Estormi's own pipelines.
  These Jinja2 templates are rendered by `packages/memory_core/prompt_templates.py` and
  fed to the local LLM (or the Claude CLI in the knowledge
  pipeline). Editing one of these immediately changes how the corresponding
  Briefing engine step talks to its LLM — no Python change required.

See `tests/memory_core/test_prompt_templates_contract.py` for the contract: every
template under `prompts/llm/` must parse, render with a minimal context, and
declare every variable it consumes.

## Companion prompts

```bash
make prompt-weekly-review            # via Makefile target
bash scripts/run_prompt.sh weekly-review
bash scripts/run_prompt.sh project-context "name=acme-migration"
```

Output is written to `~/estormi-reports/<date>-<prompt>.md` and a macOS
notification is shown.

| Slug | What it does |
|---|---|
| `weekly-review` | Surfaces themes, decisions, and forgotten action items from the last 7 days across all sources. |
| `project-context` | Pulls everything the brain knows about a named project: notes, mails, calendar mentions, and messages. |
| `decision-log` | Extracts decisions made in a date range with their stated reasons. |
| `idea-extraction` | Mines half-formed ideas mentioned across notes/messages and surfaces ones not yet acted on. |

### Add a new companion prompt

1. Drop a markdown file in `prompts/companion/<slug>.md` with the prompt body.
   Use `{{var_name}}` for substitutions.
2. Run with `bash scripts/run_prompt.sh <slug> "var_name=value"`.
3. Optionally add a `prompt-<slug>` target to the Makefile.

## LLM templates (`prompts/llm/`)

| Template | Where it's rendered | Purpose |
|---|---|---|
| `briefing_critic.j2` | `packages/estormi_briefing/compose/prompts.py` | Self-critique of the composed briefing. |
| `briefing_extractor.j2` | `packages/estormi_briefing/compose/prompts.py` | Structured-fact extraction from sources. |
| `briefing_fact_critic.j2` | `packages/estormi_briefing/compose/prompts.py` | Date/fact-fidelity critique of the composed briefing. |
| `briefing_plan.j2` | `packages/estormi_briefing/compose/composer.py` | Plans the day's threads/skeleton before prose. |
| `briefing_lede.j2` | `packages/estormi_briefing/compose/composer.py` | Best-of-N opening lede for the briefing. |
| `briefing_cohesion.j2` | `packages/estormi_briefing/compose/composer.py` | Cohesion pass linking threads into one narrative. |
| `briefing_thread_writer.j2` | `packages/estormi_briefing/compose/composer.py` | Writes one thread paragraph from its facts. |
| `briefing_readiness.j2` | `packages/estormi_briefing/compose/composer.py` | Health×agenda readiness advice line. |
| `knowledge_analysis.j2` | `packages/estormi_briefing/compose/prompts.py` | Per-video analysis stage. |
| `knowledge_common_rules.j2` | rendered standalone, injected as `common_rules` into news/analysis/opinion/rss | Shared anti-injection rule block. |
| `knowledge_consolidation.j2` | `packages/estormi_briefing/compose/prompts.py` | Theme/news consolidation. |
| `knowledge_day_vision.j2` | `packages/estormi_briefing/compose/prompts.py` | Daily morning brief. |
| `knowledge_narration.j2` | `packages/estormi_briefing/compose/prompts.py` | Spoken-edition rewrite of the composed briefing for TTS narration. |
| `knowledge_news.j2` | `packages/estormi_briefing/compose/prompts.py` | News-source-specific extraction. |
| `knowledge_news_synthesis.j2` | `packages/estormi_briefing/compose/prompts.py` | News-only daily synthesis. |
| `knowledge_opinion.j2` | `packages/estormi_briefing/compose/prompts.py` | Opinion-source extraction. |
| `knowledge_rss.j2` | `packages/estormi_briefing/compose/prompts.py` | RSS-batch summarisation. |
| `knowledge_themes.j2` | `packages/estormi_briefing/compose/prompts.py` | Theme aggregation across sources. |

### Render contract

The shared environment lives in
[`packages/memory_core/prompt_templates.py`](../packages/memory_core/prompt_templates.py):

```python
from memory_core.prompt_templates import render
rendered = render("knowledge_day_vision", date_str="2026-05-19", calendar=…, overdue=…)
```

* `autoescape=False` — output is plain text fed to an LLM, not HTML.
* `trim_blocks=True`, `lstrip_blocks=True` — control tags don't leak
  whitespace into the prompt.

### Add a new LLM template

1. Drop `prompts/llm/<slug>.j2` with the new prompt body.
2. If your prompt receives untrusted user input, reuse the anti-injection
   guardrails the way the knowledge templates do: render
   `knowledge_common_rules.j2` separately (it needs `source_label`/`date_str`/
   `language` — see `_common_prompt_rules` in
   `packages/estormi_briefing/compose/prompts.py`) and pass the resulting string as a
   `common_rules` variable that your template interpolates with
   `{{ common_rules }}`. There is no `{% include %}` directive anywhere.
3. Wrap every untrusted block in explicit delimiters
   (`<conversation>…</conversation>`, `<transcript>…</transcript>`) and tell
   the LLM in plain text that the delimited content is data, not instructions.
4. Add a render-context entry in
   `tests/memory_core/test_prompt_templates_contract.py::CONTEXTS` so the contract
   test exercises your template.
5. Call `render("<slug>", **ctx)` from your Python caller.

# Manual ingestion, schedulers, the daily DAG, models, and companion prompts.
.PHONY: ingest-notes ingest-mail ingest-docs ingest-google-calendar ingest-reminders ingest-whatsapp ingest-imessage install-agents uninstall-agents agents daily-dag model-download tts-model prompts prompt-weekly-review prompt-project-context prompt-decision-log prompt-idea-extraction

## ── Ingestion (manual) ───────────────────────────────────────────────────

ingest-notes: ## Export + ingest Apple Notes
	bash packages/estormi_ingestion/apple_notes/watch_and_ingest.sh

ingest-mail: ## Export + ingest Apple Mail (reads local Mail.app)
	bash packages/estormi_ingestion/apple_mail/watch_and_ingest.sh

ingest-docs: ## Ingest iCloud Drive documents (PDF/DOCX/ODT/PPTX/XLSX/…)
	$(VENV) -m estormi_ingestion.documents.ingest_documents

ingest-google-calendar: ## Incremental sync Google Calendar via OAuth2
	$(VENV) -c "from estormi_ingestion.google_calendar import sync; sync.sync()"

ingest-reminders: ## Export + ingest all Reminders
	bash packages/estormi_ingestion/reminders/watch_and_ingest.sh

ingest-whatsapp: ## Fetch + ingest WhatsApp messages (poll mode)
	bash packages/estormi_ingestion/whatsapp/watch_and_ingest.sh

ingest-imessage: ## Export + ingest iMessages (requires Full Disk Access for Terminal)
	bash packages/estormi_ingestion/imessage/watch_and_ingest.sh

## ── LaunchAgents (schedulers) ────────────────────────────────────────────

install-agents: ## Copy + (re)load scheduled launchd plists
	@# launchd does not expand env vars in plist paths, so we substitute
	@# `__HOME__` into the absolute home path and `__REPO_ROOT__` into this
	@# Makefile's own directory while copying the plist to
	@# ~/Library/LaunchAgents. The log directory is created first so the
	@# StandardOutPath / StandardErrorPath entries don't fail open.
	@# Migration: the daily-ingestion DAG is now scheduled in-process by the
	@# app's APScheduler (so its macOS permission grants are attributed to
	@# Estormi, not a detached launchd job). Retire any previously-installed
	@# daily-dag agent so it can't double-run the DAG and re-prompt for TCC.
	@stale=$$HOME/Library/LaunchAgents/app.estormi.local.daily-dag.plist; \
	if [ -f "$$stale" ]; then \
	  launchctl unload "$$stale" 2>/dev/null || true; \
	  rm -f "$$stale"; \
	  echo "  retired stale agent app.estormi.local.daily-dag (now in-app)"; \
	fi
	@mkdir -p "$$HOME/Library/LaunchAgents" "$$HOME/Library/Logs/Estormi"
	@repo_root="$(CURDIR)"; \
	for p in scripts/app.estormi.local.*.plist; do \
	  [ -f "$$p" ] || continue; \
	  name=$$(basename $$p .plist); \
	  dest=$$HOME/Library/LaunchAgents/$$(basename $$p); \
	  sed -e "s|__HOME__|$$HOME|g" -e "s|__REPO_ROOT__|$$repo_root|g" "$$p" > "$$dest"; \
	  err=$$(launchctl unload $$dest 2>&1); rc=$$?; \
	  if [ $$rc -ne 0 ] && ! echo "$$err" | grep -q 'Could not find specified service'; then \
	    echo "  WARN: launchctl unload $$name: $$err"; \
	  fi; \
	  launchctl load -w $$dest; \
	  echo "  installed $$name"; \
	done
	@launchctl list | grep estormi || true

uninstall-agents: ## Unload + remove all launchd plists
	@for p in $$HOME/Library/LaunchAgents/app.estormi.local.*.plist; do \
	  [ -f $$p ] || continue; \
	  err=$$(launchctl unload $$p 2>&1); rc=$$?; \
	  if [ $$rc -ne 0 ] && ! echo "$$err" | grep -q 'Could not find specified service'; then \
	    echo "  WARN: launchctl unload $$(basename $$p): $$err"; \
	  fi; \
	  rm -f $$p; \
	  echo "  removed $$(basename $$p)"; \
	done

agents: ## Show launchd status for all estormi agents
	@launchctl list | grep estormi || echo "(no agents loaded)"

## ── Daily DAG ────────────────────────────────────────────────────────────

daily-dag: ## Run the full source ingestion DAG
	bash scripts/daily_ingestion.sh

model-download: ## Download the default local-LLM GGUF (Ministral 3 14B Instruct Q4_K_M, ~7.7 GB) from HuggingFace Hub
	@dest="$${ESTORMI_DATA_DIR:-$$HOME/Library/Application Support/Estormi}/models"; \
	ESTORMI_MODELS_DIR="$$dest" $(VENV) -c "\
import os; \
from memory_core.llm_local import DEFAULT_TIER, _MODEL_REPOS, _MODEL_FILES; \
from huggingface_hub import hf_hub_download; \
dest = os.environ['ESTORMI_MODELS_DIR']; os.makedirs(dest, exist_ok=True); \
path = hf_hub_download(repo_id=_MODEL_REPOS[DEFAULT_TIER], filename=_MODEL_FILES[DEFAULT_TIER], local_dir=dest); \
print('Model saved to', path)"

tts-model: ## Download the briefing TTS model (Voxtral 4B MLX 4-bit, ~2.5 GB) from HuggingFace Hub
	@$(VENV) -c "\
from memory_core import tts_local; \
print('TTS model saved to', tts_local.download_model())"

## ── Companion prompts (synthesis via Claude CLI) ─────────────────────────

prompts: ## List available companion prompts
	@ls -1 prompts/companion/*.md 2>/dev/null | xargs -n1 basename | sed 's/\.md$$//' | sed 's/^/  /'

prompt-weekly-review: ## Synthesize themes/decisions/forgotten action items from last 7 days
	bash scripts/run_prompt.sh weekly-review

prompt-project-context: ## Build a dossier on a project (set NAME=<project>)
	bash scripts/run_prompt.sh project-context name=$(NAME)

prompt-decision-log: ## Extract decisions in [AFTER, BEFORE] window
	bash scripts/run_prompt.sh decision-log after=$(AFTER) before=$(BEFORE)

prompt-idea-extraction: ## Surface dormant ideas not yet acted on
	bash scripts/run_prompt.sh idea-extraction

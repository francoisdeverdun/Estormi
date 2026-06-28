"""CLI entry point for the connector registry: ``python -m connectors``.

The registry is the single source of truth for *what ingestion stages
exist and how to run each*. This CLI exposes that to the bash ingestion
pipeline orchestrator (``scripts/daily_ingestion.sh``):

  * ``connectors stages``      — print the ordered nightly pipeline stage list.
  * ``connectors stages --all``— print every pipeline stage (includes ``gcal``).
  * ``connectors run <stage>`` — run that one connector via the registry,
                                 exiting non-zero if it failed.

Connectors do the actual ingestion out-of-process: ``base.py`` launches each
source script as a subprocess from the repo root (``python -m`` resolves
``estormi_ingestion.*`` there), so this CLI never imports the ingestion or
server packages itself.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# ── Path bootstrap ───────────────────────────────────────────────────────────
# This file is .../packages/connectors/__main__.py. Putting packages/ on sys.path
# makes ``import connectors`` resolve when the file is run by path, and
# ``memory_core`` — the only package the connectors import in-process (lazily,
# in permission_gate) — resolve in the repo checkout and in the bundled app alike.
_PKG_DIR = str(Path(__file__).resolve().parent.parent)  # connectors' parent (packages/)
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)

from connectors import dag_stages, permission_gate, registry  # noqa: E402


def _cmd_stages(args: argparse.Namespace) -> int:
    specs = dag_stages(default_only=not args.all)
    for spec in specs:
        print(spec.name)
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    cls = registry.get(args.stage)
    if cls is None:
        print(f"[connectors] unknown stage: {args.stage}", file=sys.stderr)
        return 1
    # Permission gate — never probe TCC here. If the connector needs a macOS
    # permission the preflight already recorded as not-granted, skip cleanly
    # instead of letting the connector trigger a dialog mid-run. The preflight
    # (foreground, attributed to Estormi) is the only place that prompts.
    # Optional permissions (e.g. WhatsApp's Contacts) are soft hints: the
    # preflight still prompts, but a denial must not skip the stage.
    if cls.spec.macos_permissions and not cls.spec.permissions_optional:
        status = permission_gate.persisted_permission_status(args.stage)
        if permission_gate.is_blocked_status(status):
            print(
                f"[connectors] {args.stage}: SKIPPED — permission {status}",
                flush=True,
            )
            return permission_gate.SKIP_EXIT_CODE
    # Bracket the connector's own output with a start/end line so the
    # per-stage log (tailed live by the Settings UI) shows life immediately
    # and ends with an unambiguous outcome.
    print(f"[connectors] {args.stage}: starting", flush=True)
    result = cls().ingest()
    secs = result.duration_ms / 1000
    if not result.ok:
        for err in result.errors:
            print(f"[connectors] {args.stage}: {err}", file=sys.stderr)
        print(f"[connectors] {args.stage}: FAILED in {secs:.1f}s", flush=True)
        return 1
    print(f"[connectors] {args.stage}: ok in {secs:.1f}s", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="connectors", description="Connector registry CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_stages = sub.add_parser("stages", help="print the ordered pipeline stage list")
    p_stages.add_argument(
        "--all",
        action="store_true",
        help="include every pipeline stage (e.g. gcal), not just nightly defaults",
    )
    p_stages.set_defaults(func=_cmd_stages)

    p_run = sub.add_parser("run", help="run a single connector via the registry")
    p_run.add_argument("stage", help="connector / pipeline-stage name (e.g. notes)")
    p_run.set_defaults(func=_cmd_run)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

# connectors

Per-source ingestion adapters. Each connector subclasses `Connector` (via
`ShellConnector` or `ScriptConnector` from `base.py`), declares a typed
`ConnectorSpec`, and registers with the shared `registry` on import.

Full extensibility recipe: see [`docs/connectors.md`](../../docs/connectors.md).

Sources currently registered (nine):

| Source | Base | Corpus | Default nightly stage? |
| --- | --- | --- | --- |
| `mail` | `ShellConnector` | personal | yes |
| `notes` | `ShellConnector` | personal | yes |
| `documents` | `ScriptConnector` | personal | yes |
| `gcal` | `ScriptConnector` | personal | on-demand |
| `imessage` | `ShellConnector` | personal | yes |
| `knowledge` | `ScriptConnector` | world | yes |
| `reminders` | `ShellConnector` | personal | yes |
| `whatsapp` | `ShellConnector` | personal | yes |
| `whoop` | `ScriptConnector` | personal | on-demand |

*Corpus* is not a spec field: chunks derive `corpus=world` server-side when
`spec.name` is in `WORLD_SOURCES` (`estormi_server/storage/writers.py`),
else `personal`. *On-demand* stages set `default_stage=False` — they are
pipeline stages (`dag_stage=True`) but sit out the unattended nightly
run-all, firing only on a per-source ▶ or a scoped pipeline run.

The registry is also the executor's source of truth: `python -m
connectors stages` prints the ordered pipeline stage list and `python -m
connectors run <stage>` runs one connector. `scripts/daily_ingestion.sh`
drives the pipeline entirely through this CLI.

The registry guarantees:

- `spec.name` is unique (`ValueError` on duplicate registration).
- Every registered class has a valid `ConnectorSpec` (`TypeError` otherwise).

These invariants are enforced by `tests/connectors/test_connectors.py`.

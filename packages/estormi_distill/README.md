# estormi_distill

The **Distill** engine: the optional third engine (Apple Silicon only). It
periodically retrains the local prose model on the user's **own briefing
archive** — every briefing in the vault, composed locally and corrected by hand
— and runs through the same run-queue and engine mutex as Ingestion and
Briefing (`ENGINES = ("ingestion", "briefing", "distill")` in
`packages/estormi_server/server/jobs.py`). It sits **off** the daily path — a
briefing composes identically whether or not Distill has ever run. It is
launched by `packages/estormi_server/server/launchers/distill.py`. Full
reference:
[`../../docs/architecture/distillation.md`](../../docs/architecture/distillation.md).

Quick map:

- `run_distill.py` — engine entrypoint (the process the launcher spawns).
- `references.py` — mirror the vault's briefing archive into the refs workspace.
- `dataset.py` — build the QLoRA training dataset from those briefings.
- `trainer.py` — run the local fine-tune and fuse/install the adapter.
- `paths.py` — on-disk locations for models, datasets, and adapters.

Layering: depends on `memory_core`; never reaches up into
`packages/estormi_server/`.

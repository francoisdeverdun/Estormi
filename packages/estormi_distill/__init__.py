"""Estormi Distillation engine — train the local prose quill on your own briefings.

One user gesture ("Distill my quill"), or a weekly schedule, drives the whole
chain: harvest every briefing already in the vault (composed locally and
corrected by hand) into the refs workspace, build stage-shaped training pairs,
QLoRA-train the local prose model on-device via MLX, evaluate against held-out
days, then fuse → GGUF → install as the ``ministral3-14b-estormi`` local-only
tier (the two-quills preset upgrades itself the moment the file exists).

Everything this engine produces is PERSONAL data (the harvested archive, the
dataset, the adapter, the fused weights memorize the user's life) — all
artifacts live under ``<data dir>/distill/`` and must never enter the repo
or any unencrypted sync. See docs/architecture/distillation.md.
"""

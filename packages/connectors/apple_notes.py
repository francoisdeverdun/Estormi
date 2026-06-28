"""Apple Notes — macOS only, delegates to the AppleScript ingestion script."""

from __future__ import annotations

from .base import ConnectorSpec, ShellConnector, registry


@registry.register
class AppleNotesConnector(ShellConnector):
    spec = ConnectorSpec(
        name="notes",
        title="Apple Notes",
        description="Indexes recent Apple Notes via AppleScript export.",
        macos_permissions=("AppleEvents:Notes",),
        dag_stage=True,
        dag_order=1,
        default_stage=True,
        depth_window_env="NOTES_DAYS_WINDOW",
        uses_watermark=True,
    )
    script_path = "packages/estormi_ingestion/apple_notes/watch_and_ingest.sh"

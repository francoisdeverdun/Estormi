"""Apple Reminders — macOS only, EventKit-based, shell-wrapped script."""

from __future__ import annotations

from .base import ConnectorSpec, ShellConnector, registry


@registry.register
class RemindersConnector(ShellConnector):
    spec = ConnectorSpec(
        name="reminders",
        title="Apple Reminders",
        description="Indexes Apple Reminders via the macOS EventKit framework.",
        macos_permissions=("Reminders",),
        dag_stage=True,
        dag_order=5,
        default_stage=True,
        uses_watermark=True,
    )
    script_path = "packages/estormi_ingestion/reminders/watch_and_ingest.sh"

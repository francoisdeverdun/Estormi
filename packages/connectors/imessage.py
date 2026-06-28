"""iMessage — macOS only, requires Full Disk Access for the host process."""

from __future__ import annotations

from .base import ConnectorSpec, ShellConnector, registry


@registry.register
class IMessageConnector(ShellConnector):
    spec = ConnectorSpec(
        name="imessage",
        title="iMessage",
        description="Reads a Full-Disk-Access snapshot of ~/Library/Messages/chat.db for recent message windows.",
        macos_permissions=("FullDiskAccess",),
        dag_stage=True,
        dag_order=6,
        default_stage=True,
        depth_window_env="IMESSAGE_DAYS_WINDOW",
        uses_watermark=True,
    )
    script_path = "packages/estormi_ingestion/imessage/watch_and_ingest.sh"

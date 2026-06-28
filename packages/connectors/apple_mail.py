"""Apple Mail — macOS only, delegates to the AppleScript ingestion script."""

from __future__ import annotations

from .base import ConnectorSpec, ShellConnector, registry


@registry.register
class AppleMailConnector(ShellConnector):
    spec = ConnectorSpec(
        name="mail",
        title="Apple Mail",
        description="Indexes recent messages from local Apple Mail accounts via AppleScript.",
        macos_permissions=("AppleEvents:Mail",),
        dag_stage=True,
        dag_order=2,
        default_stage=True,
        depth_window_env="MAIL_DAYS_WINDOW",
        uses_watermark=True,
    )
    script_path = "packages/estormi_ingestion/apple_mail/watch_and_ingest.sh"

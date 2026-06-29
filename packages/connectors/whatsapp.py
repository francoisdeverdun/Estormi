"""WhatsApp — polls the Rust sidecar at :9877 for new staged messages."""

from __future__ import annotations

from .base import ConnectorSpec, ShellConnector, registry


@registry.register
class WhatsAppConnector(ShellConnector):
    spec = ConnectorSpec(
        name="whatsapp",
        title="WhatsApp",
        description="Indexes WhatsApp messages staged by the Rust sidecar (loopback on :9877).",
        dag_stage=True,
        dag_order=7,
        default_stage=True,
        # WhatsApp's linked-device pairing only volunteers a thin recent window;
        # the sidecar pages older history on demand back to this horizon (see
        # apps/estormi-macos/src/whatsapp). The depth is surfaced as `WHATSAPP_HISTORY_DAYS`
        # to watch_and_ingest.sh, which forwards it to the sidecar's sync-once
        # call. Default 2y — WhatsApp threads are sparse, so a wide window is cheap.
        depth_window_env="WHATSAPP_HISTORY_DAYS",
        default_depth="2y",
        # Contacts resolves phone-number JIDs to real names at chat-list
        # retrieval (``estormi_server/integrations/macos_contacts.py``). Declaring it fires
        # the macOS Contacts prompt when the source is activated, rather
        # than silently mid-sync. WhatsApp still ingests without it — names
        # just fall back to push_name / formatted phone numbers — so the
        # permission is optional and a denial must not skip the stage.
        macos_permissions=("Contacts",),
        permissions_optional=True,
    )
    script_path = "packages/estormi_ingestion/whatsapp/watch_and_ingest.sh"

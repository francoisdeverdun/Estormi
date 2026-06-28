"""Loopback WhatsApp sidecar address + auth header.

A single home for the sidecar base URL and the per-launch auth token lookup,
shared by the WhatsApp router (:mod:`estormi_server.api.whatsapp_settings`),
the admin/overview routers, and the service layer
(:mod:`estormi_server.services.whatsapp`). Keeping it here — a leaf module with
no project imports — avoids both the duplicated constant and the service layer
reaching up into ``api`` for the header builder.
"""

from __future__ import annotations

import os

__all__ = ["SIDECAR_URL", "sidecar_headers"]

#: Base URL of the loopback WhatsApp sidecar (bound to localhost by the host).
SIDECAR_URL = "http://127.0.0.1:9877"


def sidecar_headers() -> dict[str, str]:
    """Auth header for the loopback WhatsApp sidecar API.

    The Tauri host generates ``ESTORMI_WA_TOKEN`` once per launch and shares it
    with this server through the environment; the sidecar rejects any request
    that does not carry it, so another local process cannot read the pairing QR
    or drive the WhatsApp session.
    """
    token = os.environ.get("ESTORMI_WA_TOKEN", "")
    return {"X-Estormi-WA-Token": token} if token else {}

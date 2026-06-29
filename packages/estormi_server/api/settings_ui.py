"""Settings UI router aggregator.

The JSON endpoints backing the Settings SPA used to all live in this file
(~1200 lines mixing WhatsApp, admin reset, knowledge YAML, source toggle,
overview, folder picker). They are now split across the modules listed
below; this module aggregates them under a single ``router`` so
``main.py``'s ``app.include_router(settings_ui.router)`` keeps working.

Layout:

* :mod:`api.whatsapp_settings` — sidecar passthrough + chats CRUD.
* :mod:`api.sources_admin` — source toggle.
* :mod:`api.admin` — reset / maintenance endpoints.
* :mod:`api.knowledge_sources` — knowledge YAML CRUD + URL resolver.
* :mod:`api.apple_folder_picker` — AppleScript folder picker.
* :mod:`api.overview` — settings-page overview aggregator.
"""

from __future__ import annotations

from fastapi import APIRouter

from estormi_server.api import (
    admin,
    apple_folder_picker,
    knowledge_sources,
    overview,
    sources_admin,
    whatsapp_settings,
)

router = APIRouter()
router.include_router(whatsapp_settings.router)
router.include_router(sources_admin.router)
router.include_router(admin.router)
router.include_router(knowledge_sources.router)
router.include_router(apple_folder_picker.router)
router.include_router(overview.router)

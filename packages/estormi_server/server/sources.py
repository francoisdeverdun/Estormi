"""WhatsApp sidecar paths + source-slug validation.

Small shared helpers for the source-management endpoints: the WhatsApp sidecar's
data / live-staging locations (so the reset endpoints don't hardcode them) and
the ASCII source/stage slug guard.
"""

from __future__ import annotations

import os
import re
from pathlib import Path


def _wa_data_dir() -> Path:
    """The macOS app-support directory used by the Tauri shell + WhatsApp sidecar.

    Distinct from the server's ``ESTORMI_DATA_DIR`` (``…/Estormi``) because
    the Tauri bundle writes into its own bundle-identifier directory
    (``app.estormi.local``) — the sidecar drops ``wa.db`` there. Lives here
    so the two endpoints that read/delete it stop hardcoding the literal.
    """
    return Path(
        os.getenv(
            "ESTORMI_TAURI_SUPPORT_DIR",
            os.path.expanduser("~/Library/Application Support/app.estormi.local"),
        )
    )


# WhatsApp sidecar session database. Two endpoints used to hardcode the path;
# centralising the constant removes the duplication and lets a future
# migration move the file in one place.
WA_DB_PATH = _wa_data_dir() / "wa.db"

# WhatsApp "live staging" — the Rust sidecar writes ``.txt`` + ``.meta.json``
# pairs here in real time (``apps/estormi-macos/src/whatsapp/`` joins ``staging/
# whatsapp`` onto Tauri's ``app_data_dir``). It lives under the Tauri bundle
# dir, NOT the server's ``DATA_DIR`` — so a reset that only wipes
# ``DATA_DIR/staging`` leaves these files behind. The reset endpoints clear
# this path explicitly. Matches the first candidate in
# ``estormi_ingestion/whatsapp/watch_and_ingest.sh``.
WA_STAGING_PATH = _wa_data_dir() / "staging" / "whatsapp"


_SLUG_RE = re.compile(r"[A-Za-z0-9_-]+")


def is_valid_source_slug(name: str) -> bool:
    """True iff ``name`` is a safe source/stage slug.

    Source and pipeline-stage names flow through into argv (e.g. ``STAGES=…`` env
    in ``daily_ingestion.sh``) and onto disk (per-source log directories), so
    we restrict them to ASCII ``[A-Za-z0-9_-]+``. ``str.isalnum`` would also
    accept Unicode letters/digits (accents, other scripts) — wider than the
    documented charset — so use an explicit ASCII character class. Empty strings
    are rejected.
    """
    return bool(_SLUG_RE.fullmatch(name))

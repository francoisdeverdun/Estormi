"""Shared request-validation helpers for the API layer.

Small guards reused across endpoints so their behaviour (and client-facing
error wording) can't drift. The endpoints that accept a calendar/chat
``group_type`` — Google Calendar (``calendar_oauth``) and WhatsApp
(``whatsapp_settings``) — previously each open-coded the same check with
divergent messages and casing.
"""

from __future__ import annotations

from collections.abc import Collection

from fastapi import HTTPException


def validate_group_type(value: str, allowed: Collection[str]) -> None:
    """Raise ``HTTPException(422)`` when ``value`` is not in ``allowed``.

    The message lists the allowed values so the client can self-correct, and
    keeps the lowercase ``detail`` convention shared by the rest of the API.
    """
    if value not in allowed:
        raise HTTPException(
            status_code=422,
            detail=f"invalid group_type: {value!r} (allowed: {sorted(allowed)})",
        )

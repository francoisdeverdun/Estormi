"""Shared conversation-label heuristics.

A conversation/chat label is "opaque" when it carries no human identity: a raw
WhatsApp JID or a bare phone number. Such a label must never surface as "who"
— the server's chunk back-fill re-titles chunks carrying one once a real name
resolves, and the briefing prompt builder drops it rather than print a phone
number. Both sides apply the exact same contract, so it lives here in
:mod:`memory_core` where each can import it.

JID contract: a raw JID is ``<digits>[-<digits>]@<host>`` where the host is
one of WhatsApp's three namespaces — ``s.whatsapp.net`` (phone-number DMs),
``lid`` (privacy-masked DMs), ``g.us`` (groups). The user part is the phone
number (DMs) or an opaque group/lid id; either way it identifies no human.
"""

from __future__ import annotations

import re

__all__ = [
    "is_opaque_label",
    "ALL_GROUP_TYPES",
    "GCAL_GROUP_TYPES",
    "WA_GROUP_TYPES",
    "WA_AUTOTAG_CHOICES",
]

# ── group_type vocabulary (single source of truth) ──────────────────────────
# Life-context labels a calendar or chat may carry. Defined once here so the
# Google/Apple-calendar admin and the WhatsApp chat editor can't drift apart.
# The full set is the calendar vocabulary; WhatsApp drops the calendar-only
# self/partner labels; the auto-tag choices are an ORDERED subset (the order is
# rendered verbatim into the auto-tag LLM prompt, so it must stay a tuple).
ALL_GROUP_TYPES = frozenset(
    {
        "me",
        "partner",
        "work",
        "family",
        "couple",
        "friends",
        "organisation",
        "charity",
        "sport",
        "noise",
        "unknown",
    }
)
GCAL_GROUP_TYPES = ALL_GROUP_TYPES
WA_GROUP_TYPES = ALL_GROUP_TYPES - {"me", "partner"}
WA_AUTOTAG_CHOICES: tuple[str, ...] = (
    "work",
    "family",
    "friends",
    "organisation",
    "charity",
    "sport",
    "noise",
)

_RAW_JID_RE = re.compile(r"^\d+(?:-\d+)?@(?:g\.us|lid|s\.whatsapp\.net)$")
_BARE_NUMBER_RE = re.compile(r"^\+?\d[\d\s().-]{4,}$")


def is_opaque_label(name: str) -> bool:
    """True when ``name`` is a raw JID or a bare phone number — no real identity."""
    s = (name or "").strip()
    if not s:
        return True
    if _RAW_JID_RE.match(s):
        return True
    # Bare phone number (possibly spaced/punctuated), with no letters.
    return bool(_BARE_NUMBER_RE.match(s)) and not any(c.isalpha() for c in s)

"""macOS Contacts → phone-number name resolution.

WhatsApp's linked-device protocol never transmits the user's address book, so
a 1:1 chat surfaces as a raw phone-number JID (``33612345678@s.whatsapp.net``)
until the contact's self-set ``push_name`` happens to arrive — and for someone
who has never messaged the user, it never does. This module closes that gap
the way WhatsApp Desktop does: it matches the phone number against the Mac's
own Contacts app.

The lookup is read-only and entirely local. It uses the native **Contacts
framework** via PyObjC (``CNContactStore``) — the same PyObjC-in-sidecar path
``server/permissions.py`` already uses for EventKit (Calendar / Reminders).
Reading through the framework (rather than AppleScript automation) fires the
real ``NSContactsUsageDescription`` system prompt on first access and reports
a reliable TCC authorization status, instead of the silent ``-600`` an
unsigned osascript automation call returns. The phone→name index is cached in
memory + on disk so the Settings UI poll (every few seconds) does not re-scan
the address book each time.

First access triggers the macOS Contacts permission prompt; if the user
declines — or PyObjC/the framework is unavailable (non-macOS) — every helper
here degrades to "no enrichment" rather than erroring.
"""

from __future__ import annotations

# PyObjC exposes the Contacts framework's CN* symbols dynamically at runtime, so
# the static checker can't see them on the module.
# pyright: reportAttributeAccessIssue=false
import json
import os
import re
import threading
import time
from pathlib import Path

import structlog

log = structlog.get_logger()

# Phone numbers are matched on their trailing _SUFFIX_DIGITS digits. Long
# enough to be specific, short enough to survive country-code and trunk-zero
# variance — "+33 6 12 34 56 78", "06 12 34 56 78" and the bare WhatsApp form
# "33612345678" all share the same 9-digit national tail.
_SUFFIX_DIGITS = 9

# A successful index is cached for a week. A long TTL plus on-disk persistence
# means we only re-enumerate the address book occasionally (or on explicit
# refresh), keeping the Settings-UI poll cheap.
_CACHE_TTL_SECONDS = 7 * 24 * 3600.0

# An empty index almost always means the macOS Contacts permission has not
# been granted yet — a transient, user-fixable state. Re-probe it on a short
# cycle so the names appear shortly after the user allows access, instead of
# being locked out for the full TTL.
_EMPTY_CACHE_TTL_SECONDS = 60.0

# How long to block on the (async) Contacts access prompt before giving up.
_ACCESS_TIMEOUT_SECONDS = 120.0

# On-disk persistence path. ``ESTORMI_DATA_DIR`` mirrors the server's data dir
# (resolved here independently to keep this module import-light — it has no
# other reason to pull ``tools``).
_DATA_DIR = Path(
    os.getenv(
        "ESTORMI_DATA_DIR",
        os.path.expanduser("~/Library/Application Support/Estormi"),
    )
)
_DISK_CACHE_PATH = _DATA_DIR / "contacts_index.json"

# Module-level cache: (built_at_monotonic, index). `index` maps a phone-number
# suffix to a name. None until the first build.
_cache: "tuple[float, dict[str, str]] | None" = None


def _digits(raw: str) -> str:
    """Strip everything but digits from a phone number (or a JID userpart)."""
    return re.sub(r"\D", "", raw or "")


def _suffix(raw: str) -> str:
    """Return the trailing _SUFFIX_DIGITS digits of a phone number.

    Shorter inputs are kept whole; an input with no digits yields "". Passing a
    full JID works too — "@s.whatsapp.net" contributes no digits.
    """
    d = _digits(raw)
    return d[-_SUFFIX_DIGITS:] if len(d) > _SUFFIX_DIGITS else d


# ── Native Contacts framework (PyObjC) ────────────────────────────────────────


def _contacts_module():
    """Import the PyObjC ``Contacts`` framework lazily; None when unavailable.

    Kept local so a non-macOS host (CI, Linux dev) or a bundle missing the
    PyObjC frameworks can import this module without error.
    """
    try:
        import Contacts  # noqa: PLC0415

        return Contacts
    except Exception:
        log.debug("macos_contacts.framework_unavailable", exc_info=True)
        return None


def contacts_authorization_status() -> str:
    """Return the TCC Contacts status: authorized / denied / restricted /
    not_determined / unavailable (no framework / not macOS)."""
    C = _contacts_module()
    if C is None:
        return "unavailable"
    try:
        raw = C.CNContactStore.authorizationStatusForEntityType_(C.CNEntityTypeContacts)
    except Exception:
        log.debug("macos_contacts.authorization_status_error", exc_info=True)
        return "unavailable"
    return {
        getattr(C, "CNAuthorizationStatusNotDetermined", 0): "not_determined",
        getattr(C, "CNAuthorizationStatusRestricted", 1): "restricted",
        getattr(C, "CNAuthorizationStatusDenied", 2): "denied",
        getattr(C, "CNAuthorizationStatusAuthorized", 3): "authorized",
    }.get(int(raw), "unavailable")


def _request_access(store, C) -> bool:
    """Block on ``requestAccessForEntityType:completionHandler:``.

    Fires the macOS system prompt when status is not-determined; resolves
    immediately to the prior decision otherwise. Returns whether access was
    granted. The completion handler runs on an internal queue, so a plain
    ``threading.Event`` is enough to wait on — no run-loop spin required.
    """
    done = threading.Event()
    granted_box = {"granted": False}

    def _handler(granted, _error):
        granted_box["granted"] = bool(granted)
        done.set()

    try:
        store.requestAccessForEntityType_completionHandler_(C.CNEntityTypeContacts, _handler)
    except Exception:
        log.debug("macos_contacts.request_access_error", exc_info=True)
        return False
    if not done.wait(timeout=_ACCESS_TIMEOUT_SECONDS):
        return False
    return granted_box["granted"]


def _contacts_pairs() -> "list[tuple[str, str]]":
    """Return ``[(name, phone), ...]`` from the macOS address book.

    Requests Contacts access first (prompting on a fresh install), then
    enumerates every contact's full name + phone numbers via the native
    framework. An empty list means Contacts is unavailable or access was
    denied — the caller treats that as "no enrichment", never an error.
    """
    C = _contacts_module()
    if C is None:
        return []
    try:
        store = C.CNContactStore.alloc().init()
    except Exception:
        log.debug("macos_contacts.store_init_error", exc_info=True)
        return []

    status = contacts_authorization_status()
    if status == "not_determined":
        if not _request_access(store, C):
            return []
    elif status != "authorized":
        # denied / restricted / unavailable — nothing we can do here; the user
        # must grant access in System Settings → Privacy & Security → Contacts.
        return []

    try:
        # CNContactFormatter gives the same "display name" semantics as the
        # Contacts app; pair its required-key descriptor with the phone key.
        name_keys = C.CNContactFormatter.descriptorForRequiredKeysForStyle_(
            C.CNContactFormatterStyleFullName
        )
        req = C.CNContactFetchRequest.alloc().initWithKeysToFetch_(
            [name_keys, C.CNContactPhoneNumbersKey]
        )
    except Exception:
        log.debug("macos_contacts.fetch_request_error", exc_info=True)
        return []

    pairs: list[tuple[str, str]] = []

    def _block(contact, _stop):
        try:
            # Class method +stringFromContact:style: — the 1-arg stringFromContact_
            # selector does not exist (it returns nil), which silently produced
            # nameless results. Pair with the FullName descriptor fetched above.
            name = (
                C.CNContactFormatter.stringFromContact_style_(
                    contact, C.CNContactFormatterStyleFullName
                )
                or ""
            ).strip()
            if not name:
                return
            for labeled in contact.phoneNumbers() or []:
                value = labeled.value()
                num = (value.stringValue() if value is not None else "") or ""
                if num.strip():
                    pairs.append((name, num.strip()))
        except Exception:
            log.debug("macos_contacts.contact_parse_error", exc_info=True)
            return

    try:
        store.enumerateContactsWithFetchRequest_error_usingBlock_(req, None, _block)
    except Exception:
        log.debug("macos_contacts.enumerate_error", exc_info=True)
        return []
    return pairs


def _build_index() -> dict[str, str]:
    """Build the ``{phone-suffix: name}`` index from the address book.

    A suffix claimed by two *different* names is dropped: an ambiguous match
    would mislabel a chat, which is worse than leaving the raw number.
    """
    by_suffix: dict[str, set[str]] = {}
    for name, phone in _contacts_pairs():
        suf = _suffix(phone)
        if suf:
            by_suffix.setdefault(suf, set()).add(name)
    return {suf: next(iter(names)) for suf, names in by_suffix.items() if len(names) == 1}


def _load_disk_cache() -> "tuple[float, dict[str, str]] | None":
    """Return ``(age_seconds, index)`` from the on-disk cache, or ``None``.

    Age is computed from the file's stored ``built_at_wall`` (a POSIX timestamp,
    not a monotonic reading — those reset across reboots and can't be persisted
    meaningfully). A malformed or unreadable file yields ``None`` so the caller
    falls through to a fresh build.
    """
    try:
        raw = _DISK_CACHE_PATH.read_text()
    except OSError:
        return None
    try:
        blob = json.loads(raw)
        built_at = float(blob["built_at_wall"])
        index = blob["index"]
        if not isinstance(index, dict):
            return None
        index = {str(k): str(v) for k, v in index.items()}
    except (ValueError, KeyError, TypeError):
        return None
    age = max(0.0, time.time() - built_at)
    return (age, index)


def _save_disk_cache(index: dict[str, str]) -> None:
    """Persist a freshly built index to disk. Best-effort — IO failure is logged
    but never raised, so the in-memory cache still serves the request.
    """
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _DISK_CACHE_PATH.with_suffix(_DISK_CACHE_PATH.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"built_at_wall": time.time(), "index": index}, ensure_ascii=False)
        )
        # The index maps phone suffixes to third-party contact names — PII.
        # Lock it to owner-only before the atomic rename so it is never
        # world-readable on a multi-user box.
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(_DISK_CACHE_PATH)
    except OSError as e:
        log.warning("macos_contacts.disk_cache_write_failed", error=str(e))


def phone_name_index(*, force_refresh: bool = False) -> dict[str, str]:
    """Return the cached ``{phone-suffix: name}`` index, rebuilding past the TTL.

    Synchronous — it talks to the Contacts framework and may block on the
    access prompt — so call it through ``asyncio.to_thread`` from async code.
    Returns an empty dict when Contacts is unavailable or access was declined.

    Caches aggressively: the in-memory cache is checked first, then a JSON file
    under ``DATA_DIR``, and only on cold start (or ``force_refresh``) past the
    TTL do we re-enumerate the address book — which also avoids re-prompting.
    """
    global _cache
    now = time.monotonic()
    if not force_refresh and _cache is not None:
        ttl = _CACHE_TTL_SECONDS if _cache[1] else _EMPTY_CACHE_TTL_SECONDS
        if now - _cache[0] < ttl:
            return _cache[1]
    if not force_refresh:
        disk = _load_disk_cache()
        if disk is not None:
            age, index = disk
            ttl = _CACHE_TTL_SECONDS if index else _EMPTY_CACHE_TTL_SECONDS
            if age < ttl:
                _cache = (now, index)
                return index
    index = _build_index()
    _cache = (now, index)
    if index:
        _save_disk_cache(index)
    log.info("macos_contacts.index_built", entries=len(index))
    return index


def name_for_phone(phone: str, index: dict[str, str]) -> "str | None":
    """Resolve a phone number — any format, or a bare WhatsApp JID — to a
    Contacts name using ``index``. Returns None when the number is unknown."""
    suf = _suffix(phone)
    return index.get(suf) if suf else None

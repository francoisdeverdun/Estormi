"""Native macOS TCC permission helpers using PyObjC.

When Python launches `osascript` via subprocess, TCC attributes the permission
prompt to `osascript` rather than to `app.estormi.local`. By calling the
EventKit / Contacts frameworks in-process, the request is attributed to the
host app and the user only has to approve once.
"""

from __future__ import annotations

import platform
import threading

import structlog

log = structlog.get_logger()

# EKAuthorizationStatus / CNAuthorizationStatus enum values
_STATUS_MAP = {
    0: "not_determined",
    1: "restricted",
    2: "denied",
    3: "authorized",
}


def _is_darwin() -> bool:
    return platform.system() == "Darwin"


def request_reminders_access() -> bool:
    if not _is_darwin():
        return False
    try:
        import EventKit  # type: ignore
    except ImportError:
        log.warning("EventKit not available; install pyobjc-framework-EventKit")
        return False

    store = EventKit.EKEventStore.alloc().init()
    # authorizationStatusForEntityType_ is a CLASS method — calling it on the
    # instance raises AttributeError (and silently broke the preflight probe).
    status = EventKit.EKEventStore.authorizationStatusForEntityType_(EventKit.EKEntityTypeReminder)
    if status == 3:
        return True

    done = threading.Event()
    result = {"granted": False}

    def _completion(granted, error):  # noqa: ANN001
        result["granted"] = bool(granted)
        done.set()

    # macOS 14+ split Reminders access out of the generic entity-type API.
    store.requestFullAccessToRemindersWithCompletion_(_completion)
    done.wait(timeout=30)
    return result["granted"]


def request_contacts_access() -> bool:
    if not _is_darwin():
        return False
    try:
        import Contacts  # type: ignore
    except ImportError:
        log.warning("Contacts not available; install pyobjc-framework-Contacts")
        return False

    store = Contacts.CNContactStore.alloc().init()
    status = Contacts.CNContactStore.authorizationStatusForEntityType_(
        Contacts.CNEntityTypeContacts
    )
    if status == 3:
        return True

    done = threading.Event()
    result = {"granted": False}

    def _completion(granted, error):  # noqa: ANN001
        result["granted"] = bool(granted)
        done.set()

    store.requestAccessForEntityType_completionHandler_(Contacts.CNEntityTypeContacts, _completion)
    done.wait(timeout=30)
    return result["granted"]


def get_reminders_status() -> str:
    if not _is_darwin():
        return "unavailable"
    try:
        import EventKit  # type: ignore
    except ImportError:
        log.warning("EventKit not available; install pyobjc-framework-EventKit")
        return "unavailable"
    status = EventKit.EKEventStore.authorizationStatusForEntityType_(EventKit.EKEntityTypeReminder)
    return _STATUS_MAP.get(int(status), "unavailable")


def get_contacts_status() -> str:
    if not _is_darwin():
        return "unavailable"
    try:
        import Contacts  # type: ignore
    except ImportError:
        log.warning("Contacts not available; install pyobjc-framework-Contacts")
        return "unavailable"
    status = Contacts.CNContactStore.authorizationStatusForEntityType_(
        Contacts.CNEntityTypeContacts
    )
    return _STATUS_MAP.get(int(status), "unavailable")

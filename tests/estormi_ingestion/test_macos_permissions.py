"""Tests for estormi_ingestion.shared.host.macos_permissions."""

from __future__ import annotations

import platform
import sys
import types
from unittest.mock import MagicMock

import pytest

from estormi_ingestion.shared.host import macos_permissions as mp

pytestmark = pytest.mark.unit

VALID_STATUSES = {"authorized", "denied", "not_determined", "restricted", "unavailable"}


def test_get_contacts_status_returns_valid_string():
    assert mp.get_contacts_status() in VALID_STATUSES


def test_get_reminders_status_returns_valid_string():
    assert mp.get_reminders_status() in VALID_STATUSES


def test_non_darwin_returns_unavailable(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    assert mp.get_reminders_status() == "unavailable"
    assert mp.get_contacts_status() == "unavailable"
    assert mp.request_reminders_access() is False
    assert mp.request_contacts_access() is False


def _fake_eventkit(status_value: int) -> types.ModuleType:
    mod = types.ModuleType("EventKit")
    mod.EKEntityTypeEvent = 0
    mod.EKEntityTypeReminder = 1
    store_cls = MagicMock()
    store_cls.authorizationStatusForEntityType_ = MagicMock(return_value=status_value)
    store_instance = MagicMock()
    # authorizationStatusForEntityType_ is a CLASS method: a real EKEventStore
    # *instance* has no such attribute. Deleting it here makes any instance-call
    # regression (store.authorizationStatusForEntityType_) raise AttributeError,
    # so request_*_access must read the status off the class.
    del store_instance.authorizationStatusForEntityType_

    def _request(_entity, completion):
        completion(status_value == 3, None)

    def _request_reminders(completion):
        completion(status_value == 3, None)

    store_instance.requestAccessToEntityType_completion_ = _request
    store_instance.requestFullAccessToRemindersWithCompletion_ = _request_reminders
    store_cls.alloc.return_value.init.return_value = store_instance
    mod.EKEventStore = store_cls
    return mod


def _fake_contacts(status_value: int) -> types.ModuleType:
    mod = types.ModuleType("Contacts")
    mod.CNEntityTypeContacts = 0
    store_cls = MagicMock()
    store_cls.authorizationStatusForEntityType_ = MagicMock(return_value=status_value)
    store_instance = MagicMock()

    def _request(_entity, completion):
        completion(status_value == 3, None)

    store_instance.requestAccessForEntityType_completionHandler_ = _request
    store_cls.alloc.return_value.init.return_value = store_instance
    mod.CNContactStore = store_cls
    return mod


@pytest.mark.parametrize(
    "status,expected",
    [(0, "not_determined"), (2, "denied"), (3, "authorized")],
)
def test_contacts_status_mapping(monkeypatch, status, expected):
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setitem(sys.modules, "Contacts", _fake_contacts(status))
    assert mp.get_contacts_status() == expected


@pytest.mark.parametrize(
    "status,expected",
    [(0, "not_determined"), (1, "restricted"), (2, "denied"), (3, "authorized")],
)
def test_reminders_status_mapping(monkeypatch, status, expected):
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setitem(sys.modules, "EventKit", _fake_eventkit(status))
    assert mp.get_reminders_status() == expected


def test_request_reminders_authorized_short_circuit(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setitem(sys.modules, "EventKit", _fake_eventkit(3))
    assert mp.request_reminders_access() is True


def test_request_reminders_denied(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setitem(sys.modules, "EventKit", _fake_eventkit(2))
    assert mp.request_reminders_access() is False


def test_import_error_fallback(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    # Force import to fail.
    monkeypatch.setitem(sys.modules, "EventKit", None)
    assert mp.get_reminders_status() == "unavailable"
    assert mp.request_reminders_access() is False

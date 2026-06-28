"""Morning-notification gating — ``run_briefing._decide_notify``.

The fixed morning cron composes the briefing *before* the user wakes, so it must
NOT ring the iOS companion on that write when the WHOOP wake-trigger is enabled
(the poller delivers at real wake). The launcher's ``ESTORMI_BRIEFING_NOTIFY``
env overrides the decision for poller-triggered and manual runs.
"""

from __future__ import annotations

import pytest

import estormi_briefing.run_briefing as rb

pytestmark = pytest.mark.unit


async def test_force_always_notifies(monkeypatch):
    monkeypatch.setenv("ESTORMI_BRIEFING_NOTIFY", "force")
    assert await rb._decide_notify(object()) is True


async def test_silent_never_notifies(monkeypatch):
    monkeypatch.setenv("ESTORMI_BRIEFING_NOTIFY", "silent")
    assert await rb._decide_notify(object()) is False


async def test_default_silent_when_whoop_polling_owns_delivery(monkeypatch):
    """Default (scheduled cron) stays silent when the WHOOP wake-trigger is on."""
    monkeypatch.delenv("ESTORMI_BRIEFING_NOTIFY", raising=False)

    async def _gs(db, key, default=""):
        return {"whoop_polling_enabled": "true"}.get(key, default)

    monkeypatch.setattr(rb, "_get_setting", _gs)
    assert await rb._decide_notify(object()) is False


async def test_default_notifies_when_whoop_polling_disabled(monkeypatch):
    """No wake signal to wait for → notify on compose, as before."""
    monkeypatch.delenv("ESTORMI_BRIEFING_NOTIFY", raising=False)

    async def _gs(db, key, default=""):
        return {"whoop_polling_enabled": "false"}.get(key, default)

    monkeypatch.setattr(rb, "_get_setting", _gs)
    assert await rb._decide_notify(object()) is True

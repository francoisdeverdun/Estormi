"""memory_core.pii_filter — credit-card & code-secret redaction.

These pin the release-blocking PII boundary: a Luhn-valid card is redacted while
order numbers / timestamps are left intact, and the code-secret markers each map
to their specific token kind.
"""

from __future__ import annotations

import pytest

from memory_core.pii_filter import (
    _luhn_ok,
    _redact_credit_cards,
    filter_pii,
    redact_code_secrets,
)

pytestmark = pytest.mark.unit


def test_luhn_ok():
    assert _luhn_ok("4242424242424242")  # valid test PAN
    assert not _luhn_ok("4242424242424241")  # one digit off
    assert not _luhn_ok("1234567890123456")  # a plain numeric run, not a PAN


def test_redact_credit_cards_only_luhn_valid():
    # A Luhn-valid PAN is redacted and the digits vanish.
    out = _redact_credit_cards("paie 4242 4242 4242 4242 ce soir", True)
    assert "[REDACTED:CREDIT_CARD]" in out
    assert "4242" not in out
    # A Luhn-INVALID 16-digit run and a long numeric id/timestamp stay intact
    # (do not mangle order numbers / timestamps into a fake card redaction).
    keep = _redact_credit_cards("commande 1234567890123456 a 20260525120000000", True)
    assert "1234567890123456" in keep
    assert "20260525120000000" in keep


def test_redact_code_secrets_specific_markers():
    cases = {
        "AKIA" + "A" * 16: "[REDACTED:AWS_KEY]",
        "ghp_" + "a" * 36: "[REDACTED:GH_TOKEN]",
        "sk_live_" + "a" * 20: "[REDACTED:STRIPE_KEY]",
        "xoxb-" + "0123456789abc": "[REDACTED:SLACK_TOKEN]",
    }
    for secret, marker in cases.items():
        out = redact_code_secrets(f"token={secret} end")
        assert marker in out, f"{secret} → expected {marker}, got {out}"
        assert secret not in out


def test_filter_pii_redacts_card_end_to_end():
    out = filter_pii("CB 4242 4242 4242 4242")
    assert "[REDACTED:CREDIT_CARD]" in out and "4242" not in out

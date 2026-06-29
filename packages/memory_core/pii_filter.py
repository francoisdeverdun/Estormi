"""Regex-based PII redaction for ingested content.

Client-side defense-in-depth: the server applies its own PII filter at
``/ingest_chunk``. Chunks that pass through ``filter_pii`` here should
be marked with ``meta = {"pii_filtered": true}`` so the server can skip
duplicate work. The flag only suppresses the redundant re-redaction of the
chunk *body*: the server always re-filters the ``title`` regardless (callers
pass a raw title — email subject, calendar title, note first-line), so a
secret in a title never lands raw. See ``estormi_server/storage/writers.py``.
"""

from __future__ import annotations

import re

# Credit-card candidates: 13–19 digits with optional space/dash separators.
# We accept any match the regex sees, but only redact if Luhn validates —
# this kills the prior false-positive epidemic on long timestamps / IDs.
_CC_RE = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")
# Phone numbers — French national and a loose international format.
# - The French branch allows ``+33 0X …`` (the form users actually type) by
#   permitting an optional ``0`` after ``+33``; the previous pattern required
#   the country digit immediately after ``+33`` and missed those numbers.
# - Both branches are bracketed with ``(?<!\d)`` / ``(?!\d)`` lookarounds so
#   they can't start or end inside a longer numeric run (e.g. a 20-digit ID
#   would otherwise have its tail matched as a phone).
# - The international branch is rewritten to a single ``\d{7,14}`` window
#   instead of ``(\d[\s.-]?){7,13}\d``. The old nested-quantifier form is a
#   catastrophic-backtracking pattern on adversarial input.
_PHONE_FR_RE = re.compile(r"(?<!\d)(?:\+33\s*0?|0)[1-9](?:[\s.-]?\d{2}){4}(?!\d)")
_PHONE_INTL_RE = re.compile(r"(?<!\d)\+\d{1,3}[\s.-]?\d{1,4}(?:[\s.-]?\d{2,4}){2,4}(?!\d)")

_PII_PATTERNS = {
    # IBANs in the wild are commonly formatted with spaces every four
    # characters ("FR76 3000 …"). The previous pattern required a continuous
    # 23-char run and missed the spaced form entirely. Allow optional
    # whitespace between every character of the body.
    "french_iban": r"\bFR\d{2}(?:\s?[A-Z0-9]){23}\b",
    # French NIR (numéro de sécurité sociale): 15 digits total, structured as
    # gender(1) year(2) month(2) dept(2) commune(3) order(3) key(2). We anchor
    # on the canonical gender prefix (1 or 2) and the full 15-digit form so the
    # old bare-13-digit pattern no longer redacts harmless numeric strings.
    # The ``(?<![@\d])`` guard keeps a WhatsApp @mention — ``@100000000000002``
    # (a 15-digit ``@lid`` user-part starting with 1) — from being mistaken for
    # an NIR and redacted, which used to erase who a message was addressed to.
    "social_security": (
        r"(?<![@\d])[12]\s?\d{2}\s?(?:0[1-9]|1[0-2]|[2-9]\d|\d[02-9])\s?"
        r"(?:\d{2}|2[AB])\s?\d{3}\s?\d{3}\s?\d{2}\b"
    ),
    "email": r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
    # The negative lookahead prevents the regex from eating an already-emitted
    # ``[REDACTED:*]`` marker on the next pass of the fixed-point loop —
    # otherwise ``password: [REDACTED:PHONE]`` would be re-matched and
    # relabelled ``[REDACTED:PASSWORD_LIKE]``, losing the precise PII type.
    "password_like": (
        r"(?i)(?:password|mot\s+de\s+passe|mdp|passwd)\s*[:=]\s*"
        r"(?!\[REDACTED:)\S+"
    ),
    # OTP codes have two recognisable shapes. The previous pattern used an
    # unbounded ``.*`` lookahead that fired on any 4–8 digit run anywhere a
    # trigger word ("code", "pin", …) appeared *later* in the same chunk —
    # which routinely swallowed wine vintages and other plain years
    # (``millésime 2023``) sitting in the same email as "code postal".
    #
    # Two safer branches:
    #   (1) trigger word followed within a few characters by the digits
    #       (``Code: 123456``, ``OTP 12345``)
    #   (2) a 4–8 digit run with a trigger word within ~30 chars on either
    #       side, excluding bare 19xx/20xx years and digits that are part of
    #       a larger sequence (``2023-05-28``, phone numbers).
    "otp_code": (
        r"(?i)"
        # Branch (a): "Code: 1234", "OTP is 4829", "code de vérif 5678" — trigger
        # word, then 0–2 short connector words, then the digits. Excludes 4-digit
        # years (19xx/20xx) and digits that are part of a date / phone run.
        r"\b(?:code|otp|pin|token|verify|vérif(?:ication)?|authentification|confirmation)\b"
        r"(?:[^\w\n]+(?:\w{1,4}[^\w\n]+){0,2})?"
        r"(?!(?:19|20)\d{2}\b)\d{4,8}\b(?![-/.]\d)"
        # Branch (b): "12345 to verify" — digits then trigger within ~30 chars.
        r"|(?<![\w/-])(?!(?:19|20)\d{2}(?:[^\d]|$))\b\d{4,8}\b(?![-/.]\d)"
        r"(?=.{0,30}?\b(?:code|otp|pin|token|verify|vérif|authentification|confirmation)\b)"
    ),
}
_COMPILED = {k: re.compile(v) for k, v in _PII_PATTERNS.items()}

# Patterns that indicate the entire message is an OTP/verification message —
# these are dropped entirely rather than redacted (no residual value).
_OTP_MESSAGE_PATTERNS = re.compile(
    r"(?i)(?:verification code|code de vérification|is your otp|is your.*code"
    r"|votre code\s*(?:est|:)|your code is|ne le divulguez|do not share"
    r"|ne pas partager|don'?t share|expires? in \d|valable \d)",
)


def is_otp_message(text: str) -> bool:
    """Return True if the entire message is an OTP/2FA notification."""
    return bool(_OTP_MESSAGE_PATTERNS.search(text))


def _luhn_ok(digits: str) -> bool:
    """Return True if ``digits`` (already stripped of non-digits) passes Luhn."""
    if not digits or not digits.isdigit():
        return False
    total = 0
    parity = len(digits) % 2
    for i, ch in enumerate(digits):
        d = ord(ch) - 48
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _redact_credit_cards(text: str, redact: bool) -> str:
    def repl(match: re.Match[str]) -> str:
        raw = match.group(0)
        digits = re.sub(r"\D", "", raw)
        if not (13 <= len(digits) <= 19):
            return raw
        if not _luhn_ok(digits):
            return raw
        return "[REDACTED:CREDIT_CARD]" if redact else ""

    return _CC_RE.sub(repl, text)


def _redact_phones(text: str, redact: bool) -> str:
    # Keep the FRENCH-specific label on French matches so downstream
    # consumers (and tests) can distinguish "+33 6 …" from the looser
    # international branch below.
    fr_replacement = "[REDACTED:PHONE_FR]" if redact else ""
    intl_replacement = "[REDACTED:PHONE]" if redact else ""
    text = _PHONE_FR_RE.sub(fr_replacement, text)

    def intl_repl(match: re.Match[str]) -> str:
        raw = match.group(0)
        # An ISO-date prefix ("+1 2026-05-25T12:34:56") matches the loose phone
        # shape greedily; reject anything carrying a YYYY-MM-DD run so a
        # timestamp is never redacted as a phone number.
        if re.search(r"\d{4}-\d{2}-\d{2}", raw):
            return raw
        # Need ≥ 8 digits and the bulk of the match must be digits.
        digits = re.sub(r"\D", "", raw)
        if len(digits) < 8 or len(digits) > 15:
            return raw
        non_digit = len(raw) - len(digits)
        if non_digit > len(digits):  # mostly separators → not a real phone
            return raw
        return intl_replacement

    return _PHONE_INTL_RE.sub(intl_repl, text)


# Code-specific secret patterns. The shared `filter_pii` is tuned for human
# text (emails, phones, OTPs); code blobs need their own pass to catch
# config files that leaked into source.
_CODE_SECRET_PATTERNS = [
    # AWS access key id
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED:AWS_KEY]"),
    # GitHub personal access tokens (classic + fine-grained prefixes)
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b"), "[REDACTED:GH_TOKEN]"),
    # Generic api_key / secret / token / password = "..." assignments
    (
        re.compile(
            r"(?i)\b(api[_-]?key|secret(?:[_-]?key)?|access[_-]?token|auth[_-]?token|password|passwd|bearer)\b"
            r"\s*[:=]\s*[\"']?[A-Za-z0-9_\-./+=]{8,}[\"']?",
        ),
        "[REDACTED:SECRET]",
    ),
    # Stripe live keys
    (re.compile(r"\bsk_live_[A-Za-z0-9]{16,}\b"), "[REDACTED:STRIPE_KEY]"),
    # Slack bot/user tokens
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"), "[REDACTED:SLACK_TOKEN]"),
]


def redact_code_secrets(text: str) -> str:
    """Strip secret-shaped tokens from a code blob.

    Applied before `filter_pii` for the `code` source so generic API keys,
    tokens and password assignments never reach the embedding model.
    """
    for pattern, replacement in _CODE_SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


_MAX_PASSES = 5


def filter_pii(text: str, redact: bool = True) -> str:
    """Redact PII from ``text`` to a fixed point (max 5 passes).

    The fixed-point loop lets nested patterns converge — e.g. an email
    embedded inside a quoted password assignment, or a phone within a
    longer redaction-target run.

    Order matters: IBAN and credit-card patterns can eat the leading digits
    of a phone match, so they must run first. Phones run before the generic
    OTP/email patterns for the same reason.
    """
    for _ in range(_MAX_PASSES):
        before = text
        # Structured high-precision matches first (IBAN, NIR).
        for pii_type in ("french_iban", "social_security"):
            pattern = _COMPILED[pii_type]
            replacement = f"[REDACTED:{pii_type.upper()}]" if redact else ""
            text = pattern.sub(replacement, text)
        text = _redact_credit_cards(text, redact)
        text = _redact_phones(text, redact)
        # Remaining (email / password / otp).
        for pii_type, pattern in _COMPILED.items():
            if pii_type in ("french_iban", "social_security"):
                continue
            replacement = f"[REDACTED:{pii_type.upper()}]" if redact else ""
            text = pattern.sub(replacement, text)
        if text == before:
            break
    return text

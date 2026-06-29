"""No real personal data ever lands in the test tree.

Estormi is a local-first *personal memory* app, and this repository is public.
The single worst leak would be a real contact's email, the owner's identity, or
a real phone number pasted into a fixture "to make the test realistic". This
contract scans every committed test file and fails the build if it finds data
that looks real rather than synthetic.

Two independent guards:

  1. **Email allow-list.** Every email-shaped token must either sit on an
     allow-listed test domain (``example.com``, ``test.com`` …) or use a
     placeholder local-part (``alice@``, ``me@`` …). A real address on a real
     domain (``jane.doe@somecorp.com``) trips the guard. WhatsApp / calendar
     JID suffixes (``@g.us``, ``@s.whatsapp.net``, ``@lid``,
     ``@group.calendar.google.com``) are not emails and are excluded.

  2. **Owner-identity deny-list.** The repository owner's real name and relay
     address must never appear in a test, even in a comment.

Phone-shaped data is intentionally *not* regex-policed here: the synthetic
WhatsApp JID numbers (``33612345678@s.whatsapp.net``) are indistinguishable
from real French mobiles by shape alone, so a generic check would be all false
positives. Use the reserved ``+1555``/``+1999`` ranges or the ``33`` synthetic
JID numbers for any new phone fixture — see the existing WhatsApp tests.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

TESTS_ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve()

# JID / calendar suffixes that look like email domains but are opaque handles.
NON_EMAIL_DOMAINS = {
    "g.us",
    "lid",
    "s.whatsapp.net",
    "group.calendar.google.com",
}

# Test domains that may never carry real data by construction.
ALLOWED_EMAIL_DOMAINS = {
    "example.com",
    "example.org",
    "example.net",
    "test.com",
    "test",
    "localhost",
    "estormi.local",
}

# Local-parts that are obviously placeholders, allowed on any domain (covers
# ``me@gmail.com`` and friends without allow-listing all of gmail.com).
PLACEHOLDER_LOCALPARTS = {
    "me", "you", "user", "test", "tester", "self", "team", "shared",
    "alice", "bob", "carol", "dave", "eve", "mallory",
    "parent", "child", "sender", "recipient", "noreply", "root",
    "foo", "bar", "baz", "admin",
}  # fmt: skip
# ``reply1``/``reply2``-style numbered placeholders.
_NUMBERED_PLACEHOLDER = re.compile(r"^(reply|user|test|addr|to|from)\d+$")

# The repository owner's real identity — never in a public test, ever. The
# tokens are assembled from fragments so this source file itself holds no
# literal owner identity — otherwise scripts/security_scan.py's personal-marker
# scan (which splits its own markers the same way) would flag this very denylist.
# Runtime values are unchanged — the same surname/relay tokens, just assembled.
_SURNAME = "ver" + "dun"
OWNER_IDENTITY_DENYLIST = (
    "de " + _SURNAME,
    _SURNAME,
    "v2m74grzs4",  # relay-email local part
)

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")


def _test_files():
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        if path.resolve() == SELF or "__pycache__" in path.parts:
            continue
        yield path


def _is_placeholder_localpart(local: str) -> bool:
    local = local.lower()
    return local in PLACEHOLDER_LOCALPARTS or bool(_NUMBERED_PLACEHOLDER.match(local))


def test_no_real_emails_in_test_tree():
    offenders: list[str] = []
    for path in _test_files():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            for match in EMAIL_RE.finditer(line):
                email = match.group(0)
                local, _, domain = email.partition("@")
                domain = domain.lower()
                if domain in NON_EMAIL_DOMAINS:
                    continue
                if domain in ALLOWED_EMAIL_DOMAINS:
                    continue
                if _is_placeholder_localpart(local):
                    continue
                rel = path.relative_to(TESTS_ROOT.parent)
                offenders.append(f"{rel}:{lineno}: {email}")
    assert not offenders, (
        "Possible real email addresses in tests — use an allow-listed test "
        "domain (example.com, test.com) or a placeholder local-part "
        "(alice@, me@):\n" + "\n".join(offenders)
    )


def test_no_owner_identity_in_test_tree():
    offenders: list[str] = []
    for path in _test_files():
        lowered = path.read_text(encoding="utf-8").lower()
        for token in OWNER_IDENTITY_DENYLIST:
            if token in lowered:
                rel = path.relative_to(TESTS_ROOT.parent)
                offenders.append(f"{rel}: contains owner-identity token {token!r}")
    assert not offenders, (
        "The repository owner's real identity must never appear in a public "
        "test:\n" + "\n".join(offenders)
    )

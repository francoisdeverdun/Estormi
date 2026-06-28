"""The pick-folder prompt is interpolated into an AppleScript string.

Anything passed there has to be neutralised first: the double-quote and
backslash are the only AppleScript string metacharacters, but a control
character (CR, LF) also breaks out by splitting the statement. The sanitiser
is what stops a payload like::

    Hi" & (do shell script "open -a Calculator") & "

from reaching osascript.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def sanitize():
    from estormi_server.api.apple_folder_picker import _sanitize_pick_folder_prompt

    return _sanitize_pick_folder_prompt


@pytest.mark.unit
def test_passthrough_for_simple_prompt(sanitize):
    assert sanitize("Choose a folder") == "Choose a folder"


@pytest.mark.unit
def test_escapes_double_quotes(sanitize):
    out = sanitize('Hi" & evil()')
    assert '"' not in out.replace('\\"', "")
    assert '\\"' in out  # quote was escaped, not stripped


@pytest.mark.unit
def test_escapes_backslash(sanitize):
    # Two raw backslashes in → four backslashes out (\\ → \\\\).
    out = sanitize("a\\b\\c")
    assert out == "a\\\\b\\\\c"


@pytest.mark.unit
def test_strips_control_characters(sanitize):
    payload = "normal\nthen newline\rand cr\tand tab"
    out = sanitize(payload)
    for ch in ("\n", "\r", "\t"):
        assert ch not in out


@pytest.mark.unit
def test_clamps_length(sanitize):
    out = sanitize("x" * 5000)
    assert len(out) <= 200


@pytest.mark.unit
def test_non_string_yields_default(sanitize):
    assert sanitize(None) == "Select a folder:"  # type: ignore[arg-type]
    assert sanitize(42) == "Select a folder:"  # type: ignore[arg-type]


@pytest.mark.unit
def test_backslash_on_clamp_boundary_cannot_escape_closing_quote(sanitize):
    # Regression: clamping AFTER escaping could cut an escaped "\\" pair in
    # half, leaving an odd trailing backslash run that escapes the closing
    # quote when interpolated into f'"{out}"'. Put a backslash exactly on the
    # 200-char boundary; the trailing backslash run must be EVEN.
    out = sanitize("a" * 199 + "\\" + "TAIL")
    trailing = len(out) - len(out.rstrip("\\"))
    assert trailing % 2 == 0, (
        f"odd trailing backslash run ({trailing}) would escape the closing quote"
    )


@pytest.mark.unit
def test_injection_payload_is_neutralised(sanitize):
    payload = 'Hi" & (do shell script "open -a Calculator") & "'
    out = sanitize(payload)
    # Whatever sneaks through, it must NOT contain an unescaped " — once
    # interpolated into f'"{out}"' the resulting AppleScript string must
    # remain syntactically a single literal.
    quote_count = 0
    i = 0
    while i < len(out):
        if out[i] == "\\":
            i += 2  # skip the escaped character
            continue
        if out[i] == '"':
            quote_count += 1
        i += 1
    assert quote_count == 0

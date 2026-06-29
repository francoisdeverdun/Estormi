"""Local LLM plumbing — _claude_bin, _maybe_truncate, claude-cli retry."""

from __future__ import annotations

import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from estormi_briefing.compose.synthesis import _maybe_truncate
from estormi_briefing.llm.llm_dispatch import _claude_bin, _llm_call_dispatch

pytestmark = pytest.mark.unit

# ── llm_dispatch: _claude_bin ────────────────────────────────────────────────


def test_claude_bin_finds_fixed_path(tmp_path):
    """Returns the first fixed candidate that exists on disk."""
    fake_claude = tmp_path / "claude"
    fake_claude.touch(mode=0o755)

    with patch("estormi_briefing.llm.llm_dispatch._CLAUDE_SEARCH_PATHS", [str(fake_claude)]):
        result = _claude_bin()

    assert result == str(fake_claude)


def test_claude_bin_falls_back_to_which(tmp_path):
    """Falls back to shutil.which when fixed paths don't exist."""
    with (
        patch("estormi_briefing.llm.llm_dispatch.shutil.which", return_value="/usr/bin/claude"),
        patch("pathlib.Path.exists", return_value=False),
        patch("pathlib.Path.is_dir", return_value=False),
    ):
        result = _claude_bin()
    assert result == "/usr/bin/claude"


def test_claude_bin_raises_when_missing():
    """Raises FileNotFoundError when claude cannot be found anywhere."""
    with (
        patch("estormi_briefing.llm.llm_dispatch.shutil.which", return_value=None),
        patch("pathlib.Path.exists", return_value=False),
        patch("pathlib.Path.is_dir", return_value=False),
        pytest.raises(FileNotFoundError, match="claude CLI not found"),
    ):
        _claude_bin()


# ── run_knowledge: _maybe_truncate ───────────────────────────────────────────


def test_maybe_truncate_local_long_text():
    from estormi_briefing.compose.synthesis import _LOCAL_MAX_CHARS, _maybe_truncate

    long_text = "x" * (_LOCAL_MAX_CHARS + 1000)
    result = _maybe_truncate(long_text, "local")
    assert len(result) <= _LOCAL_MAX_CHARS + 50  # small overhead for suffix
    assert "[transcript truncated]" in result


def test_maybe_truncate_local_short_text():
    short = "hello world"
    assert _maybe_truncate(short, "local") == short


def test_maybe_truncate_cli_keeps_normal_text():
    """Normal-length transcripts pass through untouched on the claude CLI;
    only a pathological transcript past the high CLI cap is trimmed."""
    from estormi_briefing.compose.synthesis import _LOCAL_MAX_CHARS, _maybe_truncate

    long_text = "x" * (_LOCAL_MAX_CHARS * 3)  # well under the CLI cap
    result = _maybe_truncate(long_text, "claude-cli")
    assert result == long_text
    assert "[transcript truncated]" not in result


# ── run_knowledge: claude-cli retry ──────────────────────────────────────────


async def test_dispatch_retries_then_succeeds():
    """A transient CLI timeout on the first attempt is ridden out by a retry."""
    calls = {"n": 0}

    def fake_run(*_a, **_k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise subprocess.TimeoutExpired(cmd="claude", timeout=1)
        return MagicMock(stdout="  ok  ")

    with (
        patch("estormi_briefing.llm.llm_dispatch._claude_bin", return_value="/x/claude"),
        patch("estormi_briefing.llm.llm_dispatch.subprocess.run", side_effect=fake_run),
        patch("estormi_briefing.llm.llm_dispatch._CLI_ATTEMPTS", 2),
        patch("estormi_briefing.llm.llm_dispatch.asyncio.sleep", new_callable=AsyncMock),
    ):
        out = await _llm_call_dispatch("prompt", "claude-cli", "opus")

    assert out == "ok"
    assert calls["n"] == 2


async def test_dispatch_raises_after_exhausting_attempts():
    """When every attempt times out the final exception propagates so the
    caller degrades the section (and, on total collapse, the run)."""

    def fake_run(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)

    with (
        patch("estormi_briefing.llm.llm_dispatch._claude_bin", return_value="/x/claude"),
        patch("estormi_briefing.llm.llm_dispatch.subprocess.run", side_effect=fake_run),
        patch("estormi_briefing.llm.llm_dispatch._CLI_ATTEMPTS", 2),
        patch("estormi_briefing.llm.llm_dispatch.asyncio.sleep", new_callable=AsyncMock),
        pytest.raises(subprocess.TimeoutExpired),
    ):
        await _llm_call_dispatch("prompt", "claude-cli", "opus")


async def test_dispatch_local_maps_decode_options():
    """The local branch must forward every decode option to chat_completion."""
    from unittest.mock import AsyncMock, patch

    from estormi_briefing.llm.llm_dispatch import _llm_call_dispatch

    fake = AsyncMock(return_value="ok")
    with patch("memory_core.llm_local.chat_completion", fake):
        out = await _llm_call_dispatch(
            "p",
            "local",
            "tier",
            max_tokens=321,
            temperature=0.7,
            json_schema={"type": "object"},
            timeout=42.0,
        )
    assert out == "ok"
    kwargs = fake.await_args.kwargs
    assert kwargs["max_tokens"] == 321
    assert kwargs["temperature"] == 0.7
    assert kwargs["timeout"] == 42.0
    assert kwargs["response_format"] == {"type": "json_object", "schema": {"type": "object"}}
    assert kwargs["gbnf_grammar"] is None

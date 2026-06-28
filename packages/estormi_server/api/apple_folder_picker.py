"""macOS folder picker via AppleScript.

Exposes ``POST /api/pick-folder`` so the Settings SPA can ask the user to
choose a folder (e.g. the iCloud Drive vault root). The user-supplied
prompt is sanitised before being embedded in the AppleScript string.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from estormi_server.server.limiter import limiter

router = APIRouter()


class PickFolderBody(BaseModel):
    # The prompt is later clamped to 200 chars by ``_sanitize_pick_folder_prompt``;
    # this 500-char request-side cap stops a hostile caller forcing FastAPI to
    # buffer (and ``request.json()`` to parse) a multi-megabyte body before
    # the sanitiser even runs.
    prompt: str = Field(default="Select a folder:", max_length=500)


def _sanitize_pick_folder_prompt(prompt: str) -> str:
    """Make a user-supplied prompt safe to embed in an AppleScript string.

    AppleScript strings are double-quoted; the only metacharacters that can
    break out are ``"`` and ``\\``. We escape both, drop control characters,
    and clamp the length so a giant payload can't blow out the osascript
    command line. The result is *interpolated* into the script — which is why
    every byte going in has to be neutralised first.
    """
    if not isinstance(prompt, str):
        return "Select a folder:"
    # Strip control chars (including \r\n) — AppleScript would interpret them
    # as statement separators.
    cleaned = "".join(c for c in prompt if c.isprintable())
    # Clamp the RAW string BEFORE escaping. Clamping the escaped string could
    # cut an escaped "\\" pair in half, leaving a dangling backslash that
    # escapes the closing quote and breaks out of the AppleScript literal.
    cleaned = cleaned[:200]
    cleaned = cleaned.replace("\\", "\\\\").replace('"', '\\"')
    return cleaned or "Select a folder:"


@router.post("/api/pick-folder")
@limiter.limit("6/minute")
async def pick_folder(request: Request, body: PickFolderBody | None = None):
    import subprocess  # noqa: PLC0415

    # Missing / invalid body falls back to the default prompt; Pydantic
    # rejects oversize payloads at the boundary before they hit the
    # event loop's JSON parser.
    raw_prompt = body.prompt if body is not None else "Select a folder:"
    prompt = _sanitize_pick_folder_prompt(raw_prompt)
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            [
                "osascript",
                "-e",
                f'POSIX path of (choose folder with prompt "{prompt}")',
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            return {"path": None}
        path = result.stdout.strip().rstrip("/")
        return {"path": path or None}
    except Exception:
        return {"path": None}  # best-effort: picker cancelled/unavailable yields no path

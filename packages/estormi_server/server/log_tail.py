"""Bounded log-tail reader for the engine + stage log endpoints.

The previous pattern was ``Path.read_text().splitlines()[-n:]`` — bounded by
log size, not by the lines requested. A multi-MB log read whole on every
log-modal open competed with sqlite for the threadpool. ``tail_lines``
seeks from EOF and only reads the trailing window (default 128 KB), which
is enough for hundreds of typical lines but bounded regardless of file
size.
"""

from __future__ import annotations

import os


def tail_lines(path: str | os.PathLike, n_lines: int, window_bytes: int = 131072) -> str:
    """Return the last ``n_lines`` of ``path`` as a single ``\\n``-joined string.

    Reads at most ``window_bytes`` from the end of the file. If the window
    starts mid-line, that partial first line is dropped. Returns ``""`` for
    non-existent files (callers handle the empty case).
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return ""
    if size == 0:
        return ""
    start = max(0, size - window_bytes)
    with open(path, "rb") as fh:
        fh.seek(start)
        data = fh.read()
    text = data.decode("utf-8", errors="replace")
    if start > 0:
        # Drop the (possibly truncated) first line — we only have its tail.
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1 :]
    return "\n".join(text.splitlines()[-n_lines:])

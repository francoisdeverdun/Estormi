"""Shared helper for the SSE-driven model/TTS download endpoints."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import structlog

log = structlog.get_logger()


def make_log_abandoned_download(log_key: str) -> Callable[[asyncio.Task], None]:
    """Build a ``Task.add_done_callback`` that surfaces a backgrounded download's
    outcome after the client disconnects.

    The SSE generator that owned the task was cancelled, so nothing awaits the
    result. The returned callback consumes it — logging any exception under
    ``log_key`` — so a failure surfaces in the logs instead of as a GC
    "exception was never retrieved" warning. ``add_done_callback`` invokes the
    callback with the task as its sole argument.
    """

    def _log_abandoned_download(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error(log_key, error=str(exc))

    return _log_abandoned_download

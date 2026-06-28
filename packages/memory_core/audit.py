"""Structured audit log for every tool call.

Canonical source. Imports a *local* structlog `BoundLogger` rather than
calling `structlog.configure(...)` globally, so the rest of the process keeps
its own logging configuration. The audit file is opened lazily and never
overwrites stderr for non-audit consumers.
"""

from __future__ import annotations

import atexit
import hashlib
import os
import sys
import threading
from pathlib import Path
from typing import IO

import structlog

from .settings import AUDIT_LOG_MAX_BYTES, AUDIT_LOG_PATH


class _RotatingAuditFile:
    """Tiny size-bounded log writer used by structlog's PrintLoggerFactory.

    Keeps the audit file from growing unbounded on a long-lived Mac install:
    once the live file crosses :data:`AUDIT_LOG_MAX_BYTES`, it's renamed to
    ``<path>.1`` (replacing any previous backup) and a fresh empty file
    takes its place. Falls back to stderr on irrecoverable I/O errors —
    audit logging is best-effort, never a hard dependency.
    """

    def __init__(self, path: str, max_bytes: int) -> None:
        self._path = path
        self._max_bytes = max(0, int(max_bytes))
        self._lock = threading.Lock()
        self._fh: IO[str] | None = None
        self._bytes_since_rotate_check = 0
        self._fallback_to_stderr = False
        self._open_or_fallback()

    def _open_or_fallback(self) -> None:
        try:
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
            # Owner-only (0600): the audit log records every tool call and may
            # echo arguments, so it gets the same permissions as the other
            # sensitive state files rather than inheriting the process umask.
            # The mode argument only applies on creation, so chmod an existing
            # file too (best-effort — a hostile FS that rejects chmod still logs).
            fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            self._fh = os.fdopen(fd, "a", buffering=1, encoding="utf-8")
            try:
                os.chmod(self._path, 0o600)
            except OSError:
                pass
        except OSError as exc:
            # One-shot stderr warning, then permanently degrade to stderr.
            print(
                f"audit: could not open {self._path} ({exc}); using stderr",
                file=sys.stderr,
            )
            self._fh = None
            self._fallback_to_stderr = True

    def _maybe_rotate(self, just_wrote: int) -> None:
        if self._max_bytes <= 0 or self._fh is None or self._fallback_to_stderr:
            return
        # Cheap heuristic: only stat() once every ~1 KiB of writes. Avoids
        # syscalls per line on a busy server.
        self._bytes_since_rotate_check += just_wrote
        if self._bytes_since_rotate_check < 1024:
            return
        self._bytes_since_rotate_check = 0
        try:
            size = os.fstat(self._fh.fileno()).st_size
        except OSError:
            return
        if size < self._max_bytes:
            return
        backup = self._path + ".1"
        try:
            self._fh.close()
        except OSError:
            pass
        try:
            # Replace any previous backup; on POSIX rename is atomic so a
            # concurrent reader of <path> never sees a truncated file.
            os.replace(self._path, backup)
        except OSError:
            # Couldn't rotate — re-open and keep going. Don't lose audit.
            pass
        self._open_or_fallback()

    def write(self, msg: str) -> int:
        # structlog's PrintLogger calls write+flush per log line.
        with self._lock:
            if self._fh is None:
                # Permanent fallback or transient close — write to stderr so
                # the log line is at least visible to launchd's stdout file.
                sys.stderr.write(msg)
                return len(msg)
            try:
                n = self._fh.write(msg)
            except OSError as exc:
                # Disk full / fd lost — try to reopen once. Don't loop.
                try:
                    self._fh.close()
                except OSError:
                    pass
                self._open_or_fallback()
                if self._fh is not None:
                    try:
                        n = self._fh.write(msg)
                    except OSError:
                        sys.stderr.write(f"audit: write failed ({exc}); {msg}")
                        return len(msg)
                else:
                    sys.stderr.write(f"audit: write failed ({exc}); {msg}")
                    return len(msg)
            self._maybe_rotate(n)
            return n

    def flush(self) -> None:
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.flush()
                except OSError:
                    pass

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.close()
                except OSError:
                    pass
                self._fh = None


_audit_writer: _RotatingAuditFile | None = None
_audit_file_lock = threading.Lock()


def _open_audit_file() -> _RotatingAuditFile:
    """Return the singleton rotating audit-file writer, opening it lazily."""
    global _audit_writer
    if _audit_writer is not None:
        return _audit_writer
    with _audit_file_lock:
        if _audit_writer is not None:
            return _audit_writer
        _audit_writer = _RotatingAuditFile(AUDIT_LOG_PATH, AUDIT_LOG_MAX_BYTES)
        atexit.register(_audit_writer.close)
        return _audit_writer


_audit_logger: structlog.BoundLogger | None = None


def _get_logger() -> structlog.BoundLogger:
    """Return a private, lazily-built structlog logger.

    Crucially this does NOT call `structlog.configure()` — that would
    repoint every other structlog consumer in the process to the audit
    file. We build our own wrapped logger out of `BoundLoggerLazyProxy`
    so it stays isolated.
    """
    global _audit_logger
    if _audit_logger is not None:
        return _audit_logger
    # _RotatingAuditFile is a writable file-like; PrintLoggerFactory types `file`
    # as TextIO but only ever calls .write()/.flush() on it.
    factory = structlog.PrintLoggerFactory(file=_open_audit_file())  # pyright: ignore[reportArgumentType]
    logger = structlog.wrap_logger(
        factory(),
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
    ).bind(logger="audit")
    _audit_logger = logger
    return logger


def _digest(value: str | bytes) -> str:
    """Truncated sha256 — never log raw subject/email/query content."""
    if not value:
        return ""
    if not isinstance(value, (str, bytes)):
        # Defensive: callers in test code occasionally pass a MagicMock or
        # other non-string when wiring up audit calls. Never blow up audit.
        value = str(value)
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()[:16]


def log_tool_call(
    *,
    token_sub: str,
    token_email: str,
    tool_name: str,
    query: str,
    result_ids: list[str],
    duration_ms: float,
    error: str | None = None,
) -> None:
    """Append one structured JSON line per tool invocation."""
    _get_logger().info(
        "tool_call",
        subject_hash=_digest(token_sub),
        email_hash=_digest(token_email),
        tool=tool_name,
        query_hash=_digest(query),
        query_chars=len(query),
        result_count=len(result_ids),
        result_id_hashes=[_digest(result_id) for result_id in result_ids[:10]],
        duration_ms=round(duration_ms, 2),
        error=bool(error),
        error_hash=_digest(error or ""),
    )


def log_security_decision(
    *,
    decision: str,
    path: str,
    client_host: str,
    reason: str,
    method: str | None = None,
) -> None:
    """Append one structured JSON line per security-relevant decision.

    Captures middleware rejections (bearer mismatch, forwarded-without-token,
    CSRF reject) and accept events on security-sensitive endpoints
    (admin/reset, settings PUT, open-url). Raw query strings, emails, and
    chunk content never appear — only the path, method, and hashed client
    host. The `decision` field is one of: ``accept``, ``reject``.
    """
    _get_logger().info(
        "security_decision",
        decision=decision,
        path=path,
        method=method or "",
        client_hash=_digest(client_host or ""),
        reason=reason,
    )

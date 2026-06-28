"""Tests for the document-ingestion file pipeline (``ingest_documents.ingest_file``).

These drive the *real* :func:`ingest_file` — the per-file unit that the walker
calls — with only the genuinely external edges stubbed: the per-file extractor
(``EXTRACTORS[ext]``), the iCloud download wait (``ensure_downloaded``), and the
HTTP emit (module-level ``post_chunks``). Everything in between — the
transient-vs-clean failure contract, the size / min-length gates, and the
PII/secret scrubbing that runs before any chunk leaves the machine — is the
behaviour under test.

The transient flag is release-critical: a transient failure must leave the
watermark untouched (so the file is retried) while a clean empty result must
not, and the two are indistinguishable by the ``(0, 0, 0)`` chunk counters
alone — so each gate is pinned to the right boolean. The *unreadable* flag is
the third state: a deterministically unprocessable file (encrypted/corrupt PDF)
must be neither transient (it would pin the watermark forever) nor a silent
clean skip (it must stay counted/visible) — so it gets its own boolean, also
pinned per gate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from estormi_ingestion.documents import ingest_documents
from estormi_ingestion.shared.emit import EmitCounts

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


class _PostCapture:
    """Stand-in for the module-level ``post_chunks`` that records its call.

    Returns a benign all-ok :class:`EmitCounts` so ``ingest_file`` reports a
    clean, non-transient result whenever the emit is actually reached.
    """

    def __init__(self) -> None:
        self.called = False
        self.chunks: list[str] | None = None
        self.kwargs: dict[str, Any] | None = None

    def __call__(self, source: str, source_id: str, chunks, **kwargs) -> EmitCounts:
        self.called = True
        self.chunks = list(chunks)
        self.kwargs = kwargs
        return EmitCounts(ok=len(self.chunks), skipped=0, failed=0)


@pytest.fixture
def capture_post(monkeypatch: pytest.MonkeyPatch) -> _PostCapture:
    """Replace the module-level ``post_chunks`` with a recording fake.

    ``ingest_documents`` binds ``post_chunks`` into its own namespace at import
    (``from ...emit import post_chunks``), so the substitution has to target the
    name in *that* module, not ``emit.post_chunks``.
    """
    cap = _PostCapture()
    monkeypatch.setattr(ingest_documents, "post_chunks", cap)
    return cap


@pytest.fixture
def downloaded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ``ensure_downloaded`` to report the file is locally available.

    The real implementation shells out to ``brctl``/``xattr`` and sleeps; here it
    is irrelevant to what we are testing, so it always succeeds immediately.
    """
    monkeypatch.setattr(ingest_documents, "ensure_downloaded", lambda *a, **k: True)


def _set_extractor(monkeypatch: pytest.MonkeyPatch, ext: str, fn) -> None:
    """Swap one entry in the module's EXTRACTORS table (copy so we never mutate
    the shared dict across tests)."""
    table = dict(ingest_documents.EXTRACTORS)
    table[ext] = fn
    monkeypatch.setattr(ingest_documents, "EXTRACTORS", table)


def _ingest(path: Path) -> tuple[int, int, int, bool, bool]:
    return ingest_documents.ingest_file(path, mcp_url="http://x", headers={}, dry_run=False)


# ---------------------------------------------------------------------------
# (a1) extractor raises an I/O error → transient (retry), nothing posted
# ---------------------------------------------------------------------------


def test_extractor_io_error_is_transient_and_posts_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture_post: _PostCapture,
    downloaded: None,
) -> None:
    """An :class:`OSError` from the extractor (file vanished, read error
    mid-sync) is a *transient* failure: retry next run, hold the watermark, and
    never reach the emit — a half-extracted doc must not be indexed."""

    def boom(_path: Path) -> str:
        raise OSError("read failed mid-sync")

    _set_extractor(monkeypatch, ".pdf", boom)
    f = tmp_path / "report.pdf"
    f.write_bytes(b"%PDF-1.4 not really")

    ok, skipped, failed, transient, unreadable = _ingest(f)

    assert (ok, skipped, failed) == (0, 0, 0)
    assert transient is True
    assert unreadable is False
    assert capture_post.called is False


# ---------------------------------------------------------------------------
# (a2) extractor raises a parse error → unreadable (don't block), not transient
# ---------------------------------------------------------------------------


def test_extractor_parse_error_is_unreadable_not_transient(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture_post: _PostCapture,
    downloaded: None,
) -> None:
    """A non-:class:`OSError` extractor exception (encrypted/corrupt PDF, a
    parser assertion) is *deterministically unreadable*: retrying never helps,
    so it must NOT be transient (it would pin the watermark forever and re-walk
    every file nightly) — it is flagged ``unreadable`` instead, still posting
    nothing."""

    def boom(_path: Path) -> str:
        # Stands in for pdfplumber's ``PdfminerException(PDFPasswordIncorrect())``
        # — any parse/format error class, not an OSError.
        raise RuntimeError("pdf parser blew up")

    _set_extractor(monkeypatch, ".pdf", boom)
    f = tmp_path / "report.pdf"
    f.write_bytes(b"%PDF-1.4 not really")

    ok, skipped, failed, transient, unreadable = _ingest(f)

    assert (ok, skipped, failed) == (0, 0, 0)
    assert transient is False
    assert unreadable is True
    assert capture_post.called is False


# ---------------------------------------------------------------------------
# (b) oversized file → clean empty result, NOT transient
# ---------------------------------------------------------------------------


def test_oversized_file_skipped_not_transient(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture_post: _PostCapture,
    downloaded: None,
) -> None:
    """A file over ``MAX_FILE_MB`` is a *clean* skip — it returns (0,0,0,False) so
    the walker is free to advance the watermark past it (retrying never helps)."""
    monkeypatch.setattr(ingest_documents, "MAX_FILE_MB", 0.0001)  # ~104 bytes
    # The extractor must never run for an oversized file; make it fail loudly if
    # the size gate is bypassed.
    _set_extractor(
        monkeypatch,
        ".txt",
        lambda _p: pytest.fail("extractor ran on an oversized file"),
    )
    f = tmp_path / "huge.txt"
    f.write_text("x" * 4096)

    ok, skipped, failed, transient, unreadable = _ingest(f)

    assert (ok, skipped, failed, transient, unreadable) == (0, 0, 0, False, False)
    assert capture_post.called is False


# ---------------------------------------------------------------------------
# (c) too-short extracted text → skip, NOT transient
# ---------------------------------------------------------------------------


def test_short_text_skipped_not_transient(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture_post: _PostCapture,
    downloaded: None,
) -> None:
    """Under 50 chars of extracted text carries no retrievable meaning: skip it
    cleanly (non-transient), and never emit."""
    _set_extractor(monkeypatch, ".txt", lambda _p: "too short")
    f = tmp_path / "tiny.txt"
    f.write_text("too short")

    ok, skipped, failed, transient, unreadable = _ingest(f)

    assert (ok, skipped, failed, transient, unreadable) == (0, 0, 0, False, False)
    assert capture_post.called is False


# ---------------------------------------------------------------------------
# (d) unsupported extension → extractor never invoked
# ---------------------------------------------------------------------------


def test_unsupported_extension_never_extracts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture_post: _PostCapture,
    downloaded: None,
) -> None:
    """An extension with no registered extractor returns immediately, before the
    download wait or any extraction — and certainly before any emit."""
    # ``ensure_downloaded`` must NOT be consulted for an unsupported type: the
    # early ``EXTRACTORS.get`` miss returns first.
    monkeypatch.setattr(
        ingest_documents,
        "ensure_downloaded",
        lambda *a, **k: pytest.fail("ensure_downloaded called for unsupported ext"),
    )
    f = tmp_path / "diagram.xyz"
    f.write_text("some content that would otherwise be long enough to ingest")

    ok, skipped, failed, transient, unreadable = _ingest(f)

    assert (ok, skipped, failed, transient, unreadable) == (0, 0, 0, False, False)
    assert capture_post.called is False


# ---------------------------------------------------------------------------
# (e) PII + machine-secret scrubbing before emit
# ---------------------------------------------------------------------------


def test_pii_and_secrets_scrubbed_before_emit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture_post: _PostCapture,
    downloaded: None,
) -> None:
    """Text carrying an email and an AWS-shaped key must be scrubbed by
    ``redact_code_secrets`` + ``filter_pii`` before a single chunk leaves the
    machine, and the emitted meta must flag ``pii_filtered=True`` so the server
    can skip duplicate work."""
    email = "jane.doe@example.com"
    aws_key = "AKIA" + "A" * 16
    # Pad past the 50-char floor and the chunker's min_size so a chunk is emitted.
    body = (
        f"Reach me at {email} for the audit. "
        f"The deploy key leaked into this note: {aws_key}. "
        "Please rotate it before the quarterly review next month."
    )
    _set_extractor(monkeypatch, ".txt", lambda _p: body)
    f = tmp_path / "leak.txt"
    f.write_text(body)

    ok, skipped, failed, transient, unreadable = _ingest(f)

    assert transient is False
    assert unreadable is False
    assert capture_post.called is True
    assert capture_post.chunks, "expected at least one chunk emitted"

    emitted = "\n".join(capture_post.chunks)
    assert email not in emitted, "raw email reached the emit — filter_pii did not run"
    assert aws_key not in emitted, "raw AWS key reached the emit — redact_code_secrets did not run"
    # The redaction markers prove the scrub happened (not merely that the raw
    # values are absent because the text was mangled some other way).
    assert "[REDACTED:EMAIL]" in emitted
    assert "[REDACTED:AWS_KEY]" in emitted

    assert capture_post.kwargs is not None
    assert capture_post.kwargs["meta"] == {"pii_filtered": True}
    # source_id / url anchor on the absolute path so the server can replace
    # stale chunks when the file is edited in place.
    assert capture_post.kwargs["url"] == str(f)


# ---------------------------------------------------------------------------
# extract_document_date — filename parsing + validity bounds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stem", "expected"),
    [
        # Leading YYYY-MM short-circuits to day 1.
        ("2024-03 Blood test results", "2024-03-01"),
        # Full YYYY-MM-DD with each supported separator.
        ("2024-03-15 scan", "2024-03-15"),
        ("2024_03_15 underscore", "2024-03-15"),
        ("2024 03 15 spaces", "2024-03-15"),
    ],
)
def test_extract_document_date_parses_filename(tmp_path: Path, stem: str, expected: str) -> None:
    """A leading ISO date in the filename is parsed to an ISO date string."""
    f = tmp_path / f"{stem}.pdf"
    f.write_text("x")
    assert ingest_documents.extract_document_date(f) == expected


@pytest.mark.parametrize(
    "stem",
    [
        "1850-03 too-old",  # year below 1990 lower bound
        "2200-03 too-new",  # year above 2100 upper bound
        "2024-13 bad-month",  # month out of 1..12
        "2024-02-30 bad-day",  # 30 Feb — caught by the datetime() ValueError
        "untitled notes",  # no leading date at all
    ],
)
def test_extract_document_date_out_of_bounds_falls_back_to_mtime(tmp_path: Path, stem: str) -> None:
    """A filename date outside the validity bounds (or absent) is ignored and the
    file mtime is used instead — pinned deterministically via ``os.utime``."""
    import os
    from datetime import datetime, timezone

    f = tmp_path / f"{stem}.pdf"
    f.write_text("x")
    # Fixed mtime: 2021-07-04T00:00:00Z.
    mtime = datetime(2021, 7, 4, tzinfo=timezone.utc).timestamp()
    os.utime(f, (mtime, mtime))

    assert ingest_documents.extract_document_date(f) == "2021-07-04"

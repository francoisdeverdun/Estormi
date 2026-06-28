"""Tests for ingestion shared modules: chunker, pii_filter, watermark."""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from estormi_ingestion.shared.chunker import paragraph_chunks, sliding_chunks
from memory_core.pii_filter import filter_pii, is_otp_message

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# ── sliding_chunks ──────────────────────────────────────────────────────────


class TestSlidingChunks:
    pytestmark = pytest.mark.unit

    def test_empty_string(self):
        assert sliding_chunks("") == []

    def test_none_treated_as_empty(self):
        assert sliding_chunks(None) == []

    def test_whitespace_only(self):
        assert sliding_chunks("   \n\t  ") == []

    def test_short_text_single_chunk(self):
        text = "Hello, world!"
        result = sliding_chunks(text, size=800)
        assert result == [text]

    def test_exact_size_single_chunk(self):
        text = "a" * 800
        result = sliding_chunks(text, size=800)
        assert result == [text]

    def test_larger_text_produces_overlapping_chunks(self):
        text = "a" * 1600
        result = sliding_chunks(text, size=800, overlap=100)
        assert len(result) >= 2
        # All chunks should be at most 800 chars
        for chunk in result:
            assert len(chunk) <= 800

    def test_overlap_creates_shared_content(self):
        # Create text where we can verify overlap
        text = "AAAA" * 200 + "BBBB" * 200  # 1600 chars
        result = sliding_chunks(text, size=800, overlap=100)
        assert len(result) >= 2
        # Last 100 chars of first chunk should appear in start of second chunk
        end_of_first = result[0][-100:]
        assert end_of_first in result[1]

    def test_custom_size(self):
        text = "word " * 100  # 500 chars
        result = sliding_chunks(text, size=100, overlap=20)
        assert len(result) >= 5
        for chunk in result:
            assert len(chunk) <= 100

    def test_overlap_must_be_smaller_than_size(self):
        # Before the boundary check, ``overlap >= size`` collapsed the step
        # to 1 and silently emitted near-duplicate chunks. The chunker now
        # rejects the misconfiguration at the boundary — production callers
        # have no business asking for a window that doesn't slide.
        with pytest.raises(ValueError):
            sliding_chunks("ab", size=1, overlap=10)
        with pytest.raises(ValueError):
            sliding_chunks("anything", size=100, overlap=100)


# ── paragraph_chunks ─────────────────────────────────────────────────────────


class TestParagraphChunks:
    pytestmark = pytest.mark.unit

    def test_empty_and_none(self):
        assert paragraph_chunks("") == []
        assert paragraph_chunks(None) == []
        assert paragraph_chunks("   \n\n  \t ") == []

    def test_distinct_paragraphs_stay_separate(self):
        # The core fix: two unrelated subjects that happen to share a word
        # ("août") must NOT land in the same chunk, or correlation scores
        # them identically and the briefing fuses them into one story.
        text = (
            "Saint-Jacut en août : on réserve la maison du 5 au 12 août.\n\n"
            "Projet boulot : la migration doit finir avant la réunion d'août."
        )
        result = paragraph_chunks(text, max_size=800, min_size=20)
        assert len(result) == 2
        assert "Saint-Jacut" in result[0]
        assert "Saint-Jacut" not in result[1]
        assert "migration" in result[1]

    def test_short_fragments_dropped(self):
        text = "tiny\n\n" + "This paragraph is comfortably long enough to keep."
        result = paragraph_chunks(text, max_size=800, min_size=20)
        assert result == ["This paragraph is comfortably long enough to keep."]

    def test_whole_short_text_never_dropped(self):
        # A terse note below min_size must survive rather than vanish.
        assert paragraph_chunks("ok", min_size=80) == ["ok"]

    def test_long_paragraph_split_by_sentence(self):
        sentences = " ".join(f"Sentence number {i} here." for i in range(60))
        result = paragraph_chunks(sentences, max_size=200, min_size=20)
        assert len(result) > 1
        for chunk in result:
            assert len(chunk) <= 200

    def test_oversized_sentence_falls_back_to_window(self):
        text = "x" * 2000  # one "sentence", no boundaries
        result = paragraph_chunks(text, max_size=500, min_size=20)
        assert len(result) > 1
        for chunk in result:
            assert len(chunk) <= 500


# ── filter_pii ──────────────────────────────────────────────────────────────


class TestFilterPii:
    pytestmark = pytest.mark.unit

    def test_clean_text_unchanged(self):
        text = "Met Alice at the café."
        assert filter_pii(text) == text

    def test_redacts_email(self):
        text = "Contact me at alice@example.com please."
        result = filter_pii(text)
        assert "alice@example.com" not in result
        assert "[REDACTED:EMAIL]" in result

    def test_redacts_french_phone(self):
        text = "Appelle-moi au 06 12 34 56 78"
        result = filter_pii(text)
        assert "06 12 34 56 78" not in result
        assert "[REDACTED:PHONE_FR]" in result

    def test_redacts_french_phone_with_intl_prefix(self):
        text = "Call +33 6 12 34 56 78"
        result = filter_pii(text)
        assert "[REDACTED:PHONE_FR]" in result

    def test_redacts_international_phone_with_bare_label(self):
        # A non-French number takes the looser international branch and the bare
        # ``[REDACTED:PHONE]`` label (reserved +1 555 range, never a real line).
        result = filter_pii("call +1 555 010 1234 tomorrow")
        assert "+1 555 010 1234" not in result
        assert "[REDACTED:PHONE]" in result
        assert "[REDACTED:PHONE_FR]" not in result

    def test_international_phone_does_not_redact_iso_timestamp(self):
        # "+1 2026-05-25T12:34:56" matches the loose phone shape greedily; the
        # YYYY-MM-DD guard must keep the timestamp intact (no over-redaction).
        text = "event at +1 2026-05-25T12:34:56 logged"
        assert filter_pii(text) == text

    def test_international_phone_too_short_unchanged(self):
        # Matches the international pattern but carries only 6 digits (< 8), so
        # the digit-count floor rejects it.
        text = "ref +1 5 55 01 here"
        assert filter_pii(text) == text

    def test_international_phone_too_long_unchanged(self):
        # 16 digits (> 15) overshoots the longest real E.164 number → rejected.
        text = "ref +12 3456 7890 1234 567 here"
        assert filter_pii(text) == text

    def test_redacts_iban(self):
        text = "IBAN: FR7630006000011234567890189"
        result = filter_pii(text)
        assert "FR7630006000011234567890189" not in result
        assert "[REDACTED:FRENCH_IBAN]" in result

    def test_redacts_password(self):
        text = "password: mySecret123"
        result = filter_pii(text)
        assert "mySecret123" not in result
        assert "[REDACTED:PASSWORD_LIKE]" in result

    def test_redacts_french_password(self):
        text = "mot de passe: secret"
        result = filter_pii(text)
        assert "[REDACTED:PASSWORD_LIKE]" in result

    def test_redacts_otp_inline(self):
        # OTP regex: digits followed by lookahead for code/otp/etc keywords
        text = "Your code 123456 is for OTP verification"
        result = filter_pii(text)
        assert "[REDACTED:OTP_CODE]" in result

    def test_redacts_otp_with_prefix_pattern(self):
        # Second alternative: "verification code:" prefix then digits
        text = "verification code: 123456"
        result = filter_pii(text)
        assert "[REDACTED:OTP_CODE]" in result

    # ── OTP false-positive guards (negative regressions) ──────────────────
    # The OTP regex once used an unbounded ``.*`` lookahead that swallowed
    # plain years, postal codes, and order numbers sitting near a trigger
    # word — silently erasing real memory. These pin the bounded rewrite:
    # loosen it back toward ``.*`` and one of these flips to a redaction.
    def test_does_not_redact_bare_year(self):
        text = "L'année 2023 fut une bonne année."
        result = filter_pii(text)
        assert "2023" in result
        assert "[REDACTED:OTP_CODE]" not in result

    def test_does_not_redact_postal_code_near_code_keyword(self):
        # "code postal" is the canonical trap: the trigger word "code" sits
        # right next to a 5-digit postal code that must survive.
        text = "Mon code postal 75008 est correct."
        result = filter_pii(text)
        assert "75008" in result
        assert "[REDACTED:OTP_CODE]" not in result

    def test_does_not_redact_long_order_or_transaction_number(self):
        # A 9–10 digit order/transaction id is longer than an OTP and carries
        # no trigger word — it must pass through untouched.
        text = "Commande 1234567890 expédiée; référence transaction 99887766 traitée."
        result = filter_pii(text)
        assert "1234567890" in result
        assert "99887766" in result
        assert "[REDACTED:OTP_CODE]" not in result

    def test_redact_false_removes_instead(self):
        text = "Email alice@test.com here"
        result = filter_pii(text, redact=False)
        assert "alice@test.com" not in result
        assert "[REDACTED" not in result

    def test_multiple_pii_types(self):
        text = "alice@test.com called 06 12 34 56 78 password: abc"
        result = filter_pii(text)
        assert "REDACTED:EMAIL" in result
        assert "REDACTED:PHONE_FR" in result
        assert "REDACTED:PASSWORD_LIKE" in result

    def test_french_phone_with_leading_zero_after_country_code(self):
        # Users commonly type "+33 06 12 34 56 78" — the old pattern required
        # the country digit immediately after +33 and missed this form.
        result = filter_pii("appelle-moi au +33 06 12 34 56 78 demain")
        assert "REDACTED:PHONE_FR" in result
        assert "06 12 34 56 78" not in result

    def test_iban_tolerates_internal_spaces(self):
        # Real-world IBANs are formatted with spaces every 4 chars.
        result = filter_pii("IBAN: FR76 3000 4000 5000 6000 7000 123")
        assert "REDACTED:FRENCH_IBAN" in result
        assert "3000" not in result

    def test_redacts_real_social_security(self):
        # A genuine 15-digit NIR (gender-year-month-dept-…) is still redacted.
        result = filter_pii("Mon numéro de sécu : 1 84 12 75 116 001 42")
        assert "REDACTED:SOCIAL_SECURITY" in result
        assert "116 001" not in result

    def test_whatsapp_mention_not_taken_for_social_security(self):
        # A WhatsApp @mention is the addressee's JID user-part: a 15-digit @lid
        # handle starting with 1 collides with the NIR shape. The @-guard must
        # leave it intact so the briefing keeps who a message was addressed to.
        result = filter_pii("@100000000000002 du coup, tu viens ou pas ?")
        assert "REDACTED:SOCIAL_SECURITY" not in result
        assert "@100000000000002" in result

    def test_password_marker_not_eaten_on_second_pass(self):
        # On the fixed-point loop, the password regex must not re-match
        # ``password: [REDACTED:PHONE]`` and relabel it ``[REDACTED:PASSWORD_LIKE]``,
        # losing the precise PII type that was identified first.
        result = filter_pii("le mdp: hunter2; tel: +33 6 11 22 33 44")
        assert "REDACTED:PASSWORD_LIKE" in result
        assert "REDACTED:PHONE_FR" in result

    def test_phone_lookarounds_block_mid_digit_run(self):
        # Long numeric IDs starting with + must not be matched as phones.
        text = "transaction-id: +1234567890123456789012345 done"
        result = filter_pii(text)
        # The original substring should still be present — no redaction.
        assert "+1234567890123456789012345" in result


# ── is_otp_message ──────────────────────────────────────────────────────────


class TestIsOtpMessage:
    pytestmark = pytest.mark.unit

    def test_verification_code_english(self):
        assert is_otp_message("Your verification code is 123456") is True

    def test_verification_code_french(self):
        assert is_otp_message("Votre code de vérification est 789012") is True

    def test_otp_message(self):
        assert is_otp_message("654321 is your OTP for login") is True

    def test_do_not_share(self):
        assert is_otp_message("Code: 1234. Do not share this with anyone.") is True

    def test_ne_pas_partager(self):
        assert is_otp_message("Code: 5678. Ne pas partager ce code.") is True

    def test_expires_in(self):
        assert is_otp_message("Your code is 1234. Expires in 5 minutes.") is True

    def test_normal_message_not_otp(self):
        assert is_otp_message("Hey, let's meet at 3pm tomorrow!") is False

    def test_empty_string(self):
        assert is_otp_message("") is False

    def test_number_without_otp_context(self):
        assert is_otp_message("I bought 123456 items") is False


# ── watermark ───────────────────────────────────────────────────────────────


class TestWatermark:
    pytestmark = pytest.mark.integration

    async def test_get_watermark_first_run(self, db_on_disk):
        conn, db_path = db_on_disk
        await conn.close()  # Close so watermark can open its own connection

        with patch("estormi_ingestion.shared.watermark.estormi_db_path", return_value=db_path):
            from estormi_ingestion.shared.watermark import get_watermark

            fetched_at, item_id = await get_watermark("imessage")
            assert fetched_at is None
            assert item_id is None

    async def test_set_and_get_watermark(self, db_on_disk):
        conn, db_path = db_on_disk
        await conn.close()

        with patch("estormi_ingestion.shared.watermark.estormi_db_path", return_value=db_path):
            from estormi_ingestion.shared.watermark import get_watermark, set_watermark

            await set_watermark("imessage", "2024-06-15T10:00:00Z", "msg-999")
            fetched_at, item_id = await get_watermark("imessage")
            assert fetched_at == "2024-06-15T10:00:00Z"
            assert item_id == "msg-999"

    async def test_set_watermark_upserts(self, db_on_disk):
        conn, db_path = db_on_disk
        await conn.close()

        with patch("estormi_ingestion.shared.watermark.estormi_db_path", return_value=db_path):
            from estormi_ingestion.shared.watermark import get_watermark, set_watermark

            await set_watermark("cal", "2024-01-01", "a")
            await set_watermark("cal", "2024-06-01", "b")
            fetched_at, item_id = await get_watermark("cal")
            assert fetched_at == "2024-06-01"
            assert item_id == "b"


# ── forward clock-skew watermark guard (sweep 2 U6) ──────────────────────────
#
# A run whose system clock is ahead of real time stores a FUTURE watermark.
# After the clock corrects, every file's mtime is <= that future watermark, so
# the walker permanently skips all files. The fix resets last_run to None when
# the stored watermark lies strictly in the future relative to walk_started_at,
# turning the run into a full rescan. Content-hash dedup on the server keeps the
# rescan idempotent.


def _make_temp_doc_dir() -> tuple[Path, Path]:
    """Return (tmp_dir, sample_file) with a plain-text file inside."""
    tmp = Path(tempfile.mkdtemp(prefix="estormi-wm-test-"))
    sample = tmp / "note.txt"
    sample.write_text("This is a sufficiently long document body for ingest testing." * 3)
    return tmp, sample


def _future_ts() -> str:
    """ISO timestamp in year 2099 — always in the future."""
    return "2099-01-01T00:00:00+00:00"


def _past_ts() -> str:
    """ISO timestamp safely in the past (1 year ago)."""
    return (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()


class TestWatermarkSkewGuardLogic:
    """The skew-guard predicate ``shared.watermark.is_future_watermark`` (the
    REAL predicate both walkers call): last_run is reset to None iff it is
    strictly in the future, so a regression in production logic fails here."""

    pytestmark = pytest.mark.unit

    @staticmethod
    def _apply_guard(last_run, walk_started_at):
        from estormi_ingestion.shared.watermark import is_future_watermark

        return None if is_future_watermark(last_run, walk_started_at) else last_run

    def test_future_watermark_resets_to_none(self):
        now = datetime.now(timezone.utc)
        future = now + timedelta(days=365 * 73)  # year ~2099
        assert self._apply_guard(future, now) is None

    def test_past_watermark_unchanged(self):
        now = datetime.now(timezone.utc)
        past = now - timedelta(days=1)
        assert self._apply_guard(past, now) == past

    def test_equal_watermark_unchanged(self):
        now = datetime.now(timezone.utc)
        assert self._apply_guard(now, now) == now

    def test_none_watermark_unchanged(self):
        now = datetime.now(timezone.utc)
        assert self._apply_guard(None, now) is None

    def test_one_second_future_resets(self):
        now = datetime.now(timezone.utc)
        assert self._apply_guard(now + timedelta(seconds=1), now) is None


class TestDocumentsWalkerWatermarkSkew:
    """Drive ``ingest_documents.main()`` with a mocked watermark + HTTP layer:
    a FUTURE / PAST / absent watermark must all examine the file (never skip)."""

    pytestmark = pytest.mark.integration

    def _run_docs_main(self, watermark_ts, tmp_dir: Path) -> list[str]:
        import importlib.util as _ilu

        posted_urls: list[str] = []

        def fake_post(url: str, payload: dict, **kwargs):
            if "/ingest_chunk" in url:
                posted_urls.append(url)
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"status": "ok"}
            return resp

        spec_path = str(
            _REPO_ROOT / "packages" / "estormi_ingestion" / "documents" / "ingest_documents.py"
        )
        spec = _ilu.spec_from_file_location(
            "estormi_ingestion.documents.ingest_documents", spec_path
        )
        mod = _ilu.module_from_spec(spec)

        # ensure_downloaded shells out to brctl + xattr and sleeps; mock it to
        # return True immediately so the test doesn't wait 120s.
        _ensure_downloaded_true = MagicMock(return_value=True)

        with (
            patch.dict(
                "sys.modules",
                {
                    "pdfplumber": MagicMock(),
                    "docx": MagicMock(),
                    "odf": MagicMock(),
                    "odf.opendocument": MagicMock(),
                    "odf.text": MagicMock(),
                    "pptx": MagicMock(),
                    "openpyxl": MagicMock(),
                    "striprtf": MagicMock(),
                    "striprtf.striprtf": MagicMock(),
                },
            ),
            patch("sys.argv", ["ingest_documents.py", "--root", str(tmp_dir)]),
            patch(
                "estormi_ingestion.shared.watermark.get_watermark",
                new_callable=AsyncMock,
                return_value=(watermark_ts, None),
            ),
            patch("estormi_ingestion.shared.watermark.set_watermark", new_callable=AsyncMock),
            patch("estormi_ingestion.shared.http_client.post_chunk", side_effect=fake_post),
            # Skip iCloud-specific subprocess calls (batch brctl loop).
            patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=b"")),
        ):
            spec.loader.exec_module(mod)
            mod.ensure_downloaded = _ensure_downloaded_true
            try:
                mod.main()
            except SystemExit:
                pass

        return posted_urls

    def test_future_watermark_does_not_skip_file(self):
        import shutil

        tmp_dir, _ = _make_temp_doc_dir()
        try:
            posted = self._run_docs_main(_future_ts(), tmp_dir)
            assert len(posted) >= 1, (
                "Expected at least one POST to /ingest_chunk — file silently skipped "
                "despite future watermark (bug U6 not fixed)"
            )
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_past_watermark_does_not_skip_file(self):
        import shutil

        tmp_dir, _ = _make_temp_doc_dir()
        try:
            posted = self._run_docs_main(_past_ts(), tmp_dir)
            assert len(posted) >= 1, (
                "Expected at least one POST — file skipped with a past watermark"
            )
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_no_watermark_ingests_file(self):
        import shutil

        tmp_dir, _ = _make_temp_doc_dir()
        try:
            posted = self._run_docs_main(None, tmp_dir)
            assert len(posted) >= 1, "Expected at least one POST on first run (no watermark)"
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

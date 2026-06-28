"""Knowledge fetch layer — yt-dlp, VTT, RSS, transcripts."""

from __future__ import annotations

import json
import sys
import types
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from estormi_ingestion.knowledge.knowledge_fetch import (
    _strip_vtt,
    _yt_dlp_bin,
    download_transcript,
    fetch_recent_videos,
    fetch_rss_source,
)

pytestmark = pytest.mark.unit

# ── knowledge_fetch: _yt_dlp_bin ─────────────────────────────────────────────


def test_yt_dlp_bin_finds_venv_binary(tmp_path):
    """Prefers the binary co-located with the current interpreter."""
    fake_bin = tmp_path / "yt-dlp"
    fake_bin.touch(mode=0o755)

    with (
        patch("estormi_ingestion.knowledge.knowledge_fetch.sys") as mock_sys,
        patch("estormi_ingestion.knowledge.knowledge_fetch.shutil.which", return_value=None),
    ):
        mock_sys.executable = str(tmp_path / "python")
        result = _yt_dlp_bin()

    assert result == str(fake_bin)


def test_yt_dlp_bin_falls_back_to_which():
    with (
        patch("estormi_ingestion.knowledge.knowledge_fetch.sys") as mock_sys,
        patch(
            "estormi_ingestion.knowledge.knowledge_fetch.shutil.which",
            return_value="/usr/local/bin/yt-dlp",
        ),
    ):
        mock_sys.executable = "/nonexistent/python"
        result = _yt_dlp_bin()

    assert result == "/usr/local/bin/yt-dlp"


def test_yt_dlp_bin_raises_when_missing():
    with (
        patch("estormi_ingestion.knowledge.knowledge_fetch.sys") as mock_sys,
        patch("estormi_ingestion.knowledge.knowledge_fetch.shutil.which", return_value=None),
    ):
        mock_sys.executable = "/nonexistent/python"
        with pytest.raises(FileNotFoundError, match="yt-dlp not found"):
            _yt_dlp_bin()


# ── knowledge_fetch: _strip_vtt ──────────────────────────────────────────────


def test_strip_vtt_removes_timestamps(tmp_path):
    vtt = tmp_path / "test.vtt"
    vtt.write_text(
        "WEBVTT\n\n"
        "00:00:01.000 --> 00:00:03.000\n"
        "Hello world\n\n"
        "00:00:03.500 --> 00:00:05.000\n"
        "Hello world\n\n"  # duplicate — should be deduped
        "00:00:05.000 --> 00:00:07.000\n"
        "<00:05.000><c>More text</c>\n",
        encoding="utf-8",
    )
    result = _strip_vtt(vtt)
    assert "Hello world" in result
    assert "More text" in result
    assert "00:00:01" not in result
    assert result.count("Hello world") == 1


def test_strip_vtt_skips_webvtt_header(tmp_path):
    vtt = tmp_path / "test.vtt"
    vtt.write_text(
        "WEBVTT\nKind: captions\nLanguage: fr\n\n00:00:01.000 --> 00:00:02.000\nBonjour\n"
    )
    result = _strip_vtt(vtt)
    assert "WEBVTT" not in result
    assert "Kind" not in result
    assert "Bonjour" in result


def test_strip_vtt_empty_file(tmp_path):
    vtt = tmp_path / "empty.vtt"
    vtt.write_text("WEBVTT\n\n")
    assert _strip_vtt(vtt) == ""


# ── knowledge_fetch: fetch_recent_videos ─────────────────────────────────────


def test_fetch_recent_videos_passes_dateafter():
    """Verifies yt-dlp is called with --dateafter yesterday."""
    captured_args = []

    def fake_run(args, **kwargs):
        captured_args.extend(args)
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("estormi_ingestion.knowledge.knowledge_fetch.subprocess.run", side_effect=fake_run),
        patch(
            "estormi_ingestion.knowledge.knowledge_fetch._yt_dlp_bin", return_value="/fake/yt-dlp"
        ),
    ):
        fetch_recent_videos({"id": "x", "url": "https://y", "subtitle_langs": ["fr"]})

    assert "--skip-download" in captured_args
    assert "--playlist-end" in captured_args
    assert "--dateafter" in captured_args


def test_fetch_recent_videos_lookback_days():
    """lookback_days widens the dateafter cutoff and increases playlist-end."""

    captured_args = []

    def fake_run(args, **kwargs):
        captured_args.extend(args)
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("estormi_ingestion.knowledge.knowledge_fetch.subprocess.run", side_effect=fake_run),
        patch(
            "estormi_ingestion.knowledge.knowledge_fetch._yt_dlp_bin", return_value="/fake/yt-dlp"
        ),
        patch("estormi_ingestion.knowledge.knowledge_fetch.date") as mock_date,
    ):
        mock_date.today.return_value = date(2026, 5, 5)
        fetch_recent_videos(
            {"id": "x", "url": "https://y", "subtitle_langs": ["fr"]}, lookback_days=14
        )

    dateafter_idx = captured_args.index("--dateafter")
    assert captured_args[dateafter_idx + 1] == "20260421"  # 14 days before 2026-05-05
    playlist_end_idx = captured_args.index("--playlist-end")
    assert int(captured_args[playlist_end_idx + 1]) >= 14 * 3


def test_fetch_recent_videos_parses_output():
    fake_stdout = (
        json.dumps({"id": "abc123", "title": "Test video", "upload_date": "20260502"}) + "\n"
    )
    mock_result = MagicMock(returncode=0, stdout=fake_stdout, stderr="")

    with (
        patch(
            "estormi_ingestion.knowledge.knowledge_fetch.subprocess.run", return_value=mock_result
        ),
        patch(
            "estormi_ingestion.knowledge.knowledge_fetch._yt_dlp_bin", return_value="/fake/yt-dlp"
        ),
    ):
        videos = fetch_recent_videos(
            {"id": "test_ch", "url": "https://y", "subtitle_langs": ["fr"]}
        )

    assert len(videos) == 1
    assert videos[0]["id"] == "abc123"
    assert videos[0]["title"] == "Test video"


def test_fetch_recent_videos_skips_malformed_json():
    fake_stdout = (
        "not-json\n" + json.dumps({"id": "good1", "title": "G", "upload_date": "20260502"}) + "\n"
    )

    with (
        patch(
            "estormi_ingestion.knowledge.knowledge_fetch.subprocess.run",
            return_value=MagicMock(returncode=0, stdout=fake_stdout, stderr=""),
        ),
        patch(
            "estormi_ingestion.knowledge.knowledge_fetch._yt_dlp_bin", return_value="/fake/yt-dlp"
        ),
    ):
        videos = fetch_recent_videos({"id": "x", "url": "https://y", "subtitle_langs": ["fr"]})

    assert len(videos) == 1
    assert videos[0]["id"] == "good1"


def test_fetch_recent_videos_uses_json_safe_template():
    """The --print template must use yt-dlp's `j` (JSON-encode) field modifier.

    With the bare `s` modifier a title containing a double-quote or backslash
    produced invalid JSON that json.loads silently dropped, so that video was
    never ingested. The `j` modifier emits a JSON-escaped value (quotes
    included), keeping the line valid.
    """
    captured_args = []

    def fake_run(args, **kwargs):
        captured_args.extend(args)
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("estormi_ingestion.knowledge.knowledge_fetch.subprocess.run", side_effect=fake_run),
        patch(
            "estormi_ingestion.knowledge.knowledge_fetch._yt_dlp_bin", return_value="/fake/yt-dlp"
        ),
    ):
        fetch_recent_videos({"id": "x", "url": "https://y", "subtitle_langs": ["fr"]})

    print_idx = captured_args.index("--print")
    template = captured_args[print_idx + 1]
    assert "%(title)j" in template
    assert "%(id)j" in template
    # The old, broken template manually quoted a bare `s` value.
    assert '"%(title)s"' not in template


def test_fetch_recent_videos_parses_hostile_title():
    """A title with embedded quotes/backslashes, JSON-escaped by yt-dlp's `j`
    modifier, must still parse and yield the video — not be silently dropped."""
    hostile_title = 'Watch "this" \\ now'
    # This is what yt-dlp's %(title)j emits: a properly JSON-escaped string.
    fake_stdout = (
        json.dumps({"id": "vid42", "title": hostile_title, "upload_date": "20260502"}) + "\n"
    )

    with (
        patch(
            "estormi_ingestion.knowledge.knowledge_fetch.subprocess.run",
            return_value=MagicMock(returncode=0, stdout=fake_stdout, stderr=""),
        ),
        patch(
            "estormi_ingestion.knowledge.knowledge_fetch._yt_dlp_bin", return_value="/fake/yt-dlp"
        ),
    ):
        videos = fetch_recent_videos({"id": "x", "url": "https://y", "subtitle_langs": ["fr"]})

    assert len(videos) == 1
    assert videos[0]["id"] == "vid42"
    assert videos[0]["title"] == hostile_title


def test_fetch_recent_videos_tolerates_yt_dlp_error():
    """Non-zero exit from yt-dlp should log a warning but not raise."""
    with (
        patch(
            "estormi_ingestion.knowledge.knowledge_fetch.subprocess.run",
            return_value=MagicMock(returncode=1, stdout="", stderr="rate limited"),
        ),
        patch(
            "estormi_ingestion.knowledge.knowledge_fetch._yt_dlp_bin", return_value="/fake/yt-dlp"
        ),
    ):
        videos = fetch_recent_videos({"id": "x", "url": "https://y", "subtitle_langs": ["fr"]})

    assert videos == []


def test_fetch_recent_videos_terminates_options_before_url():
    """Arg-injection guard: ``--`` must sit immediately before the URL.

    yt-dlp treats anything starting with ``-`` as a flag. A hostile source URL
    such as ``--config-location=/etc/passwd`` would be parsed as an option
    unless option parsing is explicitly terminated. The ``--`` terminator
    forces every following argument (here, the single URL) to be a positional.
    This test fails if the terminator is dropped or moved away from the URL.
    """
    captured_args: list[str] = []
    hostile_url = "--config-location=/etc/evil"

    def fake_run(args, **kwargs):
        captured_args.extend(args)
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("estormi_ingestion.knowledge.knowledge_fetch.subprocess.run", side_effect=fake_run),
        patch(
            "estormi_ingestion.knowledge.knowledge_fetch._yt_dlp_bin", return_value="/fake/yt-dlp"
        ),
    ):
        fetch_recent_videos({"id": "x", "url": hostile_url, "subtitle_langs": ["fr"]})

    # The hostile URL is the final argument, and the element right before it
    # must be the bare option terminator.
    assert captured_args[-1] == hostile_url
    assert captured_args[-2] == "--"
    # And nothing after the terminator may be interpreted as a flag — only the
    # single positional URL follows it.
    term_idx = captured_args.index("--")
    assert captured_args[term_idx + 1 :] == [hostile_url]


# ── knowledge_fetch: download_transcript ─────────────────────────────────────


def test_download_transcript_returns_none_when_no_vtt(tmp_path):
    with (
        patch(
            "estormi_ingestion.knowledge.knowledge_fetch.subprocess.run",
            return_value=MagicMock(returncode=0),
        ),
        patch(
            "estormi_ingestion.knowledge.knowledge_fetch._yt_dlp_bin", return_value="/fake/yt-dlp"
        ),
        patch("estormi_ingestion.knowledge.knowledge_fetch._RUN_TMP_DIR", tmp_path),
    ):
        result = download_transcript("missing_video", ["fr", "en"])

    assert result is None


def test_download_transcript_tries_all_langs_before_giving_up(tmp_path):
    """yt-dlp is called for each language in order until one succeeds."""
    call_count = []

    def fake_run(args, **kwargs):
        call_count.append(args[args.index("--sub-lang") + 1])
        return MagicMock(returncode=0)

    with (
        patch("estormi_ingestion.knowledge.knowledge_fetch.subprocess.run", side_effect=fake_run),
        patch(
            "estormi_ingestion.knowledge.knowledge_fetch._yt_dlp_bin", return_value="/fake/yt-dlp"
        ),
        patch("estormi_ingestion.knowledge.knowledge_fetch._RUN_TMP_DIR", tmp_path),
    ):
        result = download_transcript("vid123", ["fr", "en"])

    assert result is None  # no VTT written → None
    assert call_count == ["fr", "en"]


def test_download_transcript_reads_vtt(tmp_path):
    vtt_content = "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nHello knowledge\n"

    def fake_run(args, **kwargs):
        lang = args[args.index("--sub-lang") + 1]
        vtt = tmp_path / f"test_vid.{lang}.vtt"
        vtt.write_text(vtt_content, encoding="utf-8")
        return MagicMock(returncode=0)

    with (
        patch("estormi_ingestion.knowledge.knowledge_fetch.subprocess.run", side_effect=fake_run),
        patch(
            "estormi_ingestion.knowledge.knowledge_fetch._yt_dlp_bin", return_value="/fake/yt-dlp"
        ),
        patch("estormi_ingestion.knowledge.knowledge_fetch._RUN_TMP_DIR", tmp_path),
    ):
        result = download_transcript("test_vid", ["fr"])

    assert result is not None
    assert "Hello knowledge" in result


def test_download_transcript_prefers_first_lang(tmp_path):
    """If the first language's VTT is available it should return without trying others."""
    calls: list[str] = []

    def fake_run(args, **kwargs):
        lang = args[args.index("--sub-lang") + 1]
        calls.append(lang)
        if lang == "fr":
            vtt = tmp_path / "pref_vid.fr.vtt"
            vtt.write_text("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nContenu\n")
        return MagicMock(returncode=0)

    with (
        patch("estormi_ingestion.knowledge.knowledge_fetch.subprocess.run", side_effect=fake_run),
        patch(
            "estormi_ingestion.knowledge.knowledge_fetch._yt_dlp_bin", return_value="/fake/yt-dlp"
        ),
        patch("estormi_ingestion.knowledge.knowledge_fetch._RUN_TMP_DIR", tmp_path),
    ):
        result = download_transcript("pref_vid", ["fr", "en"])

    assert result is not None
    assert "Contenu" in result
    assert calls == ["fr"]  # stopped after fr succeeded


def test_download_transcript_terminates_options_before_url(tmp_path):
    """Arg-injection guard: ``--`` precedes the watch URL on every language try.

    The transcript fetch builds the URL from a video id (``watch?v=<id>``). The
    ``--`` terminator keeps a hostile id from being mis-parsed as a yt-dlp flag.
    This fails if the terminator is removed from the transcript subprocess argv.
    """
    captured_calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        captured_calls.append(list(args))
        return MagicMock(returncode=0)  # no VTT written → all langs tried

    with (
        patch("estormi_ingestion.knowledge.knowledge_fetch.subprocess.run", side_effect=fake_run),
        patch(
            "estormi_ingestion.knowledge.knowledge_fetch._yt_dlp_bin", return_value="/fake/yt-dlp"
        ),
        patch("estormi_ingestion.knowledge.knowledge_fetch._RUN_TMP_DIR", tmp_path),
    ):
        download_transcript("vidXYZ", ["fr", "en"])

    assert captured_calls, "subprocess.run must have been invoked at least once"
    for args in captured_calls:
        # URL is last; terminator sits immediately before it; only the URL
        # follows the terminator.
        assert args[-1] == "https://www.youtube.com/watch?v=vidXYZ"
        assert args[-2] == "--"
        term_idx = args.index("--")
        assert args[term_idx + 1 :] == ["https://www.youtube.com/watch?v=vidXYZ"]


# ── knowledge_fetch: fetch_rss_source ────────────────────────────────────────


def _make_rss_entry(title, link, published_tuple, summary=""):
    """Build a minimal feedparser-like entry object."""
    import types

    e = types.SimpleNamespace()
    e.title = title
    e.link = link
    e.id = link
    e.published_parsed = published_tuple
    e.updated_parsed = None
    e.summary = summary
    return e


def _make_feed(entries):
    import types

    f = types.SimpleNamespace()
    f.entries = entries
    return f


def test_fetch_rss_source_returns_recent_articles():
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(hours=1)).timetuple()
    old = (now - timedelta(hours=48)).timetuple()

    entries = [
        _make_rss_entry("Titre récent", "https://example.com/1", recent, "Résumé récent"),
        _make_rss_entry("Titre ancien", "https://example.com/2", old, "Résumé ancien"),
    ]
    fake_feed = _make_feed(entries)

    with (
        patch("estormi_ingestion.knowledge.knowledge_fetch._rss_fetch_public", return_value=b""),
        patch("feedparser.parse", return_value=fake_feed),
    ):
        articles = fetch_rss_source({"urls": ["https://example.com/feed"], "window_hours": 24})

    assert len(articles) == 1
    assert articles[0]["title"] == "Titre récent"
    assert articles[0]["summary"] == "Résumé récent"


def test_fetch_rss_source_deduplicates_by_url():
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(hours=1)).timetuple()

    entries = [
        _make_rss_entry("Article A", "https://example.com/1", recent),
        _make_rss_entry("Article A copie", "https://example.com/1", recent),
    ]
    fake_feed = _make_feed(entries)

    with (
        patch("estormi_ingestion.knowledge.knowledge_fetch._rss_fetch_public", return_value=b""),
        patch("feedparser.parse", return_value=fake_feed),
    ):
        articles = fetch_rss_source(
            {
                "urls": ["https://example.com/feed", "https://example.com/feed2"],
                "window_hours": 24,
            }
        )

    urls = [a["url"] for a in articles]
    assert len(urls) == len(set(urls)), "Duplicate URLs must be removed"


def test_fetch_rss_source_strips_html_from_summary():
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(hours=1)).timetuple()

    entries = [
        _make_rss_entry("Test", "https://example.com/1", recent, "<p>Texte <b>important</b></p>")
    ]
    fake_feed = _make_feed(entries)

    with (
        patch("estormi_ingestion.knowledge.knowledge_fetch._rss_fetch_public", return_value=b""),
        patch("feedparser.parse", return_value=fake_feed),
    ):
        articles = fetch_rss_source({"urls": ["https://example.com/feed"], "window_hours": 24})

    assert "<p>" not in articles[0]["summary"]
    assert "Texte important" in articles[0]["summary"]


def test_fetch_rss_source_tolerates_feed_error():
    with (
        patch("estormi_ingestion.knowledge.knowledge_fetch._rss_fetch_public", return_value=b""),
        patch("feedparser.parse", side_effect=Exception("network error")),
    ):
        articles = fetch_rss_source({"urls": ["https://example.com/feed"], "window_hours": 24})

    assert articles == []


def test_fetch_rss_source_aggregates_multiple_urls():
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(hours=1)).timetuple()

    feed1 = _make_feed([_make_rss_entry("Art 1", "https://a.com/1", recent)])
    feed2 = _make_feed([_make_rss_entry("Art 2", "https://b.com/2", recent)])

    with (
        patch("estormi_ingestion.knowledge.knowledge_fetch._rss_fetch_public", return_value=b""),
        patch("feedparser.parse", side_effect=[feed1, feed2]),
    ):
        articles = fetch_rss_source(
            {"urls": ["https://a.com/feed", "https://b.com/feed"], "window_hours": 24}
        )

    assert len(articles) == 2
    titles = {a["title"] for a in articles}
    assert titles == {"Art 1", "Art 2"}


def test_fetch_rss_source_rejects_loopback_url():
    """SSRF guard: a URL whose host resolves to loopback must not reach feedparser."""
    from estormi_ingestion.knowledge.knowledge_fetch import _rss_url_is_public

    assert _rss_url_is_public("http://localhost/feed") is False
    assert _rss_url_is_public("http://127.0.0.1/feed") is False
    assert _rss_url_is_public("http://169.254.169.254/latest/meta-data/") is False
    # The public-side guard accepts well-known DNS that resolves to a public IP.
    # We can't assert positive here without network; the negative cases are enough.


# ── knowledge_fetch: _rss_fetch_public (SSRF / per-hop re-validation) ─────────


class _FakeResponse:
    """Stand-in for the streamed httpx.Response subset the fetcher reads.

    The fetcher uses ``with httpx.stream("GET", ...) as resp:`` and reads the
    body via ``resp.iter_bytes()`` under a size cap, so the fake is a context
    manager exposing ``status_code``, ``headers`` and a chunked ``iter_bytes``.
    ``chunk_size`` lets a test split a large body into many chunks to exercise
    the incremental cap without a real multi-megabyte allocation up front.
    """

    def __init__(
        self,
        status_code: int,
        *,
        location: str | None = None,
        content: bytes = b"",
        content_length: int | None = None,
        chunk_size: int = 1 << 16,
    ):
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        if location is not None:
            self.headers["location"] = location
        if content_length is not None:
            self.headers["content-length"] = str(content_length)
        self.content = content
        self._chunk_size = max(1, chunk_size)

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc) -> None:
        return None

    def iter_bytes(self):
        for i in range(0, len(self.content), self._chunk_size):
            yield self.content[i : i + self._chunk_size]


class _FakeURL:
    """Stand-in for httpx.URL modelling the ``str()`` vs ``human_repr()`` split.

    The real fetcher resolves a redirect target with
    ``str(httpx.URL(current).join(loc))``. ``str()`` returns the **wire-form**
    (percent-encoded) URL; httpx.URL.``human_repr()`` percent-**decodes** it for
    display. The SSRF guard must re-validate the wire-form — the exact string
    that is then fetched — so a regression to ``human_repr()`` (which the commit
    that introduced this split fixed) would validate a *different* string than
    it fetches. We model both surfaces faithfully (``join`` via stdlib urljoin,
    which preserves encoding; ``human_repr`` via ``unquote``) so the regression
    test below can actually catch a revert — without importing real httpx.
    """

    def __init__(self, url: str):
        self._url = url

    def join(self, other: str) -> _FakeURL:
        from urllib.parse import urljoin

        return _FakeURL(urljoin(self._url, other))

    def human_repr(self) -> str:
        from urllib.parse import unquote

        return unquote(self._url)

    def __str__(self) -> str:
        return self._url


def _install_fake_httpx(monkeypatch, *, get):
    """Inject a fake ``httpx`` module so the in-function import resolves to it.

    ``_rss_fetch_public`` does ``import httpx`` at call time, so swapping
    ``sys.modules['httpx']`` is enough — no real HTTP client is constructed and
    no socket is ever opened.
    """
    fake = types.ModuleType("httpx")
    fake.URL = _FakeURL

    # Production fetches via ``httpx.stream("GET", url, ...)``; the per-test
    # callbacks are keyed on the URL, so adapt stream(method, url, ...) → get(url).
    def stream(method, url, **kwargs):
        return get(url, **kwargs)

    fake.stream = stream
    monkeypatch.setitem(sys.modules, "httpx", fake)
    return fake


def test_rss_fetch_public_rejects_direct_loopback_up_front(monkeypatch):
    """A loopback URL is refused before any HTTP request is attempted."""
    from estormi_ingestion.knowledge import knowledge_fetch

    calls: list[str] = []

    def fake_get(url, **kwargs):  # pragma: no cover - must never run
        calls.append(url)
        raise AssertionError("httpx.get must not be called for a loopback URL")

    _install_fake_httpx(monkeypatch, get=fake_get)

    result = knowledge_fetch._rss_fetch_public("http://127.0.0.1:8080/feed")

    assert result is None
    assert calls == [], "the SSRF gate must short-circuit before the network call"


def test_rss_fetch_public_rejects_redirect_to_loopback(monkeypatch):
    """Per-hop re-validation: a public URL that 302s to loopback is refused.

    The bootstrap host passes the public gate, the server answers with a
    redirect to ``http://127.0.0.1/`` (a classic SSRF pivot), and the guard
    must re-validate the *new* target and refuse it — never fetching the
    loopback body. This proves the check runs on every hop, not just the first.
    """
    from estormi_ingestion.knowledge import knowledge_fetch

    fetched: list[str] = []

    def fake_get(url, **kwargs):
        fetched.append(url)
        if url == "http://feed.public.test/rss":
            return _FakeResponse(302, location="http://127.0.0.1/internal")
        raise AssertionError(f"loopback target was fetched: {url}")

    _install_fake_httpx(monkeypatch, get=fake_get)

    # The bootstrap host must pass the gate; the redirect target (a numeric
    # loopback literal) is judged by the real guard, which rejects it offline.
    checked: list[str] = []
    real_is_public = knowledge_fetch._rss_url_is_public

    def spy_is_public(url):
        checked.append(url)
        if url == "http://feed.public.test/rss":
            return True  # avoid a DNS lookup for the synthetic bootstrap host
        return real_is_public(url)

    monkeypatch.setattr(knowledge_fetch, "_rss_url_is_public", spy_is_public)

    result = knowledge_fetch._rss_fetch_public("http://feed.public.test/rss")

    assert result is None, "redirect to loopback must yield no body"
    assert fetched == ["http://feed.public.test/rss"], "loopback target must never be fetched"
    # The guard ran on both hops — the bootstrap and the redirect target.
    assert "http://feed.public.test/rss" in checked
    assert "http://127.0.0.1/internal" in checked


def test_rss_fetch_public_follows_redirect_to_public_target(monkeypatch):
    """Positive control: a redirect to another public host is followed and its
    body returned — proving the rejection above is the guard firing, not the
    loop being a no-op that always returns None."""
    from estormi_ingestion.knowledge import knowledge_fetch

    fetched: list[str] = []

    def fake_get(url, **kwargs):
        fetched.append(url)
        if url == "http://feed.public.test/rss":
            return _FakeResponse(301, location="http://mirror.public.test/rss")
        return _FakeResponse(200, content=b"<rss>ok</rss>")

    _install_fake_httpx(monkeypatch, get=fake_get)
    # Both synthetic public hosts pass the gate without DNS.
    monkeypatch.setattr(
        knowledge_fetch,
        "_rss_url_is_public",
        lambda url: (
            url.startswith("http://feed.public.test") or url.startswith("http://mirror.public.test")
        ),
    )

    result = knowledge_fetch._rss_fetch_public("http://feed.public.test/rss")

    assert result == b"<rss>ok</rss>"
    assert fetched == [
        "http://feed.public.test/rss",
        "http://mirror.public.test/rss",
    ]


def test_rss_fetch_public_rejects_oversized_body(monkeypatch):
    """A feed body over the cap is dropped instead of buffered into memory.

    httpx has no default response-size limit, so a hostile or compromised feed
    could stream gigabytes and OOM the ingestion engine. The fetcher reads the
    stream incrementally and aborts past ``_RSS_MAX_BODY_BYTES``. We assert both
    the incremental path (no/short Content-Length) and the up-front
    Content-Length fast-reject return None.
    """
    from estormi_ingestion.knowledge import knowledge_fetch

    # Accept the synthetic host so the test exercises the body cap, not the gate.
    monkeypatch.setattr(knowledge_fetch, "_rss_url_is_public", lambda url: True)
    cap = knowledge_fetch._RSS_MAX_BODY_BYTES

    # 1) Incremental cap: a body just over the cap, delivered in chunks, with no
    #    Content-Length header to give it away. Must abort mid-read → None.
    big = b"a" * (cap + 1)

    def fake_get_no_clen(url, **kwargs):
        return _FakeResponse(200, content=big, chunk_size=1 << 20)

    _install_fake_httpx(monkeypatch, get=fake_get_no_clen)
    assert knowledge_fetch._rss_fetch_public("http://feed.public.test/rss") is None

    # 2) Content-Length fast-reject: an honest over-cap header is refused up front.
    def fake_get_clen(url, **kwargs):
        return _FakeResponse(200, content=b"x", content_length=cap + 1)

    _install_fake_httpx(monkeypatch, get=fake_get_clen)
    assert knowledge_fetch._rss_fetch_public("http://feed.public.test/rss") is None

    # 3) A body exactly at the cap is still accepted (the cap is inclusive).
    ok = b"b" * cap

    def fake_get_ok(url, **kwargs):
        return _FakeResponse(200, content=ok, chunk_size=1 << 20)

    _install_fake_httpx(monkeypatch, get=fake_get_ok)
    assert knowledge_fetch._rss_fetch_public("http://feed.public.test/rss") == ok


def test_rss_fetch_public_revalidates_wire_form_not_decoded(monkeypatch):
    """Regression for the redirect re-validation fix: the per-hop SSRF guard
    must see the **wire-form** (percent-encoded) redirect target — the exact
    string that is then fetched — not a human-decoded one.

    The fetcher resolves each hop with ``str(httpx.URL(current).join(loc))``.
    If it reverted to ``.human_repr()`` (which percent-DECODES), the string the
    guard validates would diverge from the string httpx actually fetches, so an
    encoded ``Location`` could be validated in one shape and fetched in another.
    Here the redirect target is percent-encoded; we assert the encoded form is
    what both the guard and the fetch see, and the decoded form never appears.
    A revert to ``human_repr()`` makes ``_FakeURL`` decode and fails this test.
    """
    from urllib.parse import unquote

    from estormi_ingestion.knowledge import knowledge_fetch

    encoded = "http://mirror.public.test/%2e%2e/internal"
    decoded = unquote(encoded)
    assert decoded != encoded, "the target must actually differ once decoded"

    fetched: list[str] = []

    def fake_get(url, **kwargs):
        fetched.append(url)
        if url == "http://feed.public.test/rss":
            return _FakeResponse(302, location=encoded)
        return _FakeResponse(200, content=b"<rss>ok</rss>")

    _install_fake_httpx(monkeypatch, get=fake_get)

    checked: list[str] = []

    def spy_is_public(url):
        checked.append(url)
        return True  # accept every host so the loop reaches the fetch

    monkeypatch.setattr(knowledge_fetch, "_rss_url_is_public", spy_is_public)

    knowledge_fetch._rss_fetch_public("http://feed.public.test/rss")

    # The encoded wire-form is what gets re-validated AND fetched on the 2nd hop.
    assert encoded in checked, "the SSRF guard must re-validate the wire-form URL"
    assert encoded in fetched, "the fetched URL must be the wire-form URL"
    # The decoded form must never leak in (that would mean human_repr() is back).
    assert decoded not in checked
    assert decoded not in fetched


def test_rss_fetch_public_stops_at_redirect_limit(monkeypatch):
    """An endless redirect chain is bounded by ``_RSS_MAX_REDIRECTS`` and gives
    up with no body rather than looping forever."""
    from estormi_ingestion.knowledge import knowledge_fetch

    hops: list[str] = []

    def fake_get(url, **kwargs):
        hops.append(url)
        # Always redirect to a fresh public URL → never terminates on its own.
        return _FakeResponse(302, location=f"http://public.test/{len(hops)}")

    _install_fake_httpx(monkeypatch, get=fake_get)
    monkeypatch.setattr(knowledge_fetch, "_rss_url_is_public", lambda url: True)

    result = knowledge_fetch._rss_fetch_public("http://public.test/start")

    assert result is None
    assert len(hops) == knowledge_fetch._RSS_MAX_REDIRECTS

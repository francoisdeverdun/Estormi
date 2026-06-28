"""Behaviour tests for the knowledge-sources router and its pure helpers.

Covers the resolution logic in ``services/knowledge_sources.py`` — the
URL-resolution heuristics (``deduce_kind`` / ``is_youtube`` /
``youtube_label_from_url``), the SSRF guard (``url_is_public``), the
redirect-aware fetch (``fetch_public``), RSS/YouTube metadata extraction — plus
the ``api/knowledge_sources.py`` router (YAML CRUD + Finder + resolve routes),
with every network / DNS / subprocess boundary mocked so the suite stays
offline and deterministic.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from estormi_server.api import knowledge_sources as ks
from estormi_server.services import knowledge_sources as svc

pytestmark = pytest.mark.integration


# ── _kb_deduce_kind ──────────────────────────────────────────────────────────


class TestDeduceKind:
    def test_finance_keyword(self):
        assert ks._kb_deduce_kind("Bitcoin crypto market") == "finance"

    def test_economic_keyword(self):
        assert ks._kb_deduce_kind("macro inflation report") == "economic"

    def test_politic_keyword(self):
        assert ks._kb_deduce_kind("the election senate debate") == "politic"

    def test_tech_keyword(self):
        assert ks._kb_deduce_kind("AI software developer channel") == "tech"

    def test_default_news(self):
        assert ks._kb_deduce_kind("just some daily headlines") == "news"

    def test_case_insensitive(self):
        assert ks._kb_deduce_kind("CRYPTO WALLET") == "finance"

    def test_first_matching_axis_wins(self):
        # "finance" is checked before "tech"; both keywords present → finance.
        assert ks._kb_deduce_kind("crypto software") == "finance"

    def test_empty_text_is_news(self):
        assert ks._kb_deduce_kind("") == "news"


# ── _kb_is_youtube ───────────────────────────────────────────────────────────


class TestIsYoutube:
    def test_youtube_com(self):
        assert ks._kb_is_youtube("https://www.youtube.com/@Foo/videos") is True

    def test_youtu_be(self):
        assert ks._kb_is_youtube("https://youtu.be/abc123") is True

    def test_case_insensitive(self):
        assert ks._kb_is_youtube("HTTPS://YOUTUBE.COM/x") is True

    def test_non_youtube(self):
        assert ks._kb_is_youtube("https://example.com/feed.rss") is False


# ── _kb_youtube_label_from_url ───────────────────────────────────────────────


class TestYoutubeLabelFromUrl:
    def test_handle_form(self):
        assert ks._kb_youtube_label_from_url("https://youtube.com/@HugoDecrypte") == "HugoDecrypte"

    def test_c_form(self):
        assert ks._kb_youtube_label_from_url("https://youtube.com/c/SomeChannel") == "SomeChannel"

    def test_user_form(self):
        assert ks._kb_youtube_label_from_url("https://youtube.com/user/old_name") == "old name"

    def test_channel_form(self):
        assert ks._kb_youtube_label_from_url("https://youtube.com/channel/UC123") == "UC123"

    def test_dashes_and_underscores_become_spaces(self):
        assert ks._kb_youtube_label_from_url("https://youtube.com/@a-b_c") == "a b c"

    def test_no_match_falls_back(self):
        assert ks._kb_youtube_label_from_url("https://youtu.be/abc123") == "YouTube channel"


# ── _kb_yaml_path ────────────────────────────────────────────────────────────


def test_kb_yaml_path_under_data_dir():
    path = ks._kb_yaml_path()
    assert path.name == "knowledge_sources.yaml"
    from estormi_server.storage import tools

    assert str(path).startswith(str(tools.DATA_DIR))


# ── _kb_url_is_public (SSRF guard) ───────────────────────────────────────────


def _addrinfo(ip: str):
    """Minimal getaddrinfo tuple list with one entry for ``ip``."""
    return [(2, 1, 6, "", (ip, 0))]


class TestUrlIsPublic:
    def test_public_address_allowed(self):
        with patch.object(svc.socket, "getaddrinfo", return_value=_addrinfo("93.184.216.34")):
            assert svc.url_is_public("https://example.com/feed") is True

    def test_loopback_rejected(self):
        with patch.object(svc.socket, "getaddrinfo", return_value=_addrinfo("127.0.0.1")):
            assert svc.url_is_public("http://localhost/admin") is False

    def test_private_rejected(self):
        with patch.object(svc.socket, "getaddrinfo", return_value=_addrinfo("192.168.1.10")):
            assert svc.url_is_public("http://router.lan/") is False

    def test_link_local_metadata_rejected(self):
        # Cloud metadata endpoint — the SSRF guard's headline target.
        with patch.object(svc.socket, "getaddrinfo", return_value=_addrinfo("169.254.169.254")):
            assert svc.url_is_public("http://169.254.169.254/latest/meta-data") is False

    def test_dns_failure_rejected(self):
        with patch.object(svc.socket, "getaddrinfo", side_effect=OSError("no DNS")):
            assert svc.url_is_public("https://nope.invalid/") is False

    def test_no_host_rejected(self):
        # urlparse yields no hostname for a bare path.
        assert svc.url_is_public("not-a-url") is False

    def test_any_private_record_rejects_whole_host(self):
        infos = _addrinfo("93.184.216.34") + _addrinfo("10.0.0.1")
        with patch.object(svc.socket, "getaddrinfo", return_value=infos):
            assert svc.url_is_public("https://mixed.example/") is False


# ── _kb_fetch_public (redirect-aware, guard re-applied per hop) ──────────────


class TestFetchPublic:
    def test_non_public_returns_none_without_fetch(self):
        with (
            patch.object(svc, "url_is_public", return_value=False),
            patch.object(svc.httpx, "get") as mock_get,
        ):
            assert svc.fetch_public("http://10.0.0.1/x") is None
        mock_get.assert_not_called()

    def test_direct_200_returned(self):
        resp = MagicMock(status_code=200)
        with (
            patch.object(svc, "url_is_public", return_value=True),
            patch.object(svc.httpx, "get", return_value=resp) as mock_get,
        ):
            assert svc.fetch_public("https://example.com/feed") is resp
        mock_get.assert_called_once()

    def test_redirect_without_location_returns_response(self):
        resp = MagicMock(status_code=301, headers={})
        with (
            patch.object(svc, "url_is_public", return_value=True),
            patch.object(svc.httpx, "get", return_value=resp),
        ):
            assert svc.fetch_public("https://example.com/x") is resp

    def test_follows_redirect_to_final_response(self):
        # A 302 with a Location is followed: the next hop fetches the joined
        # absolute URL and the final 200 response is returned.
        redirect = MagicMock(status_code=302, headers={"location": "/final"})
        final = MagicMock(status_code=200)
        with (
            patch.object(svc, "url_is_public", return_value=True),
            patch.object(svc.httpx, "get", side_effect=[redirect, final]) as mock_get,
        ):
            assert svc.fetch_public("https://example.com/start") is final
        # Second hop targets the resolved absolute URL, not the relative Location.
        assert str(mock_get.call_args_list[1].args[0]) == "https://example.com/final"

    def test_redirect_target_reruns_ssrf_guard(self):
        # The guard is re-applied per hop: a redirect pointing at a private host
        # is refused (returns None) before the second fetch happens.
        redirect = MagicMock(status_code=302, headers={"location": "http://169.254.169.254/"})
        final = MagicMock(status_code=200)
        with (
            patch.object(svc, "url_is_public", side_effect=[True, False]),
            patch.object(svc.httpx, "get", side_effect=[redirect, final]) as mock_get,
        ):
            assert svc.fetch_public("https://example.com/start") is None
        mock_get.assert_called_once()


# ── _kb_resolve_rss ──────────────────────────────────────────────────────────


class TestResolveRss:
    def test_parses_title_and_strips_html_from_desc(self):
        import feedparser

        resp = MagicMock(content=b"<rss/>")
        feed = MagicMock()
        feed.feed = {"title": "  Le Monde  ", "description": "<b>World</b> news"}
        with (
            patch.object(svc, "fetch_public", return_value=resp),
            patch.object(feedparser, "parse", return_value=feed),
        ):
            title, desc = svc.resolve_rss("https://example.com/rss")
        assert title == "Le Monde"
        assert desc == "World news"

    def test_subtitle_preferred_over_description(self):
        import feedparser

        resp = MagicMock(content=b"<feed/>")
        feed = MagicMock()
        feed.feed = {"title": "T", "subtitle": "the subtitle", "description": "the desc"}
        with (
            patch.object(svc, "fetch_public", return_value=resp),
            patch.object(feedparser, "parse", return_value=feed),
        ):
            _title, desc = svc.resolve_rss("https://example.com/rss")
        assert desc == "the subtitle"

    def test_fetch_none_yields_empty(self):
        with patch.object(svc, "fetch_public", return_value=None):
            assert svc.resolve_rss("https://example.com/rss") == ("", "")

    def test_fetch_exception_yields_empty(self):
        with patch.object(svc, "fetch_public", side_effect=RuntimeError("boom")):
            assert svc.resolve_rss("https://example.com/rss") == ("", "")


# ── _kb_resolve_youtube ──────────────────────────────────────────────────────


class TestResolveYoutube:
    def test_no_binary_falls_back_to_url_label(self):
        with patch.object(svc, "youtube_label_from_url", return_value="Fallback"):
            with patch("shutil.which", return_value=None):
                assert svc.resolve_youtube("https://youtube.com/@x") == "Fallback"

    def test_yt_dlp_channel_name_returned(self):
        result = MagicMock(stdout="Hugo Décrypte\n")
        with (
            patch("shutil.which", return_value="/usr/bin/yt-dlp"),
            patch("subprocess.run", return_value=result) as mock_run,
        ):
            assert svc.resolve_youtube("https://youtube.com/@hugo") == "Hugo Décrypte"
        mock_run.assert_called_once()

    def test_na_output_falls_back(self):
        result = MagicMock(stdout="NA\n")
        with (
            patch("shutil.which", return_value="/usr/bin/yt-dlp"),
            patch("subprocess.run", return_value=result),
            patch.object(svc, "youtube_label_from_url", return_value="FromUrl"),
        ):
            assert svc.resolve_youtube("https://youtube.com/@x") == "FromUrl"

    def test_subprocess_exception_falls_back(self):
        with (
            patch("shutil.which", return_value="/usr/bin/yt-dlp"),
            patch("subprocess.run", side_effect=OSError("spawn failed")),
            patch.object(svc, "youtube_label_from_url", return_value="Safe"),
        ):
            assert svc.resolve_youtube("https://youtube.com/@x") == "Safe"


# ── GET /api/knowledge/sources ───────────────────────────────────────────────


class TestGetSources:
    async def test_returns_loaded_sources(self, client):
        sample = [{"id": "ch", "label": "Ch", "type": "rss"}]
        with patch.object(svc, "yaml_load", return_value=sample):
            resp = await client.get("/api/knowledge/sources")
        assert resp.status_code == 200
        assert resp.json() == sample


# ── PUT /api/knowledge/sources ───────────────────────────────────────────────


class TestPutSources:
    async def test_writes_yaml_and_defaults_subtitle_langs(self, client):
        sources_in = [{"id": "ch", "label": "Ch", "type": "youtube_channel"}]
        # Let the real (temp-dir) YAML write run; only stub the DB-path register.
        with patch.object(ks, "_kb_register_path", new_callable=AsyncMock) as mock_reg:
            resp = await client.put("/api/knowledge/sources", json=sources_in)

        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "count": 1}
        mock_reg.assert_awaited_once()
        # The written file round-trips with the injected default subtitle_langs.
        import yaml

        written = yaml.safe_load(ks._kb_yaml_path().read_text(encoding="utf-8"))
        assert written["sources"][0]["subtitle_langs"] == ["en", "fr"]

    async def test_write_failure_returns_500(self, client):
        with patch.object(
            ks, "_kb_register_path", new_callable=AsyncMock, side_effect=OSError("disk full")
        ):
            resp = await client.put("/api/knowledge/sources", json=[{"id": "x"}])
        assert resp.status_code == 500
        assert "error" in resp.json()

    async def test_oversize_payload_rejected(self, client):
        too_many = [{"id": str(i)} for i in range(ks._KB_MAX_SOURCES + 1)]
        resp = await client.put("/api/knowledge/sources", json=too_many)
        assert resp.status_code == 422


# ── _kb_register_path ────────────────────────────────────────────────────────


async def test_register_path_persists_setting(db):
    await ks._kb_register_path(db)
    cur = await db.execute("SELECT value FROM settings WHERE key = 'knowledge_sources_yaml'")
    row = await cur.fetchone()
    assert row is not None
    assert row[0].endswith("knowledge_sources.yaml")


# ── POST /api/knowledge/open-sources ─────────────────────────────────────────


class TestOpenInFinder:
    async def test_opens_finder_ok(self, client):
        # Never spawn a real process — stub Popen, let to_thread run it.
        with patch("subprocess.Popen") as mock_popen:
            resp = await client.post("/api/knowledge/open-sources")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
        mock_popen.assert_called_once()
        assert mock_popen.call_args[0][0][:2] == ["open", "-R"]

    async def test_open_failure_returns_500(self, client):
        with patch("subprocess.Popen", side_effect=OSError("no Finder")):
            resp = await client.post("/api/knowledge/open-sources")
        assert resp.status_code == 500
        assert "error" in resp.json()


# ── POST /api/knowledge/resolve ──────────────────────────────────────────────


class TestResolve:
    async def test_missing_url_400(self, client):
        resp = await client.post("/api/knowledge/resolve", json={})
        assert resp.status_code == 400
        assert resp.json()["error"] == "url required"

    async def test_bad_scheme_400(self, client):
        resp = await client.post("/api/knowledge/resolve", json={"url": "ftp://x/y"})
        assert resp.status_code == 400
        assert "http" in resp.json()["error"]

    async def test_non_public_url_400(self, client):
        with patch.object(svc, "url_is_public", return_value=False):
            resp = await client.post("/api/knowledge/resolve", json={"url": "http://localhost/x"})
        assert resp.status_code == 400
        assert "public" in resp.json()["error"]

    async def test_youtube_draft(self, client):
        with (
            patch.object(svc, "url_is_public", return_value=True),
            patch.object(svc, "resolve_youtube", return_value="Hugo Décrypte"),
        ):
            resp = await client.post(
                "/api/knowledge/resolve",
                json={"url": "https://www.youtube.com/@hugodecrypteactus"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["type"] == "youtube_channel"
        assert body["label"] == "Hugo Décrypte"
        assert body["url"] == "https://www.youtube.com/@hugodecrypteactus"
        assert "axis" in body

    async def test_rss_draft(self, client):
        with (
            patch.object(svc, "url_is_public", return_value=True),
            patch.object(svc, "resolve_rss", return_value=("Finance Daily", "stock market")),
        ):
            resp = await client.post(
                "/api/knowledge/resolve", json={"url": "https://example.com/rss"}
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["type"] == "rss"
        assert body["label"] == "Finance Daily"
        assert body["urls"] == ["https://example.com/rss"]
        assert body["axis"] == "finance"

    async def test_rss_draft_falls_back_to_url_label(self, client):
        with (
            patch.object(svc, "url_is_public", return_value=True),
            patch.object(svc, "resolve_rss", return_value=("", "")),
        ):
            resp = await client.post(
                "/api/knowledge/resolve", json={"url": "https://example.com/rss"}
            )
        assert resp.status_code == 200
        assert resp.json()["label"] == "https://example.com/rss"

"""load_sources — YAML config validation."""

from __future__ import annotations

import pytest

from estormi_briefing.run_briefing import load_sources

pytestmark = pytest.mark.unit

# ── run_knowledge: load_sources ───────────────────────────────────────────────


def test_load_sources_valid(tmp_path):
    yaml_content = """
sources:
  - id: test_ch
    label: Test Channel
    type: youtube_channel
    url: https://www.youtube.com/@Test/videos
    axis: tech
    mode: news
    subtitle_langs: [fr]
"""
    p = tmp_path / "sources.yaml"
    p.write_text(yaml_content, encoding="utf-8")
    sources = load_sources(p)
    assert len(sources) == 1
    assert sources[0]["id"] == "test_ch"
    assert sources[0]["mode"] == "news"


def test_load_sources_invalid_mode(tmp_path):
    p = tmp_path / "sources.yaml"
    p.write_text(
        "sources:\n  - id: bad\n    label: Bad\n    type: youtube_channel\n"
        "    url: https://x.com\n    axis: tech\n    mode: invalid_mode\n    subtitle_langs: [fr]\n"
    )
    with pytest.raises(ValueError, match="invalid mode"):
        load_sources(p)


def test_load_sources_missing_field(tmp_path):
    p = tmp_path / "sources.yaml"
    p.write_text(
        "sources:\n  - id: incomplete\n    label: Missing\n    type: youtube_channel\n"
        "    url: https://x.com\n    axis: tech\n"  # missing mode + subtitle_langs
    )
    with pytest.raises(ValueError, match="missing fields"):
        load_sources(p)


def test_load_sources_unsupported_type(tmp_path):
    p = tmp_path / "sources.yaml"
    p.write_text(
        "sources:\n  - id: pod\n    label: Podcast\n    type: apple_podcast\n"
        "    url: https://x.com\n    axis: tech\n    mode: news\n    subtitle_langs: [en]\n"
    )
    with pytest.raises(ValueError, match="unsupported type"):
        load_sources(p)


def test_load_sources_rss_valid(tmp_path):
    p = tmp_path / "sources.yaml"
    p.write_text(
        "sources:\n"
        "  - type: rss\n"
        "    label: Le Monde\n"
        "    urls:\n"
        "      - https://www.lemonde.fr/rss/une.xml\n"
        "      - https://www.lemonde.fr/politique/rss_full.xml\n"
        "    window_hours: 24\n"
        "    pre_prompt: Synthétise les articles.\n"
    )
    sources = load_sources(p)
    assert len(sources) == 1
    s = sources[0]
    assert s["type"] == "rss"
    assert s["label"] == "Le Monde"
    assert len(s["urls"]) == 2
    # Defaults filled in
    assert "id" in s
    assert s["axis"] == "news"
    assert s["mode"] == "news"


def test_load_sources_rss_missing_urls(tmp_path):
    p = tmp_path / "sources.yaml"
    p.write_text("sources:\n  - type: rss\n    label: No URLs\n")
    with pytest.raises(ValueError, match="missing fields"):
        load_sources(p)


def test_load_sources_multiple(tmp_path):
    yaml_content = """
sources:
  - id: a
    label: A
    type: youtube_channel
    url: https://www.youtube.com/@A/videos
    axis: tech
    mode: news
    subtitle_langs: [fr]
  - id: b
    label: B
    type: youtube_channel
    url: https://www.youtube.com/@B/videos
    axis: finance
    mode: opinion
    subtitle_langs: [en]
"""
    p = tmp_path / "sources.yaml"
    p.write_text(yaml_content)
    sources = load_sources(p)
    assert len(sources) == 2
    assert sources[1]["axis"] == "finance"


def test_load_sources_normalises_promotional_flag(tmp_path):
    yaml_content = """
sources:
  - id: vendor
    label: Vendor Channel
    type: youtube_channel
    url: https://www.youtube.com/@Vendor/videos
    axis: news
    mode: news
    subtitle_langs: [fr]
    promotional: true
  - id: press
    label: Press Feed
    type: rss
    urls: [https://example.com/feed]
"""
    cfg = tmp_path / "sources.yaml"
    cfg.write_text(yaml_content)
    sources = load_sources(cfg)
    by_id = {s["id"]: s for s in sources}
    assert by_id["vendor"]["promotional"] is True
    # Absent → normalised to a real False, never a missing key.
    assert by_id["press"]["promotional"] is False

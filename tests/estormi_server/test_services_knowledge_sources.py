"""Unit tests for the pure classification logic in
``estormi_server.services.knowledge_sources``.

The router-driven behaviour (routes, SSRF guard, redirect fetch, RSS/YouTube
resolution) is covered in ``tests/estormi_server/test_knowledge_sources.py``. This file pins
the subtle business rule the heuristic depends on: keyword buckets are checked
in declaration order, so the first matching axis wins.
"""

from __future__ import annotations

import pytest

from estormi_server.services import knowledge_sources as svc

pytestmark = pytest.mark.unit


class TestDeduceKindPrecedence:
    def test_default_is_news(self):
        assert svc.deduce_kind("daily headlines roundup") == "news"
        assert svc.deduce_kind("") == "news"

    def test_finance_beats_economic_when_both_match(self):
        # "crypto" (finance) and "business" (economic) both hit; finance is
        # declared first in KIND_KEYWORDS, so it wins.
        assert svc.deduce_kind("crypto business news") == "finance"

    def test_economic_beats_tech_when_both_match(self):
        # "macro" (economic) precedes "tech" in the bucket order.
        assert svc.deduce_kind("macro tech outlook") == "economic"

    def test_case_insensitive(self):
        assert svc.deduce_kind("BITCOIN WALLET") == "finance"

    def test_substring_prefix_match(self):
        # "financ" prefix matches "financial".
        assert svc.deduce_kind("financial planning") == "finance"

    def test_first_declared_bucket_order_is_stable(self):
        # The declaration order is load-bearing; assert it explicitly so a
        # reorder that would change precedence is caught here.
        assert list(svc.KIND_KEYWORDS) == ["finance", "economic", "politic", "tech"]


class TestYoutubeLabel:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://youtube.com/@HugoDecrypte", "HugoDecrypte"),
            ("https://youtube.com/c/SomeChannel", "SomeChannel"),
            ("https://youtube.com/user/old_name", "old name"),
            ("https://youtube.com/channel/UC123", "UC123"),
            ("https://youtube.com/@a-b_c", "a b c"),
            ("https://youtu.be/abc123", "YouTube channel"),
        ],
    )
    def test_label_from_url(self, url, expected):
        assert svc.youtube_label_from_url(url) == expected

    def test_is_youtube_detection(self):
        assert svc.is_youtube("https://www.youtube.com/@x") is True
        assert svc.is_youtube("https://youtu.be/abc") is True
        assert svc.is_youtube("https://example.com/feed.rss") is False

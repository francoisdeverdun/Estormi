"""Behavior tests for estormi_ingestion/shared/http_client.py.

Covers ``post_chunk`` retry/backoff contract (transient exceptions, 429 with
and without Retry-After, 5xx, non-transient 4xx pass-through, retry
exhaustion) and ``_backoff_with_jitter`` growth + cap + jitter bounds.

Every external boundary is mocked: ``http_client.httpx.post`` for the network,
``http_client.time.sleep`` so retries never actually wait, and
``http_client.random.uniform`` so backoff is deterministic.
"""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import httpx
import pytest

from estormi_ingestion.shared import http_client

pytestmark = pytest.mark.unit


def _resp(status_code: int, headers: dict[str, str] | None = None) -> MagicMock:
    """A stand-in httpx.Response with just the attributes post_chunk reads."""
    r = MagicMock(spec=httpx.Response)
    r.status_code = status_code
    r.headers = headers or {}
    return r


# --------------------------------------------------------------------------- #
# _backoff_with_jitter                                                          #
# --------------------------------------------------------------------------- #


def test_backoff_grows_exponentially_before_cap():
    # random.uniform(0, exp) returns its upper bound here, so we observe `exp`.
    with patch.object(http_client.random, "uniform", side_effect=lambda lo, hi: hi):
        assert http_client._backoff_with_jitter(1.0, 0) == 1.0
        assert http_client._backoff_with_jitter(1.0, 1) == 2.0
        assert http_client._backoff_with_jitter(1.0, 2) == 4.0
        assert http_client._backoff_with_jitter(1.0, 3) == 8.0


def test_backoff_capped_at_30s():
    with patch.object(http_client.random, "uniform", side_effect=lambda lo, hi: hi):
        # base*2**attempt would be huge; cap holds it at 30.
        assert http_client._backoff_with_jitter(1.0, 20) == 30.0


def test_backoff_jitter_called_with_full_window():
    with patch.object(http_client.random, "uniform", return_value=0.0) as uni:
        http_client._backoff_with_jitter(2.0, 2)
    # exp = min(2 * 2**2, 30) = 8.0; jitter spans [0, exp].
    uni.assert_called_once_with(0, 8.0)


# --------------------------------------------------------------------------- #
# post_chunk — happy path                                                       #
# --------------------------------------------------------------------------- #


def test_post_chunk_success_no_retry():
    ok = _resp(200)
    with (
        patch.object(http_client.httpx, "post", return_value=ok) as post,
        patch.object(http_client.time, "sleep") as sleep,
    ):
        result = http_client.post_chunk("http://x/ingest", {"a": 1})

    assert result is ok
    post.assert_called_once()
    # JSON body, merged content-type header, and timeout all flow through.
    _, kwargs = post.call_args
    assert kwargs["json"] == {"a": 1}
    assert kwargs["headers"]["Content-Type"] == "application/json"
    sleep.assert_not_called()


def test_post_chunk_merges_caller_headers():
    with (
        patch.object(http_client.httpx, "post", return_value=_resp(200)) as post,
        patch.object(http_client.time, "sleep"),
    ):
        http_client.post_chunk("http://x", {}, headers={"Authorization": "Bearer t"})

    _, kwargs = post.call_args
    assert kwargs["headers"]["Authorization"] == "Bearer t"
    assert kwargs["headers"]["Content-Type"] == "application/json"


def test_post_chunk_passes_through_non_transient_4xx():
    bad = _resp(404)
    with (
        patch.object(http_client.httpx, "post", return_value=bad) as post,
        patch.object(http_client.time, "sleep") as sleep,
    ):
        result = http_client.post_chunk("http://x", {})

    assert result is bad
    post.assert_called_once()
    sleep.assert_not_called()


# --------------------------------------------------------------------------- #
# post_chunk — retry then success                                               #
# --------------------------------------------------------------------------- #


def test_post_chunk_retries_transient_exception_then_succeeds():
    ok = _resp(200)
    with (
        patch.object(
            http_client.httpx, "post", side_effect=[httpx.ConnectError("boom"), ok]
        ) as post,
        patch.object(http_client.time, "sleep") as sleep,
        patch.object(http_client.random, "uniform", return_value=0.0),
    ):
        result = http_client.post_chunk("http://x", {})

    assert result is ok
    assert post.call_count == 2
    sleep.assert_called_once()


def test_post_chunk_retries_5xx_then_succeeds():
    ok = _resp(200)
    with (
        patch.object(http_client.httpx, "post", side_effect=[_resp(503), ok]) as post,
        patch.object(http_client.time, "sleep") as sleep,
        patch.object(http_client.random, "uniform", return_value=0.0),
    ):
        result = http_client.post_chunk("http://x", {})

    assert result is ok
    assert post.call_count == 2
    sleep.assert_called_once()


def test_post_chunk_retries_429_then_succeeds():
    ok = _resp(200)
    with (
        patch.object(http_client.httpx, "post", side_effect=[_resp(429), ok]) as post,
        patch.object(http_client.time, "sleep") as sleep,
        patch.object(http_client.random, "uniform", return_value=0.0),
    ):
        result = http_client.post_chunk("http://x", {})

    assert result is ok
    assert post.call_count == 2
    sleep.assert_called_once()


# --------------------------------------------------------------------------- #
# post_chunk — 429 Retry-After handling                                         #
# --------------------------------------------------------------------------- #


def test_post_chunk_429_honors_numeric_retry_after():
    ok = _resp(200)
    with (
        patch.object(
            http_client.httpx,
            "post",
            side_effect=[_resp(429, {"Retry-After": "5"}), ok],
        ),
        patch.object(http_client.time, "sleep") as sleep,
        patch.object(http_client.random, "uniform", return_value=0.0),
    ):
        http_client.post_chunk("http://x", {})

    # hint=5 beats jittered 0; clamped into [0.5, 30] -> 5.0.
    sleep.assert_called_once_with(5.0)


def test_post_chunk_429_retry_after_clamped_to_30s():
    ok = _resp(200)
    with (
        patch.object(
            http_client.httpx,
            "post",
            side_effect=[_resp(429, {"Retry-After": "999"}), ok],
        ),
        patch.object(http_client.time, "sleep") as sleep,
        patch.object(http_client.random, "uniform", return_value=0.0),
    ):
        http_client.post_chunk("http://x", {})

    sleep.assert_called_once_with(30.0)


def test_post_chunk_429_invalid_retry_after_falls_back_to_floor():
    ok = _resp(200)
    with (
        patch.object(
            http_client.httpx,
            "post",
            side_effect=[_resp(429, {"Retry-After": "soon"}), ok],
        ),
        patch.object(http_client.time, "sleep") as sleep,
        patch.object(http_client.random, "uniform", return_value=0.0),
    ):
        http_client.post_chunk("http://x", {})

    # Unparseable hint -> 0.0; jitter 0.0; clamped up to the 0.5s floor.
    sleep.assert_called_once_with(0.5)


# --------------------------------------------------------------------------- #
# post_chunk — retry exhaustion                                                 #
# --------------------------------------------------------------------------- #


def test_post_chunk_exhausts_retries_on_transient_exception_reraises():
    with (
        patch.object(http_client.httpx, "post", side_effect=httpx.ReadError("down")) as post,
        patch.object(http_client.time, "sleep") as sleep,
        patch.object(http_client.random, "uniform", return_value=0.0),
    ):
        with pytest.raises(httpx.ReadError):
            http_client.post_chunk("http://x", {}, retries=2)

    # retries=2 -> attempts 0,1,2 (3 posts); sleeps after the first two only.
    assert post.call_count == 3
    assert sleep.call_count == 2


def test_post_chunk_exhausts_retries_on_5xx_returns_last_response():
    last = _resp(500)
    with (
        patch.object(http_client.httpx, "post", return_value=last) as post,
        patch.object(http_client.time, "sleep") as sleep,
        patch.object(http_client.random, "uniform", return_value=0.0),
    ):
        result = http_client.post_chunk("http://x", {}, retries=2)

    # Non-transient outcome of exhaustion: the final 5xx is handed back.
    assert result is last
    assert post.call_count == 3
    assert sleep.call_count == 2


def test_post_chunk_exhausts_retries_on_429_returns_last_response():
    last = _resp(429)
    with (
        patch.object(http_client.httpx, "post", return_value=last) as post,
        patch.object(http_client.time, "sleep") as sleep,
        patch.object(http_client.random, "uniform", return_value=0.0),
    ):
        result = http_client.post_chunk("http://x", {}, retries=1)

    assert result is last
    assert post.call_count == 2
    assert sleep.call_count == 1


def test_post_chunk_zero_retries_does_not_sleep_on_5xx():
    last = _resp(500)
    with (
        patch.object(http_client.httpx, "post", return_value=last) as post,
        patch.object(http_client.time, "sleep") as sleep,
    ):
        result = http_client.post_chunk("http://x", {}, retries=0)

    assert result is last
    post.assert_called_once()
    sleep.assert_not_called()


def test_post_chunk_uses_backoff_growth_across_attempts():
    """Successive 5xx retries feed an increasing attempt index into backoff."""
    ok = _resp(200)
    with (
        patch.object(http_client.httpx, "post", side_effect=[_resp(500), _resp(500), ok]),
        patch.object(http_client.time, "sleep"),
        patch.object(http_client.random, "uniform", side_effect=lambda lo, hi: hi) as uni,
    ):
        http_client.post_chunk("http://x", {}, backoff=1.0)

    # attempt 0 -> exp 1.0, attempt 1 -> exp 2.0.
    assert uni.call_args_list == [call(0, 1.0), call(0, 2.0)]

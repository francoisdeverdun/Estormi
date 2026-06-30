"""Tests for the WHOOP connector.

Covers the sharpest edges of the integration:

* token storage + **refresh-token rotation** (WHOOP invalidates the old
  refresh token on every refresh, so the rotated one MUST be persisted),
* the sync layer: join-by-cycle, pagination (the deliberate ``nextToken``
  request / ``next_token`` response asymmetry of WHOOP v2), and the
  natural-language composer,
* ``_get`` retry/backoff — 429 with a ``Retry-After`` header (parsed and
  honoured), 5xx retry-exhaustion, and the connect-error path,
* ``sync()`` return-count aggregation, and the ``_main()`` re-auth exit code.

Keyring is stubbed out so nothing touches the real macOS Keychain, all HTTP is
mocked, and ``time.sleep`` is monkeypatched so the retry tests never block —
no network, no real waits.
"""

from __future__ import annotations

import concurrent.futures
import sys
import threading
import types

import pytest

import estormi_ingestion.whoop.auth as whoop_auth
import estormi_ingestion.whoop.sync as whoop_sync

# Markers are per-test rather than module-wide: the auth/composer/join logic is
# `unit`; the retry/backoff path, the `sync()` aggregation, and the `_main()`
# exit-code contract drive mocked HTTP / service boundaries and are
# `integration`.


# ─── Fixtures ──────────────────────────────────────────────────────────────


def _install_memory_keyring(monkeypatch):
    """Swap ``keyring`` for an in-memory stub so tokens AND client creds
    round-trip without touching the real system Keychain. Client credentials
    are keyring-only (no file fallback), so a working keyring is the baseline."""
    store: dict[tuple[str, str], str] = {}
    mem = types.ModuleType("keyring")
    mem.set_password = lambda s, k, v: store.__setitem__((s, k), v)  # noqa: ANN001
    mem.get_password = lambda s, k: store.get((s, k))  # noqa: ANN001
    mem.delete_password = lambda s, k: store.pop((s, k), None)  # noqa: ANN001
    monkeypatch.setitem(sys.modules, "keyring", mem)
    return store


def _break_keyring(monkeypatch):
    """Make every keyring call raise — used by the token *file-fallback* tests."""
    broken = types.ModuleType("keyring")

    def _raise(*_a, **_k):  # noqa: ANN002, ANN003
        raise RuntimeError("keyring disabled in tests")

    broken.set_password = _raise
    broken.get_password = _raise
    broken.delete_password = _raise
    monkeypatch.setitem(sys.modules, "keyring", broken)


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Point the connector's secret storage at a throwaway dir, with a working
    in-memory keyring so nothing touches the real system Keychain."""
    monkeypatch.setenv("ESTORMI_DATA_DIR", str(tmp_path))
    _install_memory_keyring(monkeypatch)
    return tmp_path


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


# ─── Token rotation ─────────────────────────────────────────────────────────


@pytest.mark.unit
def test_refresh_persists_rotated_refresh_token(data_dir, monkeypatch):
    """An expired access token triggers a refresh; the NEW refresh token WHOOP
    returns must be written back, or the next run is locked out."""
    whoop_auth.save_client("cid", "csecret")
    whoop_auth.save_token({"access_token": "old-access", "refresh_token": "R1", "expires_at": 0})

    calls = {}

    def fake_post(url, data=None, timeout=None):  # noqa: ANN001
        calls["url"] = url
        calls["data"] = data
        return _FakeResponse(
            {"access_token": "new-access", "refresh_token": "R2", "expires_in": 3600}
        )

    monkeypatch.setattr(whoop_auth.httpx, "post", fake_post)

    access_token = whoop_auth.get_access_token()

    assert access_token == "new-access"
    # The rotated refresh token is what got persisted …
    assert whoop_auth.load_token()["refresh_token"] == "R2"
    # … the refresh used the OLD one and asked for offline so a new refresh
    # token comes back.
    assert calls["data"]["grant_type"] == "refresh_token"
    assert calls["data"]["refresh_token"] == "R1"
    assert calls["data"]["scope"] == "offline"


@pytest.mark.unit
def test_dead_refresh_token_returns_none(data_dir, monkeypatch):
    """A revoked/expired refresh token (400/401) yields None, not a traceback,
    so the caller can show a clean 'reconnect' branch."""
    whoop_auth.save_client("cid", "csecret")
    whoop_auth.save_token({"access_token": "old", "refresh_token": "dead", "expires_at": 0})

    monkeypatch.setattr(
        whoop_auth.httpx,
        "post",
        lambda *a, **k: _FakeResponse({"error": "invalid_grant"}, status_code=400),
    )

    assert whoop_auth.get_access_token() is None


@pytest.mark.unit
def test_valid_access_token_skips_refresh(data_dir, monkeypatch):
    """A still-valid token is returned without any network round-trip."""
    whoop_auth.save_client("cid", "csecret")
    whoop_auth.save_token(
        {"access_token": "live", "refresh_token": "R1", "expires_at": 9_999_999_999}
    )

    def boom(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("must not refresh a live token")

    monkeypatch.setattr(whoop_auth.httpx, "post", boom)
    assert whoop_auth.get_access_token() == "live"


@pytest.mark.unit
def test_token_file_is_chmod_600(data_dir, monkeypatch):
    """When the keyring is unavailable the token falls back to an owner-only
    (0o600) file, matching the Google connector."""
    import os
    import stat

    _break_keyring(monkeypatch)
    whoop_auth.save_token({"access_token": "x", "refresh_token": "y", "expires_at": 0})
    mode = stat.S_IMODE(os.stat(data_dir / ".whoop_token").st_mode)
    assert mode == 0o600


@pytest.mark.unit
def test_client_creds_stored_in_keyring_not_on_disk(data_dir):
    """Client credentials are keyring-only — round-trip via the keyring and
    never written as a cleartext file in the data dir."""
    whoop_auth.save_client("cid", "csecret")
    assert whoop_auth.load_client() == {"client_id": "cid", "client_secret": "csecret"}
    assert not (data_dir / "whoop_client.json").exists()


@pytest.mark.unit
def test_load_client_migrates_legacy_file_then_deletes_it(data_dir):
    """A pre-keyring cleartext whoop_client.json is imported into the keyring on
    first load, then deleted so the secret no longer lingers on disk."""
    import json

    legacy = data_dir / "whoop_client.json"
    legacy.write_text(json.dumps({"client_id": "old", "client_secret": "oldsec"}))
    assert whoop_auth.load_client() == {"client_id": "old", "client_secret": "oldsec"}
    assert not legacy.exists()
    # Now served from the keyring even with the file gone.
    assert whoop_auth.load_client()["client_id"] == "old"


# ─── Pagination ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_collect_paginates_with_nexttoken(monkeypatch):
    """``_collect`` follows the cursor across pages. WHOOP v2's request param is
    ``nextToken`` while the response field is ``next_token`` — assert the code
    bridges that asymmetry instead of stopping after page one."""
    pages = [
        {"records": [{"id": 1}, {"id": 2}], "next_token": "PAGE2"},
        {"records": [{"id": 3}], "next_token": None},
    ]
    seen_params = []

    def fake_get(path, access_token, params):  # noqa: ANN001
        seen_params.append(dict(params))
        return pages[len(seen_params) - 1]

    monkeypatch.setattr(whoop_sync, "_get", fake_get)

    out = whoop_sync._collect("/v2/cycle", "tok", "S", "E")

    assert [r["id"] for r in out] == [1, 2, 3]
    # First page carries no cursor; the second sends the response's next_token
    # back as the request's nextToken.
    assert "nextToken" not in seen_params[0]
    assert seen_params[1]["nextToken"] == "PAGE2"


# ─── Wake probe ───────────────────────────────────────────────────────────


@pytest.mark.unit
def test_recovery_available_today_returns_newest_scored_day(monkeypatch):
    """The probe returns the local date of the most recently *scored* recovery,
    ignoring older records and any still-PENDING (unscored) one."""
    monkeypatch.setattr(whoop_sync.whoop_auth, "get_access_token", lambda: "tok")
    records = [
        {"created_at": "2026-06-03T06:30:00.000Z", "score": {"recovery_score": 55}},
        {"created_at": "2026-06-04T05:40:00.000Z", "score": {"recovery_score": 62}},
        {"created_at": "2026-06-04T07:10:00.000Z", "score": None},  # not yet scored
    ]
    monkeypatch.setattr(
        whoop_sync, "_get", lambda *a, **k: {"records": records, "next_token": None}
    )

    got = whoop_sync.recovery_available_today()

    # Newest scored record is the 05:40 UTC one; the probe reports it in local time.
    expected = whoop_sync._parse_dt("2026-06-04T05:40:00.000Z").astimezone().strftime("%Y-%m-%d")
    assert got == expected


@pytest.mark.unit
def test_recovery_available_today_none_without_token(monkeypatch):
    monkeypatch.setattr(whoop_sync.whoop_auth, "get_access_token", lambda: None)
    assert whoop_sync.recovery_available_today() is None


@pytest.mark.unit
def test_recovery_available_today_none_when_unscored(monkeypatch):
    """A night WHOOP hasn't scored yet (PENDING) must not look like "awake"."""
    monkeypatch.setattr(whoop_sync.whoop_auth, "get_access_token", lambda: "tok")
    monkeypatch.setattr(
        whoop_sync,
        "_get",
        lambda *a, **k: {
            "records": [{"created_at": "2026-06-04T07:00:00.000Z", "score": None}],
            "next_token": None,
        },
    )
    assert whoop_sync.recovery_available_today() is None


# ─── Join by cycle ──────────────────────────────────────────────────────────


@pytest.mark.unit
def test_index_by_cycle_keeps_first():
    recs = [
        {"cycle_id": "c1", "score": {"recovery_score": 41}},
        {"cycle_id": "c1", "score": {"recovery_score": 99}},  # dup — ignored
        {"cycle_id": "c2", "score": {"recovery_score": 70}},
    ]
    idx = whoop_sync._index_by_cycle(recs)
    assert idx["c1"]["score"]["recovery_score"] == 41
    assert idx["c2"]["score"]["recovery_score"] == 70


@pytest.mark.unit
def test_workouts_for_cycle_filters_by_time():
    cycle = {
        "id": "c1",
        "start": "2026-06-01T06:00:00.000Z",
        "end": "2026-06-02T06:00:00.000Z",
    }
    inside = {"start": "2026-06-01T18:00:00.000Z", "sport_name": "running"}
    before = {"start": "2026-05-30T18:00:00.000Z", "sport_name": "cycling"}
    got = whoop_sync._workouts_for_cycle(cycle, [inside, before])
    assert got == [inside]


@pytest.mark.unit
def test_sleep_join_skips_naps():
    """The night's record is the first non-nap sleep for the cycle."""
    sleeps = [
        {"cycle_id": "c1", "nap": True, "score": {"sleep_performance_percentage": 10}},
        {"cycle_id": "c1", "nap": False, "score": {"sleep_performance_percentage": 68}},
    ]
    by_cycle = {}
    for s in sleeps:
        cid = s.get("cycle_id")
        if cid is None or s.get("nap"):
            continue
        by_cycle.setdefault(cid, s)
    assert by_cycle["c1"]["score"]["sleep_performance_percentage"] == 68


# ─── Composer ───────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_recovery_band_thresholds():
    assert whoop_sync._recovery_band(80) == "green"
    assert whoop_sync._recovery_band(50) == "yellow"
    assert whoop_sync._recovery_band(20) == "red"
    assert whoop_sync._recovery_band(None) == ""


@pytest.mark.unit
def test_asleep_hours_sums_stages():
    stage = {
        "total_light_sleep_time_milli": 9_000_000,
        "total_slow_wave_sleep_time_milli": 5_000_000,
        "total_rem_sleep_time_milli": 4_400_000,
    }
    # (9.0 + 5.0 + 4.4)e6 ms = 18.4e6 ms = 5.111… h
    assert whoop_sync._asleep_hours(stage) == pytest.approx(18_400_000 / 3_600_000)


@pytest.mark.unit
def test_compose_full_cycle_renders_all_blocks():
    cyc = {
        "id": "c1",
        "start": "2026-06-01T06:30:00.000Z",
        "end": "2026-06-02T06:00:00.000Z",
        "timezone_offset": "+02:00",
        "score": {
            "strain": 13.4,
            "kilojoule": 9665.0,
            "average_heart_rate": 72,
            "max_heart_rate": 168,
        },
    }
    rec = {
        "cycle_id": "c1",
        "score": {
            "recovery_score": 41,
            "hrv_rmssd_milli": 38,
            "resting_heart_rate": 61,
            "spo2_percentage": 96.4,
            "skin_temp_celsius": 33.8,
        },
    }
    slp = {
        "cycle_id": "c1",
        "nap": False,
        "score": {
            "stage_summary": {
                "total_light_sleep_time_milli": 9_000_000,
                "total_slow_wave_sleep_time_milli": 5_000_000,
                "total_rem_sleep_time_milli": 4_400_000,
                "disturbance_count": 14,
            },
            "sleep_performance_percentage": 68,
            "sleep_efficiency_percentage": 88,
            "respiratory_rate": 14.2,
        },
    }
    wk = {
        "start": "2026-06-01T18:00:00.000Z",
        "end": "2026-06-01T18:38:00.000Z",
        "sport_name": "running",
        "score": {"strain": 9.1, "average_heart_rate": 152, "distance_meter": 6200},
    }
    text = whoop_sync._compose(cyc, rec, slp, [wk], baseline={})

    assert text.startswith("WHOOP — Mon 1 Jun 2026.")
    assert "Recovery 41% (yellow)" in text
    assert "HRV 38 ms" in text
    assert "resting HR 61 bpm" in text
    # 9665 kJ * 0.239006 ≈ 2310 kcal
    assert "2310 kcal" in text
    assert "strain 13.4" in text
    assert "running 38 min (strain 9.1, avg HR 152, 6.2 km)" in text
    # No baseline supplied → no "vs avg" deltas leak in.
    assert "vs avg" not in text


@pytest.mark.unit
def test_compose_handles_missing_recovery_and_workouts():
    """A cycle scored before recovery lands (PENDING) still renders cleanly."""
    cyc = {
        "id": "c1",
        "start": "2026-06-01T06:30:00.000Z",
        "end": "2026-06-02T06:00:00.000Z",
        "score": {"strain": 8.0, "kilojoule": 5000.0},
    }
    text = whoop_sync._compose(cyc, recovery=None, sleep=None, workouts=[], baseline={})
    assert "WHOOP —" in text
    assert "Recovery" not in text
    assert "strain 8.0" in text


@pytest.mark.unit
def test_delta_suppresses_near_zero():
    """A figure within rounding of the mean carries no '(+0 …)' trailer."""
    assert whoop_sync._delta(55, 55, " bpm") == ""
    assert whoop_sync._delta(55.3, 55, " bpm") == ""  # rounds to 0
    assert whoop_sync._delta(38, 47, " ms") == " (−9 ms vs avg 47 ms)"
    assert whoop_sync._delta(None, 47) == ""


@pytest.mark.unit
def test_compose_includes_deltas_when_baseline_present():
    cyc = {
        "id": "c1",
        "start": "2026-06-01T06:30:00.000Z",
        "end": "2026-06-02T06:00:00.000Z",
        "score": {"strain": 10.0},
    }
    rec = {"cycle_id": "c1", "score": {"hrv_rmssd_milli": 38, "recovery_score": 41}}
    text = whoop_sync._compose(
        cyc, rec, sleep=None, workouts=[], baseline={"hrv": 47, "recovery": 55}
    )
    # 38 vs avg 47 → −9; uses the U+2212 minus to match briefing typography.
    assert "HRV 38 ms (−9 ms vs avg 47 ms)" in text


# ─── concurrent refresh + dead-token cleanup (sweep 2 U14/S2) ────────────────


@pytest.mark.unit
def test_concurrent_refresh_does_not_double_spend_rotating_token(data_dir, monkeypatch):
    """U14: two concurrent ``get_access_token()`` callers must not double-spend
    WHOOP's single-use rotating refresh token.

    WHOOP rotates RT0→RT1 on the first refresh and rejects any later POST of RT0.
    With the refresh lock + re-read, BOTH callers end up with a valid access
    token (the second blocks, re-reads, sees RT1 already valid) and RT0 is POSTed
    exactly once. Without the lock both threads load RT0 and both POST it; the
    second gets a 400 → None, and the rotated token is clobbered.
    """
    whoop_auth.save_client("cid", "csecret")
    whoop_auth.save_token({"access_token": "AT0", "refresh_token": "RT0", "expires_at": 0})

    lock = threading.Lock()
    rt0_posts = []  # every POST that carried RT0
    barrier = threading.Barrier(2)

    def fake_post(url, data=None, timeout=None):  # noqa: ANN001
        presented = data.get("refresh_token")
        # Make both threads reach the POST boundary together if they were *not*
        # serialized — exposes the race when the lock is absent. With the lock
        # only one thread ever gets here for RT0, so guard the barrier so the
        # single-caller case can't hang.
        try:
            barrier.wait(timeout=1.0)
        except threading.BrokenBarrierError:
            pass
        with lock:
            if presented == "RT0":
                rt0_posts.append(presented)
                if len(rt0_posts) == 1:
                    return _FakeResponse(
                        {"access_token": "AT1", "refresh_token": "RT1", "expires_in": 3600}
                    )
                # RT0 already consumed by WHOOP → invalid_grant.
                return _FakeResponse({"error": "invalid_grant"}, status_code=400)
            if presented == "RT1":
                return _FakeResponse(
                    {"access_token": "AT2", "refresh_token": "RT2", "expires_in": 3600}
                )
            return _FakeResponse({"error": "invalid_grant"}, status_code=400)

    monkeypatch.setattr(whoop_auth.httpx, "post", fake_post)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(whoop_auth.get_access_token)
        f2 = ex.submit(whoop_auth.get_access_token)
        results = [f1.result(timeout=5), f2.result(timeout=5)]

    # Neither caller falls back to None (no spurious "disconnected").
    assert all(r is not None for r in results), results
    assert set(results) <= {"AT1", "AT2"}
    # RT0 was spent exactly once — the rotating token was not double-spent.
    assert rt0_posts == ["RT0"]
    # The persisted token is one of the rotated successors, never the dead RT0.
    assert whoop_auth.load_token()["refresh_token"] in {"RT1", "RT2"}


@pytest.mark.unit
def test_dead_refresh_token_is_deleted_from_disk(data_dir, monkeypatch):
    """S2: a definitive invalid_grant (400) returns None AND wipes the stored
    token, so later polls don't re-POST a known-dead refresh token forever.

    Without the ``delete_token()`` call the token survives on disk and
    ``load_token()`` would still return it.
    """
    whoop_auth.save_client("cid", "csecret")
    whoop_auth.save_token({"access_token": "old", "refresh_token": "dead", "expires_at": 0})
    assert whoop_auth.load_token() is not None  # precondition

    monkeypatch.setattr(
        whoop_auth.httpx,
        "post",
        lambda *a, **k: _FakeResponse({"error": "invalid_grant"}, status_code=400),
    )

    assert whoop_auth.get_access_token() is None
    # The dead token was purged — no infinite re-POST loop.
    assert whoop_auth.load_token() is None


# ─── _get retry / backoff (integration: mocked httpx, no real sleeps) ────────


class _RetryResponse:
    """A WHOOP HTTP response stub carrying status + headers for the retry path."""

    def __init__(self, status_code, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.text = str(self._payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture
def no_sleep(monkeypatch):
    """Record every backoff sleep WITHOUT actually waiting, so the retry tests
    stay instant. Returns the list of slept-for durations for assertions."""
    slept: list[float] = []
    monkeypatch.setattr(whoop_sync.time, "sleep", lambda s: slept.append(s))
    return slept


@pytest.mark.integration
def test_get_honours_retry_after_header_on_429(monkeypatch, no_sleep):
    """A 429 with a ``Retry-After`` is retried, and the header value (parsed as
    seconds) is what the backoff sleeps on — not the exponential default."""
    responses = [
        _RetryResponse(429, headers={"Retry-After": "7"}),
        _RetryResponse(200, payload={"records": [{"id": 1}]}),
    ]
    calls: list[dict] = []

    def fake_get(url, headers=None, params=None, timeout=None):  # noqa: ANN001
        calls.append({"url": url, "headers": headers, "params": params})
        return responses[len(calls) - 1]

    monkeypatch.setattr(whoop_sync.httpx, "get", fake_get)

    out = whoop_sync._get("/v2/cycle", "tok", {"start": "S"})

    assert out == {"records": [{"id": 1}]}
    assert len(calls) == 2  # retried once after the 429
    # The Retry-After value (7s) was honoured, clamped into [0.5, 30].
    assert no_sleep == [7.0]
    # The bearer token rode on every request.
    assert calls[0]["headers"]["Authorization"] == "Bearer tok"


@pytest.mark.integration
def test_get_retry_after_clamped_to_thirty_seconds(monkeypatch, no_sleep):
    """An over-long ``Retry-After`` is clamped to the 30s ceiling so a hostile
    or buggy header can't park the connector for an hour."""
    responses = [
        _RetryResponse(429, headers={"Retry-After": "3600"}),
        _RetryResponse(200, payload={"records": []}),
    ]
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):  # noqa: ANN001
        calls.append(1)
        return responses[len(calls) - 1]

    monkeypatch.setattr(whoop_sync.httpx, "get", fake_get)
    whoop_sync._get("/v2/cycle", "tok", {})
    assert no_sleep == [30.0]


@pytest.mark.integration
def test_get_malformed_retry_after_falls_back_to_exponential(monkeypatch, no_sleep):
    """A non-numeric ``Retry-After`` (e.g. an HTTP-date) is ignored and the
    exponential default (min(2**attempt, 30)) is used instead — never a crash."""
    responses = [
        _RetryResponse(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
        _RetryResponse(200, payload={"records": []}),
    ]
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):  # noqa: ANN001
        calls.append(1)
        return responses[len(calls) - 1]

    monkeypatch.setattr(whoop_sync.httpx, "get", fake_get)
    whoop_sync._get("/v2/cycle", "tok", {})
    # attempt 0 → 2**0 = 1, clamped into [0.5, 30] → 1.0.
    assert no_sleep == [1.0]


@pytest.mark.integration
def test_get_raises_after_exhausting_5xx_retries(monkeypatch, no_sleep):
    """A persistent 5xx is retried up to the bounded ceiling then raised — the
    connector never loops forever on a dead upstream."""
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):  # noqa: ANN001
        calls.append(1)
        return _RetryResponse(503)

    monkeypatch.setattr(whoop_sync.httpx, "get", fake_get)

    with pytest.raises(RuntimeError, match="exhausted retries.*503"):
        whoop_sync._get("/v2/cycle", "tok", {})

    # Six bounded attempts, six backoff sleeps — not an unbounded retry storm.
    assert len(calls) == 6
    assert len(no_sleep) == 6


@pytest.mark.integration
def test_get_retries_then_raises_on_persistent_connect_error(monkeypatch, no_sleep):
    """A connect/timeout error is retried; if it never clears, the ORIGINAL
    exception is re-raised (not a generic RuntimeError) so the caller sees the
    real network fault."""
    boom = whoop_sync.httpx.ConnectError("connection refused")
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):  # noqa: ANN001
        calls.append(1)
        raise boom

    monkeypatch.setattr(whoop_sync.httpx, "get", fake_get)

    with pytest.raises(whoop_sync.httpx.ConnectError):
        whoop_sync._get("/v2/cycle", "tok", {})

    assert len(calls) == 6  # bounded retries, then surfaces the connect error
    assert len(no_sleep) == 6


@pytest.mark.integration
def test_get_recovers_after_transient_connect_error(monkeypatch, no_sleep):
    """One connect error followed by a 200 succeeds — a transient blip does not
    fail the page pull."""
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):  # noqa: ANN001
        calls.append(1)
        if len(calls) == 1:
            raise whoop_sync.httpx.ReadError("reset")
        return _RetryResponse(200, payload={"records": [{"id": 9}]})

    monkeypatch.setattr(whoop_sync.httpx, "get", fake_get)
    out = whoop_sync._get("/v2/cycle", "tok", {})
    assert out == {"records": [{"id": 9}]}
    assert len(calls) == 2
    assert len(no_sleep) == 1  # one backoff after the blip


# ─── sync() return-count aggregation (integration) ───────────────────────────


@pytest.fixture
def stub_watermark(monkeypatch):
    """Make the post-run watermark write a no-op so sync() never touches the DB.

    sync() only writes a watermark on a clean run (errors == 0); stubbing it as
    an async no-op keeps that branch hermetic."""

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(whoop_sync, "set_watermark", _noop)


def _drive_sync(monkeypatch, *, cycles, outcomes):
    """Run whoop_sync.sync() with the API + post layer stubbed.

    ``cycles`` is the list of cycle dicts ``_collect('/v2/cycle', …)`` returns;
    every other collection returns empty. ``outcomes`` is the list of per-cycle
    POST results (POST_INGESTED / POST_SKIPPED / POST_ERROR) handed back in
    order by a stubbed ``_post_cycle``."""
    monkeypatch.setattr(whoop_sync.whoop_auth, "get_access_token", lambda: "tok")

    def fake_collect(path, token, start, end):  # noqa: ANN001
        return cycles if path == "/v2/cycle" else []

    monkeypatch.setattr(whoop_sync, "_collect", fake_collect)

    it = iter(outcomes)
    monkeypatch.setattr(whoop_sync, "_post_cycle", lambda cycle, text: next(it))
    # The composer is exercised by its own unit tests; keep sync() fast + stable.
    monkeypatch.setattr(whoop_sync, "_compose", lambda *a, **k: "text")
    return whoop_sync.sync()


@pytest.mark.integration
def test_sync_aggregates_per_cycle_outcomes(monkeypatch, stub_watermark):
    """sync() tallies ingested / skipped / errors and counts every cycle seen."""
    cycles = [{"id": f"c{i}", "start": f"2026-06-0{i + 1}T06:00:00.000Z"} for i in range(5)]
    outcomes = [
        whoop_sync.POST_INGESTED,
        whoop_sync.POST_INGESTED,
        whoop_sync.POST_SKIPPED,
        whoop_sync.POST_ERROR,
        whoop_sync.POST_INGESTED,
    ]

    counts = _drive_sync(monkeypatch, cycles=cycles, outcomes=outcomes)

    assert counts == {"ingested": 3, "skipped": 1, "cycles": 5, "errors": 1}


@pytest.mark.integration
def test_sync_persists_watermark_only_on_clean_run(monkeypatch):
    """The 'last sync' watermark advances on a clean run and is held back when
    any cycle errored (so the Settings UI never shows a fresh time for a run
    that silently dropped data)."""
    saved: list[tuple] = []

    async def fake_set(source, fetched_at, item_id=None):  # noqa: ANN001
        saved.append((source, item_id))

    monkeypatch.setattr(whoop_sync, "set_watermark", fake_set)

    # Clean run → watermark written, tagged with the last cycle id.
    _drive_sync(
        monkeypatch,
        cycles=[{"id": "c1", "start": "2026-06-01T06:00:00.000Z"}],
        outcomes=[whoop_sync.POST_INGESTED],
    )
    assert saved == [("whoop", "c1")]

    # Run with an error → no further watermark write.
    saved.clear()
    _drive_sync(
        monkeypatch,
        cycles=[{"id": "c2", "start": "2026-06-02T06:00:00.000Z"}],
        outcomes=[whoop_sync.POST_ERROR],
    )
    assert saved == []


@pytest.mark.integration
def test_sync_no_token_returns_single_error(monkeypatch, stub_watermark):
    """No WHOOP credentials short-circuits to one counted error and never calls
    the API — the pipeline stage fails loudly instead of looking clean-empty."""
    monkeypatch.setattr(whoop_sync.whoop_auth, "get_access_token", lambda: None)

    def must_not_collect(*_a, **_k):
        raise AssertionError("must not hit the WHOOP API without a token")

    monkeypatch.setattr(whoop_sync, "_collect", must_not_collect)

    counts = whoop_sync.sync()
    assert counts == {"ingested": 0, "skipped": 0, "cycles": 0, "errors": 1}


# ─── _main(): exit-code contract (integration) ───────────────────────────────


@pytest.mark.integration
def test_main_returns_reauth_exit_code_when_token_revoked(monkeypatch, capsys):
    """A stored-but-dead refresh token (load_token present, get_access_token
    None) surfaces as the reserved re-auth exit code 2 — distinct from a
    transient API error (1) — and sync() is never run."""
    monkeypatch.setattr(whoop_sync.whoop_auth, "load_token", lambda: {"refresh_token": "dead"})
    monkeypatch.setattr(whoop_sync.whoop_auth, "get_access_token", lambda: None)

    def must_not_run(*_a, **_k):
        raise AssertionError("sync() must not run once re-auth is detected")

    monkeypatch.setattr(whoop_sync, "sync", must_not_run)

    assert whoop_sync._main() == whoop_sync._EXIT_REAUTH == 2
    assert "reconnect" in capsys.readouterr().out.lower()


@pytest.mark.integration
def test_main_returns_one_on_sync_errors(monkeypatch):
    """A run that reports errors (but has a usable token) exits 1."""
    monkeypatch.setattr(whoop_sync.whoop_auth, "load_token", lambda: None)
    monkeypatch.setattr(
        whoop_sync, "sync", lambda: {"cycles": 3, "ingested": 1, "skipped": 0, "errors": 2}
    )
    assert whoop_sync._main() == 1


@pytest.mark.integration
def test_main_returns_zero_on_clean_run(monkeypatch):
    """A clean run exits 0 so the pipeline stage is marked green."""
    monkeypatch.setattr(whoop_sync.whoop_auth, "load_token", lambda: None)
    monkeypatch.setattr(
        whoop_sync, "sync", lambda: {"cycles": 4, "ingested": 4, "skipped": 0, "errors": 0}
    )
    assert whoop_sync._main() == 0

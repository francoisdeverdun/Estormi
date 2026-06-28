"""Tests for llm_local.py — model path resolution and the config ladder."""

from __future__ import annotations

import os

import pytest

from memory_core.llm_local import (
    _LLM_LADDER,
    _MODEL_FILES,
    _MODEL_REPOS,
    _ROLE_DEFAULT_TIER,
    _ROLE_SETTING_KEY,
    _TIER_LOAD_OPTS,
    MODEL_CATALOG,
    _engine_role,
    _model_path,
    model_file_path,
    role_default_tier,
    selected_tier_for,
)

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _fresh_llm_locks():
    """Rebind llm_local's module-level asyncio locks to each test's event loop.

    ``_infer_lock`` / ``_llm_lock`` are created once at import and bind lazily to
    the first loop they're awaited on; pytest-asyncio runs each test on a fresh
    function-scoped loop, so a lock reused across tests raises "bound to a
    different event loop". Re-creating them per test keeps the concurrency tests
    independent of run order."""
    import asyncio

    from memory_core import llm_local

    llm_local._infer_lock = asyncio.Lock()
    llm_local._llm_lock = asyncio.Lock()
    yield


# ── _MODEL_FILES ──────────────────────────────────────────────────────────────


class TestModelFiles:
    def test_all_keys_present(self):
        # Ministral 3 14B is the sole shipped local tier after the bench-off.
        assert "ministral3-14b" in _MODEL_FILES

    def test_all_values_are_gguf(self):
        for key, filename in _MODEL_FILES.items():
            assert filename.endswith(".gguf"), f"{key} → {filename} is not .gguf"

    def test_catalog_in_sync(self):
        """The model maps must all key on the same tiers — a missing entry in
        any of them is a silent half-added model (no repo, no chat format, or
        no UI row). local_only tiers are produced on-device and rightly have
        no _MODEL_REPOS entry."""
        tiers = set(_MODEL_FILES)
        downloadable = {t for t in tiers if not MODEL_CATALOG.get(t, {}).get("local_only")}
        assert set(_MODEL_REPOS) == downloadable
        assert set(_TIER_LOAD_OPTS) == tiers
        assert set(MODEL_CATALOG) == tiers

    async def test_download_refuses_local_only_tier(self, monkeypatch, tmp_path):
        import pytest as _pytest

        from memory_core import llm_local

        monkeypatch.setenv("ESTORMI_DATA_DIR", str(tmp_path))
        with _pytest.raises(RuntimeError, match="local-only"):
            await llm_local.download_model("ministral3-14b-estormi")

    def test_ministral3_uses_embedded_template(self):
        # Ministral 3 loads with chat_format=None so llama.cpp uses the GGUF's
        # own template — forcing a legacy format reproduced the all-"unrelated"
        # tokenizer-mismatch regression seen on Nemo.
        assert _TIER_LOAD_OPTS["ministral3-14b"]["chat_format"] is None


# ── per-engine selection ───────────────────────────────────────────────────────


class TestEngineSelection:
    def test_engine_role_from_env(self, monkeypatch):
        monkeypatch.setenv("ESTORMI_ENGINE_ROLE", "briefing")
        assert _engine_role() == "briefing"

    def test_engine_role_defaults_to_briefing(self, monkeypatch):
        monkeypatch.delenv("ESTORMI_ENGINE_ROLE", raising=False)
        assert _engine_role() == "briefing"
        monkeypatch.setenv("ESTORMI_ENGINE_ROLE", "bogus")
        assert _engine_role() == "briefing"

    def test_role_defaults_match_recommendation(self):
        # The default the user signed off on for the briefing engine.
        assert role_default_tier("briefing") == "ministral3-14b"

    async def test_selected_tier_reads_per_engine_key(self, db):
        from estormi_server.storage import tools

        await db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)",
            (_ROLE_SETTING_KEY["briefing"], "ministral3-14b"),
        )
        await db.commit()
        original = getattr(tools, "sqlite_conn", None)
        tools.sqlite_conn = lambda: db
        try:
            assert await selected_tier_for("briefing") == "ministral3-14b"
        finally:
            if original is not None:
                tools.sqlite_conn = original

    async def test_selected_tier_falls_back_to_role_default(self, db):
        from estormi_server.storage import tools

        original = getattr(tools, "sqlite_conn", None)
        tools.sqlite_conn = lambda: db  # empty settings table
        try:
            assert await selected_tier_for("briefing") == _ROLE_DEFAULT_TIER["briefing"]
        finally:
            if original is not None:
                tools.sqlite_conn = original

    def test_model_file_path_canonical(self, monkeypatch):
        # Catalog install-state reports the canonical per-tier path.
        monkeypatch.setenv("ESTORMI_DATA_DIR", "/test/data")
        p = model_file_path("ministral3-14b")
        assert p.endswith(_MODEL_FILES["ministral3-14b"])
        assert p.startswith(os.path.join("/test/data", "models"))


# ── _model_path ───────────────────────────────────────────────────────────────


class TestModelPath:
    async def test_default_fallback(self, monkeypatch):
        """Falls back to the role default when the settings lookup fails.

        A roleless call (no ``ESTORMI_ENGINE_ROLE``) resolves as the briefing
        engine, whose default tier picks the canonical model path.
        """
        monkeypatch.delenv("ESTORMI_ENGINE_ROLE", raising=False)
        monkeypatch.setenv("ESTORMI_DATA_DIR", "/test/data")

        # Make the DB lookup raise
        from estormi_server.storage import tools

        original = getattr(tools, "sqlite_conn", None)
        tools.sqlite_conn = lambda: (_ for _ in ()).throw(Exception("no db"))
        try:
            result = await _model_path()
            expected_tier = role_default_tier("briefing")
            assert result == os.path.join("/test/data", "models", _MODEL_FILES[expected_tier])
        finally:
            if original is not None:
                tools.sqlite_conn = original


# ── config ladder ─────────────────────────────────────────────────────────────


class TestStreamDownload:
    """The resumable streaming downloader that replaced hf_hub_download."""

    def test_resumes_after_mid_stream_stall(self, tmp_path, monkeypatch):
        import httpx

        from memory_core import llm_local

        full = b"GGUF" + b"ABCDEF"  # 10 bytes, valid magic
        part = tmp_path / "m.gguf.part"
        final = tmp_path / "m.gguf"

        class _Head:
            headers = {"content-length": str(len(full))}

        class _Stream:
            def __init__(self, start, stall):
                self.status_code = 206
                self.headers = {}
                self._start, self._stall = start, stall

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def iter_bytes(self, _n):
                yield full[self._start : self._start + 4]
                if self._stall:
                    raise httpx.ReadTimeout("simulated CDN stall")
                yield full[self._start + 4 :]

        calls = {"n": 0}

        def _fake_stream(_method, _url, headers=None, **_k):
            start = 0
            if headers and "Range" in headers:
                start = int(headers["Range"].split("=")[1].split("-")[0])
            calls["n"] += 1
            return _Stream(start, stall=(calls["n"] == 1))

        monkeypatch.setattr(httpx, "head", lambda *a, **k: _Head())
        monkeypatch.setattr(httpx, "stream", _fake_stream)

        llm_local._stream_gguf("https://example/m.gguf", part, final)

        # Stalled once mid-stream, resumed via Range, assembled the full file,
        # validated the GGUF magic, and atomically promoted .part → final.
        assert calls["n"] == 2
        assert final.read_bytes() == full
        assert not part.exists()

    def test_rejects_non_gguf(self, tmp_path, monkeypatch):
        import httpx

        from memory_core import llm_local

        body = b"<html>404</html>"
        part = tmp_path / "m.gguf.part"
        final = tmp_path / "m.gguf"

        class _Stream:
            status_code = 200
            headers = {"content-length": str(len(body))}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def iter_bytes(self, _n):
                yield body

        monkeypatch.setattr(httpx, "head", lambda *a, **k: type("H", (), {"headers": {}})())
        monkeypatch.setattr(httpx, "stream", lambda *a, **k: _Stream())

        with pytest.raises(RuntimeError, match="not a valid GGUF"):
            llm_local._stream_gguf("https://example/m.gguf", part, final)
        assert not final.exists()

    def test_verifies_sha256_digest(self, tmp_path, monkeypatch):
        """The content pin: a wrong expected_sha256 is rejected (and the .part
        deleted so a retry re-downloads clean); the right digest promotes the
        file. Guards against a force-pushed branch / compromised mirror serving
        a different (maliciously-tuned) GGUF that still has the magic bytes."""
        import hashlib

        import httpx

        from memory_core import llm_local

        full = b"GGUF" + b"weights-bytes"
        good = hashlib.sha256(full).hexdigest()

        class _Stream:
            status_code = 200
            headers = {"content-length": str(len(full))}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def iter_bytes(self, _n):
                yield full

        monkeypatch.setattr(httpx, "head", lambda *a, **k: type("H", (), {"headers": {}})())
        monkeypatch.setattr(httpx, "stream", lambda *a, **k: _Stream())

        bad_part, bad_final = tmp_path / "bad.gguf.part", tmp_path / "bad.gguf"
        with pytest.raises(RuntimeError, match="digest mismatch"):
            llm_local._stream_gguf(
                "https://example/m.gguf", bad_part, bad_final, expected_sha256="0" * 64
            )
        assert not bad_final.exists()
        assert not bad_part.exists()  # deleted so a retry re-downloads clean

        ok_part, ok_final = tmp_path / "ok.gguf.part", tmp_path / "ok.gguf"
        llm_local._stream_gguf("https://example/m.gguf", ok_part, ok_final, expected_sha256=good)
        assert ok_final.read_bytes() == full


def test_llm_ladder_n_ctx_large_enough():
    """Regression: every ladder rung must hold a full briefing prompt.

    A large briefing prompt plus the ~1,536 token reply budget needs roughly
    9,200 tokens of context. Every rung of the load ladder — including the
    lightest fallback — must clear that, or a fallback load would silently
    truncate prompts.
    """
    assert _LLM_LADDER, "the LLM config ladder is empty"
    for cfg in _LLM_LADDER:
        assert cfg["n_ctx"] >= 10240, (
            f"ladder rung n_ctx={cfg['n_ctx']} is too small for briefing prompts"
        )


def test_llm_ladder_steps_only_lighter():
    """Context never grows down the ladder — loading only ever steps down."""
    for prev, cur in zip(_LLM_LADDER, _LLM_LADDER[1:]):
        assert cur["n_ctx"] <= prev["n_ctx"]


# ── chat_completion / _infer_lock decode serialization ─────────────────────────


class _FitProbeMixin:
    """tokenize/n_ctx stubs for the context-fit guard (~4 bytes per token)."""

    def n_ctx(self):
        return 13312

    def tokenize(self, data: bytes, add_bos=False, special=False):
        return list(range(len(data) // 4))


class _RecordingLlama(_FitProbeMixin):
    """Fake llama.cpp model that flags re-entrancy on overlapping decodes.

    ``create_chat_completion`` runs in a worker thread (``asyncio.to_thread``),
    so two concurrent ``chat_completion`` calls would race here if the
    ``_infer_lock`` were removed. The brief sleep widens the window so an
    unserialized second entry is observed while the first is still inside.
    """

    def __init__(self):
        self._busy = False
        self.overlapped = False
        self.calls = 0

    def create_chat_completion(
        self, messages, max_tokens, temperature, response_format=None, grammar=None
    ):
        import time

        self.calls += 1
        if self._busy:
            self.overlapped = True
        self._busy = True
        try:
            time.sleep(0.05)
        finally:
            self._busy = False
        return {"choices": [{"message": {"content": "ok"}}]}


class TestChatCompletionLock:
    async def test_infer_lock_serializes_concurrent_decodes(self, monkeypatch):
        """Two concurrent chat_completion() calls must not overlap inside the
        shared model — the regression lock for the concurrent-decode crash."""
        import asyncio

        from memory_core import llm_local

        fake = _RecordingLlama()

        async def _fake_get_llm(tier=None):
            return fake

        monkeypatch.setattr(llm_local, "get_llm", _fake_get_llm)

        msgs = [{"role": "user", "content": "hi"}]
        results = await asyncio.gather(
            llm_local.chat_completion(msgs),
            llm_local.chat_completion(msgs),
        )

        assert results == ["ok", "ok"]
        assert fake.calls == 2
        assert not fake.overlapped, "decodes overlapped — _infer_lock did not serialize"

    async def test_returns_model_content(self, monkeypatch):
        from memory_core import llm_local

        fake = _RecordingLlama()

        async def _fake_get_llm(tier=None):
            return fake

        monkeypatch.setattr(llm_local, "get_llm", _fake_get_llm)

        out = await llm_local.chat_completion([{"role": "user", "content": "hi"}])
        assert out == "ok"


class _SlowLlama(_FitProbeMixin):
    """Fake llama.cpp model whose decode blocks far longer than the timeout."""

    def __init__(self):
        self.calls = 0

    def create_chat_completion(
        self, messages, max_tokens, temperature, response_format=None, grammar=None
    ):
        import time

        self.calls += 1
        # Block well past the tiny timeout used in the test so asyncio.wait_for
        # times out while this "decode" is still in flight on the worker thread.
        time.sleep(0.5)
        return {"choices": [{"message": {"content": "too late"}}]}


class TestFitToContext:
    """The context-fit guard: shrink the reply budget, then trim the prompt
    middle, instead of letting llama.cpp hard-error past n_ctx."""

    def _fake(self):
        return _RecordingLlama()

    def test_fitting_prompt_passes_through(self):
        from memory_core import llm_local

        msgs = [{"role": "user", "content": "x" * 400}]  # ~100 tokens
        out, budget = llm_local._fit_to_context(self._fake(), msgs, 2048)
        assert out == msgs
        assert budget == 2048

    def test_reply_budget_shrinks_before_trimming(self):
        from memory_core import llm_local

        # ~12000 tokens of prompt: 12000+2048+96 > 13312, but the prompt alone
        # fits — so only the reply budget gives way, and content is untouched.
        msgs = [{"role": "user", "content": "x" * 48_000}]
        out, budget = llm_local._fit_to_context(self._fake(), msgs, 2048)
        assert out[0]["content"] == msgs[0]["content"]
        assert llm_local._MIN_REPLY_TOKENS <= budget < 2048

    def test_oversized_prompt_trims_middle(self):
        from memory_core import llm_local

        head, tail = "HEAD-INSTRUCTIONS ", " TAIL-RULES"
        msgs = [{"role": "user", "content": head + "d" * 80_000 + tail}]  # ~20k tokens
        out, budget = llm_local._fit_to_context(self._fake(), msgs, 2048)
        content = out[0]["content"]
        assert budget == llm_local._MIN_REPLY_TOKENS
        assert llm_local._TRIM_MARKER in content
        assert content.startswith(head)
        assert content.endswith(tail)
        # The trimmed prompt actually fits the window.
        assert len(content) // 4 <= 13312 - llm_local._MIN_REPLY_TOKENS

    def test_original_messages_not_mutated(self):
        from memory_core import llm_local

        original = "d" * 80_000
        msgs = [{"role": "user", "content": original}]
        llm_local._fit_to_context(self._fake(), msgs, 2048)
        assert msgs[0]["content"] == original


class TestChatCompletionTimeoutUnloads:
    """Bug U2: on an ``asyncio.wait_for`` timeout the underlying llama.cpp decode
    keeps running in its worker thread (it can't be cancelled). If
    ``chat_completion`` re-raised while leaving ``_llm`` loaded, the next caller
    would acquire the freed ``_infer_lock`` and start a SECOND decode on the SAME
    single-context model — the KV-cache-corruption / SIGSEGV crash ``_infer_lock``
    exists to prevent. The fix drops the module's model reference on timeout."""

    async def test_timeout_unloads_model(self, monkeypatch):
        import asyncio

        from memory_core import llm_local

        fake = _SlowLlama()

        async def _fake_get_llm(tier=None):
            return fake

        monkeypatch.setattr(llm_local, "get_llm", _fake_get_llm)
        # Simulate a loaded model so we can observe it being dropped on timeout.
        monkeypatch.setattr(llm_local, "_llm", fake)

        with pytest.raises(asyncio.TimeoutError):
            await llm_local.chat_completion(
                [{"role": "user", "content": "hi"}],
                timeout=0.05,
            )

        # The orphaned decode is still running in its worker thread; the model
        # reference must have been dropped so the NEXT caller loads fresh rather
        # than reusing the in-flight single-context model.
        assert llm_local._llm is None
        assert await llm_local.is_loaded() is False

    async def test_queued_caller_after_timeout_uses_fresh_model(self, monkeypatch):
        """A caller queued behind a timing-out decode must resolve its model
        reference INSIDE the lock, so it picks up a fresh model rather than the
        orphaned single-context model the dead decode is still mutating.

        Regression for the race where ``get_llm`` was awaited BEFORE acquiring
        ``_infer_lock``: the second caller captured the same model reference as
        the first and raced its un-cancellable in-flight decode."""
        import asyncio

        from memory_core import llm_local

        orphan = _SlowLlama()  # first caller's model — its decode outlives the timeout
        fresh = _RecordingLlama()  # what the second caller must get instead
        models = [orphan, fresh]

        async def _fake_get_llm(tier=None):
            # Mimics the real get_llm: hands out the live model, and only loads
            # a new one once the previous reference was dropped (set to None).
            if llm_local._llm is None:
                llm_local._llm = models.pop(0)
            return llm_local._llm

        monkeypatch.setattr(llm_local, "get_llm", _fake_get_llm)
        monkeypatch.setattr(llm_local, "_llm", None)

        msgs = [{"role": "user", "content": "hi"}]

        async def _first():
            with pytest.raises(asyncio.TimeoutError):
                await llm_local.chat_completion(msgs, timeout=0.05)

        async def _second():
            # Start slightly after the first so it queues on _infer_lock while
            # the first is mid-decode; it must run only after the first releases.
            await asyncio.sleep(0.01)
            return await llm_local.chat_completion(msgs)

        _, out = await asyncio.gather(_first(), _second())

        # The second caller used the FRESH model, never the orphaned one.
        assert out == "ok"
        assert fresh.calls == 1
        # The orphan decoded exactly once (the first caller) and was never
        # reused by the queued caller.
        assert orphan.calls == 1


class TestTierSwap:
    """get_llm(tier=...) swaps the resident model for per-stage routing."""

    async def test_get_llm_swaps_on_tier_change(self, monkeypatch):
        from memory_core import llm_local

        loads: list[tuple[str, str]] = []

        def fake_load(llama_cls, model_path, tier):
            loads.append((tier, model_path))
            return object()

        monkeypatch.setattr(llm_local, "_load_with_fallback", fake_load)
        monkeypatch.setitem(
            __import__("sys").modules, "llama_cpp", type("M", (), {"Llama": object})
        )
        monkeypatch.setattr(llm_local, "_llm", None)
        monkeypatch.setattr(llm_local, "_loaded_tier", None)

        first = await llm_local.get_llm("ministral3-14b")
        again = await llm_local.get_llm("ministral3-14b")
        assert first is again  # same tier → resident model reused
        swapped = await llm_local.get_llm("gemma4-12b")
        assert swapped is not first
        assert [t for t, _ in loads] == ["ministral3-14b", "gemma4-12b"]
        assert "gemma-4-12b" in loads[1][1]

        await llm_local.unload()

    async def test_unknown_tier_resolves_to_selected(self, monkeypatch):
        from memory_core import llm_local

        loads: list[str] = []

        def fake_load(llama_cls, model_path, tier):
            loads.append(tier)
            return object()

        async def fake_selected():
            return "ministral3-14b"

        monkeypatch.setattr(llm_local, "_load_with_fallback", fake_load)
        monkeypatch.setattr(llm_local, "_selected_tier", fake_selected)
        monkeypatch.setitem(
            __import__("sys").modules, "llama_cpp", type("M", (), {"Llama": object})
        )
        monkeypatch.setattr(llm_local, "_llm", None)
        monkeypatch.setattr(llm_local, "_loaded_tier", None)

        await llm_local.get_llm("claude-haiku-4-5-20251001")  # a cloud id passed through
        assert loads == ["ministral3-14b"]

        await llm_local.unload()

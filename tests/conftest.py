"""Shared pytest fixtures for the Estormi test suite."""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Fail fast on the wrong interpreter. The codebase targets Python 3.12+
# (pyproject ``requires-python = ">=3.12"``) and uses 3.10+ syntax (``X | None``)
# plus 3.11 stdlib (``tomllib``) throughout, so an older interpreter blows up at
# import time with cryptic SyntaxError/ModuleNotFoundError across ~40% of the
# suite. This guard turns that into one clear message — run via the bundled
# ``.venv`` (see CONTRIBUTING.md) if your system ``python3`` is older.
if sys.version_info < (3, 12):
    raise RuntimeError(
        f"Estormi tests require Python 3.12+ (pyproject requires-python = '>=3.12'); "
        f"found {sys.version.split()[0]}. Use the project venv, e.g. `.venv/bin/python -m pytest`."
    )

from tests.helpers.database import apply_runtime_schema

# ---------------------------------------------------------------------------
# Path setup — make the server, ingestion, and the local packages importable.
# The six first-party Python packages live under ``packages/`` (beside the JS
# workspaces), so ``packages/`` on sys.path makes ``import estormi_server`` /
# ``import estormi_ingestion`` / ``import memory_core`` / ``import connectors``
# resolve without an editable install. REPO_ROOT itself stays on the path so
# ``import tests.*`` (helpers, fixtures) keeps resolving.
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "packages"))
sys.path.insert(0, str(REPO_ROOT))

# The sys.path inserts above only affect THIS interpreter. Tests that spawn a
# child process directly — e.g. ``python -m memory_core.dag_state`` in
# tests/memory_core/test_dag_state.py — bypass the production launcher
# (server.jobs sets PYTHONPATH itself, see test_engine_subprocess_env.py), so
# without PYTHONPATH the child cannot import the first-party packages and fails
# with ModuleNotFoundError under CI (which has no editable install). Mirror the
# sys.path roots onto PYTHONPATH so child interpreters resolve them too,
# preserving any caller-provided value.
_import_roots = os.pathsep.join([str(REPO_ROOT / "packages"), str(REPO_ROOT)])
os.environ["PYTHONPATH"] = (
    f"{_import_roots}{os.pathsep}{os.environ['PYTHONPATH']}"
    if os.environ.get("PYTHONPATH")
    else _import_roots
)

# ---------------------------------------------------------------------------
# Override ESTORMI_DATA_DIR to a temp directory so tests never touch real data
# ---------------------------------------------------------------------------
_tmpdir = tempfile.mkdtemp(prefix="estormi-test-")
os.environ["ESTORMI_DATA_DIR"] = _tmpdir
# Sandbox the relocation pointer/marker (memory_core.datadir.config_home) too,
# so a storage-relocate test never writes into the real Application Support dir.
os.environ["ESTORMI_CONFIG_HOME"] = os.path.join(_tmpdir, "config-home")
os.environ["AUDIT_LOG_PATH"] = os.path.join(_tmpdir, "audit-test.log")
# Point the iCloud Drive vault at the temp dir too, so a stray vault write
# from an engine snapshot never touches the user's real iCloud Drive folder.
os.environ["ESTORMI_VAULT_DIR"] = os.path.join(_tmpdir, "vault")
atexit.register(shutil.rmtree, _tmpdir, ignore_errors=True)

# Pin the briefing timezone so day-boundary logic and %H:%M agenda rendering are
# deterministic regardless of the host timezone. CI runners are UTC and a dev may
# be anywhere; without this, timezone-sensitive assertions (e.g. the distill
# dataset's "09:45–10:00 Daily", which is an event's Europe/Paris local time)
# fail off Europe/Paris. setdefault so an explicit override still wins.
os.environ.setdefault("ESTORMI_LOCAL_TZ", "Europe/Paris")

# ---------------------------------------------------------------------------
# Pin the vendored-font static mount to this checkout's assets/fonts directory
# so the ``/fonts`` mount stays alive regardless of how ``server.jobs`` resolves
# its repo root (it may differ when the suite runs from inside a worktree).
# ---------------------------------------------------------------------------
_fonts_dir = REPO_ROOT / "assets" / "fonts"
if _fonts_dir.is_dir() and "FONTS_DIR" not in os.environ:
    os.environ["FONTS_DIR"] = str(_fonts_dir)


# Suite-wide guard: never let a test reach the real macOS login Keychain.
#
# Several modules persist OAuth tokens / the MCP bearer token through
# ``keyring`` (``estormi_ingestion.shared.token_store``,
# ``estormi_server.server.security``). On a developer Mac an un-stubbed
# ``keyring.get_password`` pops the system "allow access to your keychain"
# dialog and can stall the run; in CI it errors out. This autouse fixture
# replaces the three keyring entrypoints with an in-memory dict so no test
# can ever touch the real Keychain, whether it asked for a keyring mock or
# not.
#
# It coexists with both opt-in patterns already in the suite:
#   * tests that swap the whole module via
#     ``monkeypatch.setitem(sys.modules, "keyring", <stub>)`` — the lazy
#     ``import keyring`` inside the code under test then resolves *their*
#     stub, bypassing (and unaffected by) the attribute patches here;
#   * tests that do ``patch("keyring.get_password", ...)`` — that nested
#     patch is entered after this fixture, so it wins for its duration and
#     restores back to this in-memory stub on exit.
# Robust by design: a no-op if ``keyring`` cannot be imported at all.
@pytest.fixture(autouse=True)
def _stub_keyring():
    try:
        # Imported only to confirm the module resolves before we patch its
        # functions by string target below; the names patched are attributes
        # of this same module object.
        import keyring  # noqa: F401, PLC0415
    except Exception:
        # No keyring backend at all (e.g. a minimal CI image) — nothing real
        # to hit, so there is nothing to guard against.
        yield
        return

    store: dict[tuple[str, str], str] = {}

    def _get(service: str, key: str):
        return store.get((service, key))

    def _set(service: str, key: str, value: str):
        store[(service, key)] = value

    def _delete(service: str, key: str):
        # Real ``keyring.delete_password`` raises ``PasswordDeleteError`` when
        # the entry is absent; the production callers all wrap delete in a
        # try/except, so a quiet no-op here matches what they tolerate.
        store.pop((service, key), None)

    with (
        patch("keyring.get_password", side_effect=_get),
        patch("keyring.set_password", side_effect=_set),
        patch("keyring.delete_password", side_effect=_delete),
    ):
        yield store


# Suite-wide guard: never construct the real llama.cpp model in a test.
#
# ``memory_core.llm_local.get_llm`` lazily does ``from llama_cpp import Llama``
# and builds it via ``_load_with_fallback``. The prebuilt ``llama-cpp-python``
# wheel emits CPU instructions (AVX/AVX512/FMA) that some CI runners lack, so a
# real construction aborts the whole process with ``Fatal Python error: Illegal
# instruction`` — taking the entire ``-m integration`` run down with it. No test
# needs a real model: the LLM-aware paths either mock the call (briefing
# ``llm.runtime._llm_call``) or, for the one integration test that runs the real
# briefing pipeline, rely on its best-effort degrade-when-the-load-fails branch.
#
# We patch the ``llama_cpp.Llama`` *class* (not ``get_llm`` / ``_load_with_fallback``,
# which have dedicated unit tests that pass their own fake ``llama_cls``). The
# replacement raises on construction, so the load ladder exhausts and the caller
# degrades exactly as it would on a machine without a model. A no-op when
# ``llama_cpp`` isn't installed (e.g. the typecheck env). Mirrors ``_stub_keyring``.
@pytest.fixture(autouse=True)
def _block_real_local_llm(monkeypatch):
    try:
        import llama_cpp  # noqa: PLC0415
    except Exception:
        yield
        return

    class _BlockedLlama:
        def __init__(self, *_a, **_k):
            raise RuntimeError(
                "real local LLM blocked in tests — mock the LLM call "
                "(see _block_real_local_llm in tests/conftest.py)"
            )

    monkeypatch.setattr(llama_cpp, "Llama", _BlockedLlama)
    yield


# Reset the security boundary's bearer-token cache between tests. In
# production the cache is primed once at lifespan startup so the keychain
# round-trip doesn't happen on every request; in the test suite each test
# may re-patch ``keyring.get_password`` with a different value and would
# otherwise see a stale cached token from a prior test in the same session.
@pytest.fixture(autouse=True)
def _reset_security_token_cache():
    try:
        from estormi_server.server import security  # noqa: PLC0415

        security._cached_token = None
        security._token_resolved = False
        security._mcp_auto_token = None
    except Exception:
        pass
    yield
    try:
        from estormi_server.server import security  # noqa: PLC0415

        security._cached_token = None
        security._token_resolved = False
        security._mcp_auto_token = None
    except Exception:
        pass


# Disable SlowAPI rate limiting for the whole test run. Tests exercise route
# handlers both directly (passing a mock request) and over ASGI; the
# ``@limiter.limit`` decorator rejects a non-``Request`` object, and per-test
# call counts would otherwise risk flaking limit-bounded tests. The limiter's
# rejection behaviour is covered explicitly in
# ``tests/estormi_server/test_rate_limiting.py``, which re-enables the shared
# limiter for one test and restores it on teardown, so disabling it here loses
# no coverage.
from estormi_server.server.limiter import limiter as _limiter  # noqa: E402

_limiter.enabled = False


@pytest.fixture
async def db():
    """Yield an in-memory aiosqlite connection with the full Estormi schema."""
    import aiosqlite

    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await apply_runtime_schema(conn)
    yield conn
    await conn.close()


@pytest.fixture
async def db_on_disk(tmp_path):
    """Yield a file-backed aiosqlite DB with full schema (for watermark tests etc.)."""
    import aiosqlite

    db_path = str(tmp_path / "estormi.db")
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    await apply_runtime_schema(conn)
    yield conn, db_path
    await conn.close()


@pytest.fixture
def mock_embedder():
    """Patch embed_one and sparse_embed_one to return deterministic fake vectors."""
    fake_dense = [0.1] * 768

    fake_sparse = {"indices": [0, 1, 2], "values": [0.5, 0.3, 0.2]}

    with (
        patch(
            "estormi_server.storage.tools.embed_one",
            new_callable=AsyncMock,
            return_value=fake_dense,
        ),
        patch(
            "estormi_server.storage.tools.sparse_embed_one",
            new_callable=AsyncMock,
            return_value=fake_sparse,
        ),
    ):
        yield fake_dense, fake_sparse


@pytest.fixture
def mock_qdrant():
    """Patch Qdrant client with a mock that tracks calls."""
    mock_client = AsyncMock()
    mock_client.upsert = AsyncMock()
    mock_client.delete = AsyncMock()
    mock_client.retrieve = AsyncMock(return_value=[])

    # query_points returns empty by default
    query_result = MagicMock()
    query_result.points = []
    mock_client.query_points = AsyncMock(return_value=query_result)

    with patch("estormi_server.storage.tools._client", return_value=mock_client):
        yield mock_client


@pytest.fixture
async def wired_tools_db(db, mock_embedder, mock_qdrant):
    """Wire the shared in-memory DB into ``tools._db`` for the duration of a test.

    Eight-plus call sites used to duplicate the ``tools._db = db`` /
    ``tools._db = None`` setup by hand; this fixture is the single source.

    Always include ``mock_embedder`` and ``mock_qdrant`` in the closure so
    callers that just request ``wired_tools_db`` still get the storage
    backends stubbed out (production code reaches for both as soon as it
    touches ``writers.ingest_chunk`` or ``search_api.search_memory``).
    """
    from estormi_server.storage import tools

    tools._db = db
    try:
        yield db
    finally:
        tools._db = None


@pytest.fixture
async def client(db, mock_embedder, mock_qdrant, tmp_path):
    """Create an httpx AsyncClient backed by the FastAPI app with mocked deps."""
    from httpx import ASGITransport
    from httpx import AsyncClient as _AsyncClient

    from estormi_server.storage import tools

    tools._db = db

    # Mark setup as completed so the redirect middleware doesn't kick in
    await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('setup_completed', '1')")
    await db.commit()

    # Patch ensure_collection to skip actual Qdrant setup.
    # Also mock keyring — no system keyring is available in CI.
    # Set a known MCP token so the auto-token generator doesn't kick in;
    # tests that exercise /mcp must send "Authorization: Bearer test-bearer-token".
    _test_token = "test-bearer-token"
    with (
        patch("estormi_server.server.lifespan.ensure_collection", new_callable=AsyncMock),
        patch("keyring.get_password", return_value=_test_token),
        patch("keyring.set_password"),
    ):
        from estormi_server.main import app

        transport = ASGITransport(app=app)
        # Default both the CSRF origin header and the bearer token on every
        # request.  Production Tauri webview requests carry the CSRF header;
        # MCP clients carry the bearer.  Tests that want to exercise the
        # "missing header" branch can override by passing the header as ""
        # explicitly.
        async with _AsyncClient(
            transport=transport,
            base_url="http://test",
            follow_redirects=False,
            headers={
                "X-Estormi-Origin": "tauri",
                "Authorization": f"Bearer {_test_token}",
            },
        ) as ac:
            yield ac

    # An endpoint may have swapped a fresh connection into ``tools._db``
    # (e.g. ``reset_db()`` reopens a file-backed DB). Close whatever is there
    # so its aiosqlite background thread does not outlive this event loop —
    # an orphaned thread later raises "Event loop is closed". The fixture's
    # own ``db`` is closed by its own fixture, so guard against double-close.
    if tools._db is not None and tools._db is not db:
        await tools._db.close()
    tools._db = None

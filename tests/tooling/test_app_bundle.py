"""Regression tests for the macOS app bundle layout.

These guard the source `Info.plist` (the manifest baked into the .app at
bundle time) and the Tauri configuration. The bundled `Info.plist` inside
`/Applications/Estormi.app/Contents/` is not committed — we test the inputs
that produce it.
"""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
INFO_PLIST = REPO_ROOT / "apps" / "estormi-macos" / "Info.plist"
TAURI_CONF = REPO_ROOT / "apps" / "estormi-macos" / "tauri.conf.json"


@pytest.mark.unit
def test_info_plist_exists():
    assert INFO_PLIST.is_file(), f"missing {INFO_PLIST}"


@pytest.mark.unit
def test_info_plist_is_well_formed():
    with INFO_PLIST.open("rb") as f:
        data = plistlib.load(f)
    assert isinstance(data, dict)


@pytest.mark.unit
def test_info_plist_does_not_hide_app_from_launchpad():
    """LSUIElement=true would hide Estormi from Dock, Launchpad, and Cmd-Tab.

    Estormi is a regular Mac app (with a tray icon on top). This regression
    test catches accidental re-introduction of the agent-style flag.
    """
    with INFO_PLIST.open("rb") as f:
        data = plistlib.load(f)
    # Either absent (preferred — macOS default is "visible") or explicitly False.
    assert data.get("LSUIElement", False) is False, (
        "LSUIElement is truthy — Estormi would be hidden from Launchpad/Dock/Cmd-Tab. "
        "Remove the key from apps/estormi-macos/Info.plist."
    )


@pytest.mark.unit
def test_info_plist_declares_required_permission_strings():
    with INFO_PLIST.open("rb") as f:
        data = plistlib.load(f)
    required = {
        "NSRemindersUsageDescription",
        "NSContactsUsageDescription",
        "NSAppleEventsUsageDescription",
        "NSCalendarsUsageDescription",
        "NSHumanReadableCopyright",
    }
    missing = required - data.keys()
    assert not missing, f"Info.plist missing required keys: {sorted(missing)}"


@pytest.mark.unit
def test_tauri_conf_identifier_and_min_macos():
    import json

    conf = json.loads(TAURI_CONF.read_text())
    assert conf["identifier"] == "app.estormi.local"
    assert conf["bundle"]["macOS"]["minimumSystemVersion"] == "13.0"
    assert conf["bundle"]["macOS"]["infoPlist"] == "Info.plist"


# The bundle's resource paths are repo-root-relative (``../../x`` from
# apps/estormi-macos/) and resolve at *bundle time*, never at app launch — so a
# refactor that moves a bundled directory (e.g. relocating connectors to the repo
# root, or moving the Tauri shell again) silently ships a broken .app, and with
# CI offline the only backstop is this local-gate guard. ``python/`` and
# ``web-ui/dist`` are build products (bundled CPython, compiled SPA) that may not
# exist on a fresh checkout, so we assert their parent resolves, not the leaf.
_BUNDLE_BUILD_PRODUCTS = {"../../python", "../../packages/web-ui/dist"}


@pytest.mark.unit
def test_bundle_resource_paths_resolve():
    import json

    conf = json.loads(TAURI_CONF.read_text())
    entries = list(conf["bundle"]["resources"]) + [conf["build"]["frontendDist"]]
    src_tauri = TAURI_CONF.parent
    missing: list[str] = []
    for entry in entries:
        target = (src_tauri / entry).resolve()
        ok = target.parent.is_dir() if entry in _BUNDLE_BUILD_PRODUCTS else target.exists()
        if not ok:
            missing.append(f"{entry} -> {target}")
    assert not missing, "tauri.conf.json bundle paths that do not resolve:\n" + "\n".join(missing)


@pytest.mark.unit
def test_all_engine_packages_bundled():
    """All four engine packages ship as bundle resources.

    ``estormi_distill`` is a 124K pure-Python package — it is NOT the GPU
    training surface. The heavy MLX toolkit (~1 GB of wheels) is installed into
    the data dir on first use by ``estormi_distill.trainer.bootstrap_tooling``
    (engine phase ⓪; ``scripts/setup_distill.sh`` does the same by hand) and
    probed at runtime by ``estormi_distill.trainer.tooling()``; it is
    deliberately never a bundle resource. But the package itself must ship:
    without it the packaged app's ``/api/distill/status`` route raises
    ``ModuleNotFoundError`` (HTTP 500), and the web-ui Distillation card degrades
    to "unavailable (update the app)" instead of its intended "Set up & distill"
    state. The scheduled retrain (``-m estormi_distill.run_distill``) likewise
    cannot launch when the package is absent.
    """
    import json

    conf = json.loads(TAURI_CONF.read_text())
    resources = conf["bundle"]["resources"]
    for engine in (
        "../../packages/estormi_server",
        "../../packages/estormi_ingestion",
        "../../packages/estormi_briefing",
        "../../packages/estormi_distill",
    ):
        assert engine in resources, f"{engine} must be a bundled resource"

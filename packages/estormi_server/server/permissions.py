"""Per-source macOS permission orchestration.

A connector's ``ConnectorSpec.macos_permissions`` declares which macOS
TCC permissions the source needs to ingest. ``ensure_source_permission``
is called the moment a user activates a source so the macOS prompt fires
*then* — attributed to Estormi — instead of surfacing mid-pipeline-run,
which is confusing and easy to miss.

Each permission is also *verified*: after the request the real TCC
authorization status is read back, so activation reports ground truth
rather than assuming an enabled toggle implies access.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from pathlib import Path

import structlog

log = structlog.get_logger()

# Honour ESTORMI_REPO_ROOT (set by the Rust sidecar to the bundle resource
# root) so the relocatable .app resolves correctly; fall back to the
# file-relative derivation in a dev checkout. Matches ``server.jobs.ROOT``.
_env_repo_root = os.getenv("ESTORMI_REPO_ROOT", "").strip()
ROOT = (
    Path(_env_repo_root) if _env_repo_root else Path(__file__).resolve().parent.parent.parent.parent
)
for _p in (str(ROOT),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from connectors import registry  # noqa: E402

# System Settings deep links — used to send the user to the right pane
# when a permission is denied, or (for Full Disk Access) cannot be
# prompted for at all. Every URL here is also in the open-url allow-list
# in estormi_server/api/system.py.
_PANE_REMINDERS = "x-apple.systempreferences:com.apple.preference.security?Privacy_Reminders"
_PANE_CONTACTS = "x-apple.systempreferences:com.apple.preference.security?Privacy_Contacts"
_PANE_ALL_FILES = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
_PANE_FILES = "x-apple.systempreferences:com.apple.preference.security?Privacy_FilesAndFolders"
_PANE_REMOVABLE = "x-apple.systempreferences:com.apple.preference.security?Privacy_RemovableVolumes"
_PANE_AUTOMATION = "x-apple.systempreferences:com.apple.preference.security?Privacy_AppleEvents"

# AppleEvents:<App> permission key → the app a benign probe event targets.
# A closed map so nothing user-controlled is ever interpolated into the
# osascript command.
_AUTOMATION_TARGETS = {"AppleEvents:Notes": "Notes", "AppleEvents:Mail": "Mail"}

# One-line explanation per status, formatted with the permission label.
_DETAIL = {
    "authorized": "{label} is granted.",
    "denied": ("{label} was denied. Grant it in System Settings, then re-run the source."),
    "undetermined": (
        "{label} could not be confirmed. Approve the macOS prompt, or grant it in System Settings."
    ),
    "manual": "{label} must be granted manually in System Settings.",
    "unavailable": "{label} could not be checked on this system.",
}

# Statuses where the user has something to fix → surface the Settings link.
_ACTIONABLE = {"denied", "undetermined", "manual"}


def source_permission_key(name: str) -> str | None:
    """Return the macOS permission key a source needs, or None.

    Every connector declares at most one macOS permission today, so the
    first entry of ``macos_permissions`` is the whole story.
    """
    cls = registry.get(name)
    if cls is None:
        return None
    perms = cls.spec.macos_permissions
    return perms[0] if perms else None


def ensure_source_permission(name: str, root: str | None = None) -> dict | None:
    """Trigger + verify the macOS permission a source needs on activation.

    ``root`` is the configured filesystem root for folder-rooted sources
    (Documents, Code); it is probed to trigger the macOS Files-and-Folders
    prompt at activation. Ignored by sources whose permission isn't
    filesystem-based.

    Returns None when the source needs no macOS permission. Otherwise a
    dict the Settings UI renders::

        {
          "key":   <spec permission key>,
          "label": <human label>,
          "status": authorized | denied | undetermined | manual | unavailable,
          "detail": <one-line human explanation>,
          "settings_pane": <System Settings URL> | None,
        }

    ``status`` semantics:
      - authorized   — verified granted; the source can ingest.
      - denied       — the user (previously) refused; fix it in Settings.
      - manual       — macOS exposes no prompt (Full Disk Access); the
                       user must grant it by hand.
      - undetermined — the prompt was shown but no answer was captured
                       (timeout); the source may still work next run.
      - unavailable  — not macOS, or the permission frameworks are absent
                       (running from source without PyObjC). Not an error.
    """
    key = source_permission_key(name)
    if key is None:
        return None

    if key == "Reminders":
        return _eventkit_result(key, "Reminders access", _PANE_REMINDERS)
    if key == "Contacts":
        return _contacts_result(key, "Contacts access", _PANE_CONTACTS)
    if key == "FullDiskAccess":
        return _result(
            key,
            "Full Disk Access",
            _full_disk_access_status(),
            settings_pane=_PANE_ALL_FILES,
            detail_overrides={
                "manual": (
                    "iMessage needs Full Disk Access. macOS has no prompt "
                    "for this — grant it to Estormi in System Settings, then "
                    "restart Estormi."
                )
            },
        )
    if key in _AUTOMATION_TARGETS:
        app = _AUTOMATION_TARGETS[key]
        return _result(
            key,
            f"{app} automation access",
            _probe_automation(app),
            settings_pane=_PANE_AUTOMATION,
        )
    if key == "FilesAndFolders":
        return _files_result(key, "Files & Folders access", root)

    log.warning("permissions.unknown_key", key=key, source=name)
    return None


def _eventkit_result(key: str, label: str, pane: str) -> dict:
    """Request + read back the EventKit Reminders permission."""
    try:
        from estormi_ingestion.shared.host import macos_permissions as mp  # noqa: PLC0415
    except ImportError:
        return _result(key, label, "unavailable", settings_pane=pane)

    mp.request_reminders_access()
    raw = mp.get_reminders_status()
    return _result(key, label, _normalize_tcc(raw), settings_pane=pane)


def _contacts_result(key: str, label: str, pane: str) -> dict:
    """Request + read back the Contacts permission via the native framework.

    Mirrors ``_eventkit_result``: ``request_contacts_access`` fires the real
    ``NSContactsUsageDescription`` prompt (no-op once already decided), then
    the status is read back so activation reports ground truth. Runs in the
    sidecar at preflight (server startup, see ``server/lifespan.py``), so the
    prompt appears at launch rather than mid-sync.
    """
    try:
        from estormi_ingestion.shared.host import macos_permissions as mp  # noqa: PLC0415
    except ImportError:
        return _result(key, label, "unavailable", settings_pane=pane)

    mp.request_contacts_access()
    return _result(key, label, _normalize_tcc(mp.get_contacts_status()), settings_pane=pane)


def _normalize_tcc(raw: str) -> str:
    """Map an EventKit / Contacts authorization string to a permission status."""
    if raw == "authorized":
        return "authorized"
    if raw in ("denied", "restricted"):
        return "denied"
    if raw == "not_determined":
        return "undetermined"
    return "unavailable"


def _probe_automation(app: str) -> str:
    """Send a benign Apple Event to ``app``, triggering the Automation prompt.

    The first call shows the macOS "Estormi wants to control <app>"
    dialog and ``osascript`` blocks until the user answers. ``get
    version`` is the lightest event that *must* round-trip to the target
    process — it reads no user data, but unlike ``name`` AppleScript
    cannot answer it locally, so the Apple Events TCC check the
    AppleScript ingestion relies on is always exercised. A grant here
    therefore carries over to the pipeline run.
    """
    if platform.system() != "Darwin":
        return "unavailable"
    try:
        proc = subprocess.run(
            ["osascript", "-e", f'tell application "{app}" to get version'],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "undetermined"
    except OSError:
        log.exception("automation probe could not launch osascript")
        return "unavailable"
    if proc.returncode == 0:
        return "authorized"
    err = (proc.stderr or "").lower()
    if "-1743" in err or "not authori" in err or "not allowed" in err:
        return "denied"
    log.warning(
        "permissions.automation_probe_exited", app=app, returncode=proc.returncode, stderr=err[:200]
    )
    return "undetermined"


def _files_result(key: str, label: str, root: str | None) -> dict:
    """Probe the configured root to trigger the Files-and-Folders prompt.

    macOS exposes no API to *request* Files-and-Folders / Removable-Volume
    access the way EventKit does for Calendars. The only way to surface the
    prompt attributed to Estormi (rather than mid-pipeline-run) is to touch
    the folder from this process — a descendant of the app — while the user
    is in the app. ``scandir`` blocks on the TCC prompt and then reports
    ground truth: it succeeds once granted, raises ``PermissionError`` when
    denied. Until the user picks a folder there is nothing to probe.

    A root on a removable / external volume is a *different* TCC bucket
    (Removable Volumes, not Files & Folders), so the label and Settings pane
    are switched to match — pointing the user at the right pane when denied.
    """
    if platform.system() != "Darwin":
        return _result(key, label, "unavailable", settings_pane=_PANE_FILES)
    if not (root or "").strip():
        return _result(
            key,
            label,
            "undetermined",
            settings_pane=_PANE_FILES,
            detail_overrides={
                "undetermined": (
                    "Choose the folder to index first; access is requested when you do."
                )
            },
        )
    removable = _is_removable(_volume_of(root))  # type: ignore[arg-type]
    pane = _PANE_REMOVABLE if removable else _PANE_FILES
    if removable:
        label = "Removable-volume access"
    return _result(
        key,
        label,
        _probe_filesystem(root),  # type: ignore[arg-type]
        settings_pane=pane,
        detail_overrides={
            "denied": (
                "Folder access was denied. Grant Estormi access to the folder "
                "(and the removable volume it lives on) in System Settings, then "
                "re-run the source."
            )
        },
    )


def _volume_of(path: str) -> Path:
    """Return the mount point (volume root) containing ``path``.

    Walks up from ``path`` to the first directory that is a filesystem mount
    point. Falls back to ``/`` for a path that resolves nowhere mountable.
    """
    p = Path(path).expanduser()
    try:
        p = p.resolve()
    except OSError:
        pass
    for candidate in (p, *p.parents):
        try:
            if os.path.ismount(candidate):
                return candidate
        except OSError:
            continue
    return Path("/")


def _is_removable(volume: Path) -> bool:
    """Whether ``volume`` is an external / removable mount.

    macOS mounts the system + data volumes at ``/`` and external drives under
    ``/Volumes/<name>``. Any mount point under ``/Volumes`` that isn't the
    boot volume is treated as removable — accessing it triggers the dedicated
    Removable-Volumes TCC prompt rather than the Files-and-Folders one.
    """
    return volume != Path("/") and volume.parent == Path("/Volumes")


def _probe_filesystem(root: str) -> str:
    """List ``root`` to exercise (and surface) the macOS Files-and-Folders check."""
    path = Path(root).expanduser()
    try:
        with os.scandir(path) as it:
            for _ in it:
                break
        return "authorized"
    except PermissionError:
        return "denied"
    except FileNotFoundError:
        return "undetermined"
    except OSError:
        log.exception("permissions.fda_probe_failed", root=str(root))
        return "undetermined"


def _full_disk_access_status() -> str:
    """Read the iMessage Full Disk Access flag the Tauri shell writes.

    macOS exposes no API to *request* Full Disk Access, so this only
    reads reality: the native shell probes ``chat.db`` at launch (a real
    open + read, not a stat) and writes ``imessage-fda.flag`` — the
    sandboxed Python sidecar cannot read the FDA-protected path itself.
    ``"1"`` → granted; ``"absent"`` → no chat.db at all, so FDA is moot
    and nothing is blocked; anything else, or a missing flag, means the
    user must grant it manually.
    """
    try:
        from estormi_server.storage.tools import DATA_DIR  # noqa: PLC0415
    except Exception:  # noqa: BLE001 — best-effort: treat as not-yet-granted
        return "manual"
    try:
        flag = (Path(DATA_DIR) / "imessage-fda.flag").read_text(encoding="utf-8").strip()
    except OSError:
        return "manual"
    return "authorized" if flag in ("1", "absent") else "manual"


def recheck_full_disk_access() -> str:
    """Re-probe iMessage Full Disk Access by asking the Tauri host to refresh
    the chat.db snapshot.

    The bundled Python sidecar never inherits the app's Full Disk Access — macOS
    treats the re-signed interpreter as its own TCC responsible process, denied
    even after the user grants the app and relaunches. So a Python-side read can
    never confirm a grant; only the main app binary can. This calls the loopback
    snapshot endpoint (the exact path ingestion uses), so the verdict reflects
    whether the next run will actually be able to read. The Rust side rewrites
    ``imessage-fda.flag`` as a side effect, keeping the overview in sync.

    Returns the status: ``authorized`` | ``manual`` | ``unavailable``.
    """
    if platform.system() != "Darwin":
        return "unavailable"
    token = os.environ.get("ESTORMI_WA_TOKEN", "")
    if not token:
        return "unavailable"
    import urllib.request  # noqa: PLC0415 — stdlib, only this loopback call needs it

    req = urllib.request.Request(
        "http://127.0.0.1:9877/api/imessage/snapshot",
        method="POST",
        headers={"x-estormi-wa-token": token},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            status = json.loads(resp.read().decode()).get("status", "")
    except Exception:  # noqa: BLE001 — host unreachable / malformed: report unavailable
        log.exception("fda snapshot recheck failed")
        return "unavailable"
    return status if status in ("authorized", "manual") else "unavailable"


def _result(
    key: str,
    label: str,
    status: str,
    *,
    settings_pane: str,
    detail_overrides: dict[str, str] | None = None,
) -> dict:
    """Assemble the permission dict, picking a human detail for ``status``."""
    overrides = detail_overrides or {}
    detail = overrides.get(status) or _DETAIL.get(status, "{label}: {status}").format(
        label=label, status=status
    )
    return {
        "key": key,
        "label": label,
        "status": status,
        "detail": detail,
        "settings_pane": settings_pane if status in _ACTIONABLE else None,
    }


def probe_working_set_volumes(extra_roots: tuple[str, ...] = ()) -> list[dict]:
    """Surface (and grant) removable-volume access for the app's working set.

    Beyond per-source roots, the app itself reads its repo and data directory.
    When either lives on a removable / external volume — the common case when
    running Estormi from a repo on an external SSD — macOS fires the
    Removable-Volumes prompt the first time the volume is touched. Probing the
    distinct volumes here, at preflight, surfaces that prompt once (attributed
    to Estormi) instead of mid-pipeline-run.

    Returns one result dict per *distinct* removable volume, keyed
    ``RemovableVolume:<mountpoint>``. Empty on non-macOS, or when nothing in
    the working set lives on a removable volume (the packaged-install case:
    app in /Applications, data in ~/Library).
    """
    if platform.system() != "Darwin":
        return []

    candidates = [str(ROOT)]
    try:
        from estormi_server.storage.tools import DATA_DIR  # noqa: PLC0415

        candidates.append(str(DATA_DIR))
    except Exception:  # noqa: BLE001 — best-effort: skip if tools unimportable
        pass
    candidates.extend(r for r in extra_roots if (r or "").strip())

    seen: dict[str, dict] = {}
    for raw in candidates:
        volume = _volume_of(raw)
        if not _is_removable(volume):
            continue
        mount = str(volume)
        if mount in seen:
            continue
        seen[mount] = _result(
            f"RemovableVolume:{mount}",
            f"Removable volume ({volume.name})",
            _probe_filesystem(mount),
            settings_pane=_PANE_REMOVABLE,
            detail_overrides={
                "denied": (
                    f"Access to the removable volume {volume.name!r} was denied. "
                    "Estormi reads its own files there — grant it access in System "
                    "Settings → Privacy & Security → Files and Folders, then restart "
                    "Estormi."
                )
            },
        )
    return list(seen.values())

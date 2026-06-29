"""CloudKit doorbell — let Apple deliver the "new briefing" banner.

Option B of the notification design: the Mac holds no push key at all. A
small signed helper app (``EstormiCloud.app``, source in
``apps/estormi-cloud/``) writes one tiny ``Briefing`` record into the
user's PRIVATE CloudKit database; a ``CKQuerySubscription`` saved by the iOS
companion makes Apple sign and deliver the visible banner. The helper is a
separate .app because CloudKit is a *restricted* entitlement that a bare
executable can never claim — this module just execs its inner Mach-O.

Everything degrades gracefully: when the helper is missing, the doorbell is
disabled, or the helper's code signature is not the expected team's, every
entry point is a silent no-op returning ``False`` and the caller falls back
to the direct APNs path (see ``vault_sync._notify_new_briefing``).

The helper and its config live at the **config home** (``config_home()/bin`` —
``~/Library/Application Support/Estormi`` by default), NOT inside the data
library: it is a signed, entitlement-bearing machine tool, so it belongs with
the never-relocated config home rather than the movable library. A legacy
install under ``$ESTORMI_DATA_DIR/bin`` is still honoured (resolver fallback)
and promoted to the config home at startup by
:func:`migrate_helper_to_config_home`, so relocating the library never strands
the doorbell.

Setup (full walkthrough in ``docs/cloudkit-doorbell.md``):

  * ``make doorbell DOORBELL_TEAM=<team id>`` builds + installs the helper at
    ``config_home()/bin/EstormiCloud.app``
  * pin the team whose signature you trust via the env var
    ``$ESTORMI_DOORBELL_TEAM_ID`` (or the ``ESTORMI_APNS_TEAM_ID`` already used
    by the APNs path), or ``"team_id"`` in
    ``config_home()/doorbell_config.json``. Without it the doorbell stays a
    no-op — it never trusts an unspecified signer.
  * enable via ``config_home()/doorbell_config.json`` →
    ``{"enabled": true}`` (or ``ESTORMI_DOORBELL_ENABLED=1``). A distributed
    build ships this config pre-set alongside the bundled helper (see the
    ``bundle`` make target). Default-on is safe: a record nobody subscribes to
    yet is harmless — CloudKit auto-purges it within days — and a download Mac
    holds no APNs key, so there is no fallback channel to "lose" by ringing
    early. The user enables iOS alerts once (which saves the subscription); a
    briefing composed before that self-heals on the next run.

Smoke-test (from the repo root)::

    python -m estormi_ingestion.shared.delivery.cloudkit_doorbell --test
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

import structlog

from memory_core.datadir import config_home

from ..paths import estormi_data_dir

log = structlog.get_logger()

# Exit-code contract with apps/estormi-cloud/Sources/main.swift — keep in sync.
_EXIT_OK = 0
_EXIT_NO_ACCOUNT = 2
_EXIT_NETWORK = 3

_SUBPROCESS_TIMEOUT = 30.0


# ── Helper discovery / gating ────────────────────────────────────────────────


def _canonical_helper() -> Path:
    """The fixed, relocation-immune install location (config home)."""
    return Path(config_home()) / "bin" / "EstormiCloud.app"


def _legacy_helper() -> Path:
    """The pre-config-home location, inside the (movable) data library."""
    return estormi_data_dir() / "bin" / "EstormiCloud.app"


def _helper_app() -> Path:
    """Resolve the installed helper.

    Order: the ``ESTORMI_DOORBELL_HELPER`` override → the fixed config home
    (which never moves when the library is relocated) → a legacy
    ``$ESTORMI_DATA_DIR/bin`` install (carried along by a move and promoted to
    the config home by :func:`migrate_helper_to_config_home`). When neither is
    installed, returns the canonical config-home path so "where it should be"
    messaging points at the right place."""
    override = os.environ.get("ESTORMI_DOORBELL_HELPER")
    if override:
        return Path(override).expanduser()
    canonical = _canonical_helper()
    if canonical.exists():
        return canonical
    legacy = _legacy_helper()
    if legacy.exists():
        return legacy
    return canonical


def _helper_binary() -> Path:
    return _helper_app() / "Contents" / "MacOS" / "EstormiCloud"


def migrate_helper_to_config_home() -> bool:
    """Promote a legacy ``$ESTORMI_DATA_DIR/bin`` install to the fixed config
    home, so a library relocation never strands the doorbell helper.

    The helper is a signed, entitlement-bearing machine tool — it belongs with
    the never-relocated config home, not inside the movable data library. This
    one-time, idempotent migration runs at server startup (which a library move
    triggers via the restart): when a helper sits in the old data-dir ``bin`` and
    none is installed at the config home yet, ``ditto`` it across (preserving the
    code signature), verify it, carry its ``doorbell_config.json`` alongside, and
    drop the legacy copy. Best-effort — any failure leaves the legacy install in
    place (still found via the resolver fallback) and never raises. Returns True
    only when a migration actually happened."""
    try:
        canonical = _canonical_helper()
        if canonical.exists():
            return False  # already installed at the fixed location
        legacy = _legacy_helper()
        if not legacy.exists():
            return False  # nothing to migrate
        canonical.parent.mkdir(parents=True, exist_ok=True)
        # ditto, not shutil.copytree: it is the macOS-correct copy for a signed
        # .app (preserves the code signature + xattrs across volumes) — a broken
        # signature would make _verify_team refuse the helper.
        proc = subprocess.run(
            ["/usr/bin/ditto", str(legacy), str(canonical)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode != 0:
            log.warning("doorbell: helper migration ditto failed — %s", (proc.stderr or "").strip())
            shutil.rmtree(canonical, ignore_errors=True)
            return False
        verify = subprocess.run(
            ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(canonical)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if verify.returncode != 0:
            log.warning("doorbell: migrated helper failed codesign verify — keeping legacy")
            shutil.rmtree(canonical, ignore_errors=True)
            return False
        legacy_cfg = estormi_data_dir() / "doorbell_config.json"
        canonical_cfg = Path(config_home()) / "doorbell_config.json"
        if legacy_cfg.is_file() and not canonical_cfg.is_file():
            try:
                shutil.copy2(legacy_cfg, canonical_cfg)
                legacy_cfg.unlink(missing_ok=True)
            except OSError:
                log.warning("doorbell: could not migrate doorbell_config.json")
        shutil.rmtree(legacy, ignore_errors=True)
        log.info("doorbell: migrated helper to %s", canonical)
        return True
    except Exception:
        log.exception("doorbell: helper migration failed")
        return False


def is_configured() -> bool:
    """True when the helper app is installed and executable."""
    binary = _helper_binary()
    return binary.is_file() and os.access(binary, os.X_OK)


def _config_path() -> Path:
    """``doorbell_config.json`` location — the config home (where the helper now
    lives) first, a legacy ``$ESTORMI_DATA_DIR`` copy as a fallback."""
    canonical = Path(config_home()) / "doorbell_config.json"
    if canonical.is_file():
        return canonical
    legacy = estormi_data_dir() / "doorbell_config.json"
    if legacy.is_file():
        return legacy
    return canonical


def _doorbell_config() -> dict[str, object]:
    """Parse ``doorbell_config.json`` (``{}`` when absent or unreadable)."""
    cfg_path = _config_path()
    if not cfg_path.is_file():
        return {}
    try:
        return json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        log.exception("doorbell: cannot parse %s", cfg_path)
        return {}


def is_enabled() -> bool:
    """Whether the doorbell is switched on — an explicit flag, distinct from mere
    installation. A distributed build ships ``enabled: true`` in the bundled
    config (default-on is safe: an unsubscribed record is harmless and CloudKit
    auto-purges it within days, and a download Mac has no APNs key to fall back
    to). A self-builder who pins their own team leaves it off until they have
    confirmed their iPhone subscription end-to-end."""
    env = (os.environ.get("ESTORMI_DOORBELL_ENABLED") or "").strip().lower()
    if env in {"1", "true", "yes"}:
        return True
    if env in {"0", "false", "no"}:
        return False
    return bool(_doorbell_config().get("enabled"))


def _expected_team() -> str | None:
    """The signing team whose helper we agree to exec, from the env var or
    ``doorbell_config.json``; ``None`` when unset.

    A Team ID is a public identifier (it is readable in every signed binary) —
    pinning it only anchors *which* signature we trust, it grants nothing by
    itself — but we never ship a default: an unconfigured doorbell trusts no
    signer and no-ops, exactly like the APNs path with no Team ID. Falls back
    to ``ESTORMI_APNS_TEAM_ID`` so a single-team self-builder configures once."""
    env = (
        os.environ.get("ESTORMI_DOORBELL_TEAM_ID") or os.environ.get("ESTORMI_APNS_TEAM_ID") or ""
    ).strip()
    if env and env.lower() != "not set":
        return env
    team = str(_doorbell_config().get("team_id") or "").strip()
    # ``not set`` is the literal string ``codesign`` reports as the
    # ``TeamIdentifier`` of an ad-hoc signature — never accept it as a pin, or a
    # misconfigured config would coincide with the sentinel and trust an ad-hoc
    # (tampered/mis-built) helper.
    if team and team.lower() != "not set":
        return team
    return None


def _verify_team(app: Path) -> bool:
    """Refuse to exec a helper unless its signature VALIDATES and is the expected
    team's.

    Two checks, in order: first ``codesign --verify --deep --strict`` re-hashes
    the code and confirms the bytes still match the seal (``-dv`` alone only
    reads the stored signature metadata, so a tampered-but-signed Mach-O would
    pass the team parse) — this matches the install/migration paths. Then
    ``codesign -dv`` reads ``TeamIdentifier``; ``not set`` (ad-hoc) is refused,
    as is any team other than the expected one. Never raises."""
    expected = _expected_team()
    if not expected:
        log.warning(
            "doorbell: no team id configured (set ESTORMI_DOORBELL_TEAM_ID or "
            '"team_id" in doorbell_config.json) — refusing to trust %s',
            app,
        )
        return False
    try:
        verify = subprocess.run(
            ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if verify.returncode != 0:
            log.warning(
                "doorbell: codesign --verify failed for %s (%s) — refusing",
                app,
                verify.stderr.strip(),
            )
            return False
        proc = subprocess.run(
            ["/usr/bin/codesign", "-dv", str(app)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        # codesign writes the details to stderr.
        for line in proc.stderr.splitlines():
            if line.startswith("TeamIdentifier="):
                team = line.split("=", 1)[1].strip()
                # ``not set`` is the ad-hoc sentinel — never trust it, even if a
                # pin happened to match the literal string.
                if team and team.lower() != "not set" and team == expected:
                    return True
                log.warning(
                    "doorbell: helper at %s is signed by %r, expected %r — refusing",
                    app,
                    team,
                    expected,
                )
                return False
        log.warning("doorbell: no TeamIdentifier in codesign output for %s — refusing", app)
        return False
    except Exception:
        log.exception("doorbell: codesign verification failed for %s", app)
        return False


# ── Ringing ──────────────────────────────────────────────────────────────────


def send_doorbell(title: str, body: str, date: str) -> bool:
    """Ring the doorbell: have the helper write one Briefing record.

    Returns True only when the record was accepted by CloudKit (the caller
    may then skip the APNs fallback). False on every other outcome — helper
    absent, doorbell disabled, signature mismatch, no iCloud session,
    network trouble, timeout. Never raises."""
    try:
        if not is_enabled():
            log.debug("doorbell: not enabled — skipping")
            return False
        if not is_configured():
            log.debug("doorbell: helper not installed — skipping")
            return False
        app = _helper_app()
        if not _verify_team(app):
            return False
        proc = subprocess.run(
            [str(_helper_binary()), "--title", title, "--body", body, "--date", date],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )
        if proc.returncode == _EXIT_OK:
            log.info("doorbell: rang for %s", date)
            return True
        detail = (proc.stderr or proc.stdout).strip()
        if proc.returncode == _EXIT_NO_ACCOUNT:
            log.warning("doorbell: no iCloud session on this Mac — %s", detail)
        elif proc.returncode == _EXIT_NETWORK:
            log.warning("doorbell: transient CloudKit/network failure — %s", detail)
        else:
            log.warning("doorbell: helper failed (exit %d) — %s", proc.returncode, detail)
        return False
    except subprocess.TimeoutExpired:
        log.warning("doorbell: helper timed out after %.0fs", _SUBPROCESS_TIMEOUT)
        return False
    except Exception:
        log.exception("doorbell: unexpected failure")
        return False


# ── CLI ──────────────────────────────────────────────────────────────────────


def _main(argv: list[str]) -> int:
    import argparse
    import datetime

    parser = argparse.ArgumentParser(description="Estormi CloudKit doorbell utility")
    parser.add_argument("--test", action="store_true", help="ring a test doorbell")
    parser.add_argument("--status", action="store_true", help="show helper + config state")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    if args.status or not args.test:
        app = _helper_app()
        print(f"helper:     {app} ({'installed' if is_configured() else 'MISSING'})")
        print(f"enabled:    {is_enabled()} (doorbell_config.json or ESTORMI_DOORBELL_ENABLED)")
        print(f"team pin:   {_expected_team() or 'UNSET (doorbell will no-op)'}")
        if is_configured():
            print(f"signature:  {'ok' if _verify_team(app) else 'MISMATCH'}")
            proc = subprocess.run(
                [str(_helper_binary()), "--status"], capture_output=True, text=True, timeout=30
            )
            print(f"cloudkit:   {(proc.stdout or proc.stderr).strip()} (exit {proc.returncode})")
        return 0

    today = datetime.date.today().isoformat()
    ok = send_doorbell("Test doorbell", "Estormi CloudKit doorbell is wired up correctly.", today)
    print("Doorbell rang — check the iPhone." if ok else "Doorbell did NOT ring (see logs above).")
    return 0 if ok else 2


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))

"""Direct APNs sender — push a "new briefing" alert to the iOS companion.

Option A of the notification design: no cloud server, no CloudKit. The Mac is
the APNs provider. It signs a JWT with an APNs auth key (.p8) and POSTs to
Apple over HTTP/2. iOS devices drop their push tokens into the vault as
``apns/<vendorID>.json`` (written by ``RemotePushRegistrar``); we read every
token there and fan the alert out to all registered devices.

Everything degrades gracefully: when the key / config is missing (e.g. before
the user joins the Apple Developer Program) every entry point is a silent
no-op, so the briefing pipeline is never affected.

Setup (full walkthrough in ``docs/ios-push-notifications.md``):

  * Apple Developer Program + Push Notifications capability on ``app.estormi.ios``
  * an APNs auth key ``.p8`` plus its Key ID and your Team ID
  * drop the key at ``$ESTORMI_DATA_DIR/apns_auth_key.p8`` (chmod 600), or point
    ``$ESTORMI_APNS_KEY_PATH`` at it
  * config via ``$ESTORMI_DATA_DIR/apns_config.json``
    (``{"key_id": ..., "team_id": ..., "bundle_id": ...}``) or the env vars
    ``ESTORMI_APNS_KEY_ID`` / ``ESTORMI_APNS_TEAM_ID`` / ``ESTORMI_APNS_BUNDLE_ID``

Smoke-test without waiting for a real briefing (from the repo root)::

    python -m estormi_ingestion.shared.delivery.apns_push --test
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import structlog

from ..paths import estormi_data_dir
from .vault_sync import vault_dir

log = structlog.get_logger()

_DEFAULT_BUNDLE_ID = "app.estormi.ios"
_HOSTS = {
    "sandbox": "api.sandbox.push.apple.com",
    "production": "api.push.apple.com",
}
# Provider JWTs are valid up to 60 min and Apple rate-limits regeneration;
# cache and refresh well inside the window.
_JWT_TTL = 50 * 60

_jwt_cache: tuple[str, float, tuple[str, str]] | None = (
    None  # (token, issued_at, (key_id, team_id))
)


# ── Config / key loading ─────────────────────────────────────────────────────


def _key_path() -> Path:
    override = os.environ.get("ESTORMI_APNS_KEY_PATH")
    return Path(override).expanduser() if override else estormi_data_dir() / "apns_auth_key.p8"


def _load_config() -> dict[str, str] | None:
    """Key ID / Team ID / bundle ID, from env vars or ``apns_config.json``.
    Returns ``None`` when the mandatory key/team IDs are absent."""
    cfg: dict[str, Any] = {}
    cfg_path = estormi_data_dir() / "apns_config.json"
    if cfg_path.is_file():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            log.exception("apns: cannot parse %s", cfg_path)
    key_id = os.environ.get("ESTORMI_APNS_KEY_ID") or cfg.get("key_id")
    team_id = os.environ.get("ESTORMI_APNS_TEAM_ID") or cfg.get("team_id")
    bundle_id = (
        os.environ.get("ESTORMI_APNS_BUNDLE_ID") or cfg.get("bundle_id") or _DEFAULT_BUNDLE_ID
    )
    if not key_id or not team_id:
        return None
    return {"key_id": str(key_id), "team_id": str(team_id), "bundle_id": str(bundle_id)}


def _load_key_pem() -> bytes | None:
    path = _key_path()
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except Exception:
        log.exception("apns: cannot read key at %s", path)
        return None


def is_configured() -> bool:
    """True when both the auth key and Key/Team IDs are present."""
    return _load_config() is not None and _load_key_pem() is not None


# ── JWT (ES256) ──────────────────────────────────────────────────────────────


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _provider_jwt(cfg: dict[str, str], key_pem: bytes) -> str:
    global _jwt_cache
    now = time.time()
    identity = (cfg["key_id"], cfg["team_id"])
    # Key the cache on the config identity too: a mid-process key_id/team_id change
    # must not serve a JWT carrying the stale ``kid``/``iss``.
    if _jwt_cache and (now - _jwt_cache[1]) < _JWT_TTL and _jwt_cache[2] == identity:
        return _jwt_cache[0]

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    header = {"alg": "ES256", "kid": cfg["key_id"], "typ": "JWT"}
    payload = {"iss": cfg["team_id"], "iat": int(now)}
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64url(json.dumps(payload, separators=(",", ":")).encode())
    )
    key = load_pem_private_key(key_pem, password=None)
    der = key.sign(signing_input.encode("ascii"), ec.ECDSA(hashes.SHA256()))
    # APNs wants the raw R||S pair (64 bytes), not the DER envelope that
    # ``cryptography`` returns.
    r, s = decode_dss_signature(der)
    raw_sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    token = signing_input + "." + _b64url(raw_sig)
    _jwt_cache = (token, now, identity)
    return token


# ── Device tokens (from the vault) ───────────────────────────────────────────


def _apns_dir() -> Path:
    return vault_dir() / "apns"


def _read_devices() -> list[dict[str, Any]]:
    d = _apns_dir()
    if not d.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(d.glob("*.json")):
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            log.warning("apns: skipping unreadable token file %s", p.name)
            continue
        token = str(obj.get("token") or "").strip()
        if not token:
            continue
        out.append(
            {
                "file": p,
                "token": token,
                "environment": str(obj.get("environment") or "production"),
                "bundle_id": obj.get("bundleId"),
            }
        )
    return out


# ── Sending ──────────────────────────────────────────────────────────────────


def _post(
    client, host: str, token: str, jwt: str, bundle_id: str, payload: dict
) -> tuple[int, str]:
    resp = client.post(
        f"https://{host}/3/device/{token}",
        headers={
            "authorization": f"bearer {jwt}",
            "apns-topic": bundle_id,
            "apns-push-type": "alert",
            "apns-priority": "10",
        },
        json=payload,
    )
    reason = ""
    if resp.status_code != 200:
        try:
            reason = str(resp.json().get("reason", ""))
        except Exception:
            reason = resp.text[:200]
    return resp.status_code, reason


def send_alert(title: str, body: str) -> int:
    """Fan an alert push out to every registered device. Returns the number of
    devices the push was accepted for. Never raises."""
    try:
        cfg = _load_config()
        key_pem = _load_key_pem()
        if not cfg or not key_pem:
            log.debug("apns: not configured — skipping push")
            return 0
        devices = _read_devices()
        if not devices:
            log.debug("apns: no registered devices — skipping push")
            return 0

        env_override = (os.environ.get("ESTORMI_APNS_ENV") or "").strip().lower() or None
        payload = {"aps": {"alert": {"title": title, "body": body}, "sound": "default"}}

        import httpx

        jwt = _provider_jwt(cfg, key_pem)
        sent = 0
        with httpx.Client(http2=True, timeout=10.0) as client:
            for dev in devices:
                env = env_override or dev["environment"]
                host = _HOSTS.get(env, _HOSTS["production"])
                bundle_id = dev.get("bundle_id") or cfg["bundle_id"]
                try:
                    status, reason = _post(client, host, dev["token"], jwt, bundle_id, payload)
                except Exception as exc:
                    log.warning("apns: send failed for %s: %s", dev["file"].name, exc)
                    continue
                if status == 200:
                    sent += 1
                elif status == 410 or reason == "Unregistered":
                    # The device uninstalled / disabled the app: Apple says this
                    # token is permanently dead, so prune it.
                    log.info("apns: pruning dead token %s", dev["file"].name)
                    try:
                        dev["file"].unlink()
                    except Exception:
                        log.warning("apns: failed to prune dead token %s", dev["file"].name)
                elif reason == "BadDeviceToken":
                    # Almost always an environment mismatch (a sandbox token sent
                    # to the production host or vice-versa). Do NOT prune — the app
                    # only rewrites the file when the token rotates, so deleting a
                    # still-valid token would silence the device until reinstall.
                    log.warning(
                        "apns: BadDeviceToken for %s — check sandbox/production "
                        "(token env=%s, host=%s)",
                        dev["file"].name,
                        dev["environment"],
                        host,
                    )
                else:
                    log.warning("apns: %s rejected (%s %s)", dev["file"].name, status, reason)
        if sent:
            log.info("apns: pushed to %d device(s)", sent)
        return sent
    except Exception:
        log.exception("apns: unexpected failure")
        return 0


# ── CLI ──────────────────────────────────────────────────────────────────────


def _main(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Estormi APNs push utility")
    parser.add_argument("--test", action="store_true", help="send a test push to all devices")
    parser.add_argument("--list", action="store_true", help="list registered device tokens")
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

    if args.list:
        devices = _read_devices()
        if not devices:
            print(f"No registered devices in {_apns_dir()}")
            return 0
        for dev in devices:
            print(f"{dev['file'].name}: {dev['environment']:>10} · {dev['token'][:12]}…")
        return 0

    if not is_configured():
        print("APNs is not configured. See docs/ios-push-notifications.md")
        print(f"  expected key:    {_key_path()}")
        print(f"  expected config: {estormi_data_dir() / 'apns_config.json'}")
        return 1

    count = send_alert("Test push", "Estormi APNs is wired up correctly.")
    print(f"Pushed to {count} device(s).")
    return 0 if count else 2


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))

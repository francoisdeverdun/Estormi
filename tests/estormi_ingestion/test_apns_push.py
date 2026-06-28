"""Unit tests for estormi_ingestion.shared.delivery.apns_push.

The end-to-end push can only be exercised with a real Apple Developer account,
so these tests pin the parts that are pure logic: configuration discovery, the
ES256 provider-JWT (the bit that is impossible to debug remotely), device-token
reading, and the graceful no-op when APNs is not set up.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from estormi_ingestion.shared.delivery import apns_push


def _b64url_decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


@pytest.fixture(autouse=True)
def _reset_jwt_cache() -> None:
    apns_push._jwt_cache = None


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "Estormi"
    monkeypatch.setenv("ESTORMI_VAULT_DIR", str(d))
    return d


@pytest.fixture
def p8_key(tmp_path: Path) -> bytes:
    """A throwaway P-256 private key in PKCS8 PEM — same shape as an APNs .p8."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_not_configured_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ESTORMI_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("ESTORMI_APNS_KEY_ID", raising=False)
    monkeypatch.delenv("ESTORMI_APNS_TEAM_ID", raising=False)
    assert apns_push.is_configured() is False


@pytest.mark.unit
def test_configured_via_env_and_key_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, p8_key: bytes
) -> None:
    key_path = tmp_path / "AuthKey.p8"
    key_path.write_bytes(p8_key)
    monkeypatch.setenv("ESTORMI_APNS_KEY_PATH", str(key_path))
    monkeypatch.setenv("ESTORMI_APNS_KEY_ID", "ABC1234567")
    monkeypatch.setenv("ESTORMI_APNS_TEAM_ID", "TEAM123456")
    assert apns_push.is_configured() is True


# ---------------------------------------------------------------------------
# provider JWT (ES256)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_provider_jwt_is_valid_es256(p8_key: bytes) -> None:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    cfg = {"key_id": "KEY1234567", "team_id": "TEAM987654", "bundle_id": "app.estormi.ios"}
    token = apns_push._provider_jwt(cfg, p8_key)
    header_b64, payload_b64, sig_b64 = token.split(".")

    assert json.loads(_b64url_decode(header_b64)) == {
        "alg": "ES256",
        "kid": "KEY1234567",
        "typ": "JWT",
    }
    payload = json.loads(_b64url_decode(payload_b64))
    assert payload["iss"] == "TEAM987654"
    assert isinstance(payload["iat"], int)

    sig = _b64url_decode(sig_b64)
    assert len(sig) == 64  # raw R||S, not DER

    # Reconstruct the DER signature and verify against the public key.
    public_key = serialization.load_pem_private_key(p8_key, password=None).public_key()
    r = int.from_bytes(sig[:32], "big")
    s = int.from_bytes(sig[32:], "big")
    public_key.verify(
        encode_dss_signature(r, s),
        f"{header_b64}.{payload_b64}".encode(),
        ec.ECDSA(hashes.SHA256()),
    )


@pytest.mark.unit
def test_provider_jwt_is_cached(p8_key: bytes) -> None:
    cfg = {"key_id": "K", "team_id": "T", "bundle_id": "b"}
    assert apns_push._provider_jwt(cfg, p8_key) == apns_push._provider_jwt(cfg, p8_key)


# ---------------------------------------------------------------------------
# device tokens
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_read_devices_parses_and_skips_junk(vault: Path) -> None:
    apns = vault / "apns"
    apns.mkdir(parents=True)
    (apns / "phone.json").write_text(
        json.dumps({"token": "abc123", "environment": "sandbox", "bundleId": "app.estormi.ios"}),
        encoding="utf-8",
    )
    (apns / "no-token.json").write_text(json.dumps({"environment": "sandbox"}), encoding="utf-8")
    (apns / "broken.json").write_text("{not json", encoding="utf-8")

    devices = apns_push._read_devices()
    assert len(devices) == 1
    assert devices[0]["token"] == "abc123"
    assert devices[0]["environment"] == "sandbox"


@pytest.mark.unit
def test_read_devices_empty_when_no_folder(vault: Path) -> None:
    assert apns_push._read_devices() == []


# ---------------------------------------------------------------------------
# graceful degradation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_send_alert_noop_when_unconfigured(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ESTORMI_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("ESTORMI_APNS_KEY_ID", raising=False)
    monkeypatch.delenv("ESTORMI_APNS_TEAM_ID", raising=False)
    assert apns_push.send_alert("t", "b") == 0


@pytest.mark.unit
def test_send_alert_noop_when_no_devices(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, p8_key: bytes
) -> None:
    key_path = tmp_path / "AuthKey.p8"
    key_path.write_bytes(p8_key)
    monkeypatch.setenv("ESTORMI_APNS_KEY_PATH", str(key_path))
    monkeypatch.setenv("ESTORMI_APNS_KEY_ID", "ABC1234567")
    monkeypatch.setenv("ESTORMI_APNS_TEAM_ID", "TEAM123456")
    # Configured, but no device tokens in the vault → still a no-op, no network.
    assert apns_push.send_alert("t", "b") == 0


# ---------------------------------------------------------------------------
# send_alert loop — per-device outcomes
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Minimal stand-in for an ``httpx`` response from the APNs HTTP/2 endpoint."""

    def __init__(self, status_code: int, reason: str | None = None) -> None:
        self.status_code = status_code
        self._reason = reason
        self.text = json.dumps({"reason": reason}) if reason else ""

    def json(self) -> dict[str, Any]:
        if self._reason is None:
            raise ValueError("no JSON body")
        return {"reason": self._reason}


class _FakeClient:
    """Context-manager stand-in for ``httpx.Client`` that yields a queued
    response (or raises a queued exception) per ``.post`` call, in token order.

    Records the device tokens it was POSTed to so a test can assert *which*
    devices were contacted, not merely that the boundary was touched.
    """

    def __init__(self, outcomes: list) -> None:
        self._outcomes = list(outcomes)
        self.posted_tokens: list[str] = []

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def post(self, url: str, **kwargs):
        self.posted_tokens.append(url.rsplit("/", 1)[-1])
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, p8_key: bytes) -> None:
    """Set up a valid key + Key/Team IDs so ``send_alert`` reaches the send loop."""
    key_path = tmp_path / "AuthKey.p8"
    key_path.write_bytes(p8_key)
    monkeypatch.setenv("ESTORMI_APNS_KEY_PATH", str(key_path))
    monkeypatch.setenv("ESTORMI_APNS_KEY_ID", "ABC1234567")
    monkeypatch.setenv("ESTORMI_APNS_TEAM_ID", "TEAM123456")
    # A real environment value would route a sandbox token to the sandbox host;
    # leave it unset so each device uses its own stored ``environment``.
    monkeypatch.delenv("ESTORMI_APNS_ENV", raising=False)


def _write_token(vault: Path, name: str, token: str, environment: str = "production") -> Path:
    apns = vault / "apns"
    apns.mkdir(parents=True, exist_ok=True)
    path = apns / f"{name}.json"
    path.write_text(json.dumps({"token": token, "environment": environment}), encoding="utf-8")
    return path


def _install_fake_client(monkeypatch: pytest.MonkeyPatch, outcomes: list) -> _FakeClient:
    """Patch ``httpx.Client`` (imported lazily inside ``send_alert``) with a fake
    that replays ``outcomes`` per POST. Returns the instance for inspection."""
    import httpx

    client = _FakeClient(outcomes)
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: client)
    return client


@pytest.mark.integration
def test_send_alert_410_prunes_dead_token(
    vault: Path, configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 410 (Apple's "this token is permanently dead") prunes the token file so
    a dead device is never retried."""
    dead = _write_token(vault, "dead-phone", "DEADTOKEN")
    _install_fake_client(monkeypatch, [_FakeResponse(410, "Unregistered")])

    sent = apns_push.send_alert("New briefing", "Tap to read")

    assert sent == 0
    assert not dead.exists(), "a 410 must prune the dead token file"


@pytest.mark.integration
def test_send_alert_bad_device_token_does_not_prune(
    vault: Path, configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``BadDeviceToken`` is almost always a sandbox/production mismatch, not a
    dead device — the file must survive so a still-valid token isn't silenced
    until the app reinstalls."""
    survivor = _write_token(vault, "mismatched", "MISMATCHTOKEN")
    _install_fake_client(monkeypatch, [_FakeResponse(400, "BadDeviceToken")])

    sent = apns_push.send_alert("New briefing", "Tap to read")

    assert sent == 0
    assert survivor.exists(), "BadDeviceToken must NOT prune the token file"


@pytest.mark.integration
def test_send_alert_prunes_only_the_dead_token(
    vault: Path, configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Across a mixed fan-out, only the 410 token is pruned; the accepted and the
    BadDeviceToken files both survive, and the accepted device is counted."""
    good = _write_token(vault, "a-good", "GOODTOKEN")
    dead = _write_token(vault, "b-dead", "DEADTOKEN")
    mismatched = _write_token(vault, "c-mismatch", "MISMATCHTOKEN")
    # Files are read in sorted order: a-good, b-dead, c-mismatch.
    client = _install_fake_client(
        monkeypatch,
        [
            _FakeResponse(200),
            _FakeResponse(410, "Unregistered"),
            _FakeResponse(400, "BadDeviceToken"),
        ],
    )

    sent = apns_push.send_alert("New briefing", "Tap to read")

    assert sent == 1
    assert client.posted_tokens == ["GOODTOKEN", "DEADTOKEN", "MISMATCHTOKEN"]
    assert good.exists()
    assert not dead.exists()
    assert mismatched.exists()


@pytest.mark.integration
def test_send_alert_never_raises_on_transport_error(
    vault: Path, configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A per-device transport error is swallowed (the contract is *never raise*),
    the device is simply not counted, and the loop continues to the next one —
    the briefing pipeline must never be taken down by a push failure."""
    import httpx

    _write_token(vault, "a-flaky", "FLAKYTOKEN")
    good = _write_token(vault, "b-good", "GOODTOKEN")
    _install_fake_client(
        monkeypatch,
        [httpx.ConnectError("connection reset"), _FakeResponse(200)],
    )

    # Must not raise, and must still deliver to the healthy device after the
    # flaky one errored.
    sent = apns_push.send_alert("New briefing", "Tap to read")

    assert sent == 1
    assert good.exists()

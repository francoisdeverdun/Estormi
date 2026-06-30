"""Shared OAuth-token storage: keyring-first, with a chmod-600 file fallback.

The WHOOP and Google-Calendar connectors persist their OAuth token dict the same
way — in the system keyring under ``(service, key)``, falling back to an atomic,
permission-locked file when keyring is unavailable (headless / locked login
keychain). This module is the single home for that triad so the two connectors
can't drift; each passes its own ``service`` name, keyring ``key``, and
``token_file`` path.

The file write is atomic (temp-then-rename) with a **PID-suffixed** temp name,
so two processes writing the same token file — e.g. the in-process engine and a
manually-launched ``make daily-dag`` — can't clobber each other's temp or leave
a torn/empty file (the same guard ``vault_sync._atomic_write_json`` uses).
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any

import structlog

log = structlog.get_logger()


def save_token(service: str, key: str, data: dict[str, Any], *, token_file: str) -> None:
    """Persist a token dict. Tries keyring first, then a chmod-600 file."""
    payload = json.dumps(data)
    try:
        import keyring  # type: ignore  # noqa: PLC0415

        keyring.set_password(service, key, payload)
        return
    except Exception as e:  # noqa: BLE001
        log.warning("keyring save failed, falling back to file: %s", e)

    os.makedirs(os.path.dirname(token_file), exist_ok=True)
    # Write-and-rename keeps the previous token readable if the new one never
    # lands; the PID suffix keeps concurrent writers from racing on one temp.
    # Open the temp 0o600 from the start so the OAuth secret is never
    # world-readable, not even for the instant between write and chmod. O_TRUNC
    # (not O_EXCL) so a leftover temp from a crashed prior run with the same PID
    # self-heals instead of wedging every future save.
    tmp_path = f"{token_file}.{os.getpid()}.tmp"
    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        # A pre-existing temp keeps its old mode (O_CREAT's mode arg is ignored),
        # so re-assert 0o600 best-effort before the rename.
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        os.replace(tmp_path, token_file)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def load_token(service: str, key: str, *, token_file: str) -> dict[str, Any] | None:
    """Read a token dict. Tries keyring first, then the file fallback."""
    try:
        import keyring  # type: ignore  # noqa: PLC0415

        raw = keyring.get_password(service, key)
        if raw:
            return json.loads(raw)
    except Exception as e:  # noqa: BLE001
        log.warning("keyring load failed: %s", e)

    if os.path.exists(token_file):
        try:
            with open(token_file, encoding="utf-8") as f:
                return json.loads(f.read())
        except Exception as e:  # noqa: BLE001
            log.warning("token file unreadable: %s", e)
    return None


def delete_token(service: str, key: str, *, token_file: str) -> None:
    """Remove the stored token from both keyring and the file fallback."""
    try:
        import keyring  # type: ignore  # noqa: PLC0415

        keyring.delete_password(service, key)
    except Exception:  # noqa: BLE001
        pass
    if os.path.exists(token_file):
        try:
            os.remove(token_file)
        except OSError:
            pass


# ─── Keychain-only secrets (no file fallback) ────────────────────────────────
#
# Long-lived app credentials — the OAuth *client* id/secret a connector needs,
# as opposed to the per-user token above — live in the keyring ONLY. Unlike a
# token, a client secret is never mirrored to a cleartext file in the data dir
# (which would otherwise travel with a relocated/backed-up library). Reads and
# writes propagate a keyring fault to the caller rather than silently degrading,
# so the UI can surface a clear "couldn't reach the Keychain" error instead of
# losing the secret. ``migrate_file_to_keyring`` imports any legacy cleartext
# file once, then deletes it.


def save_secret(service: str, key: str, data: dict[str, Any]) -> None:
    """Persist a secret dict to the system keyring. No file fallback."""
    import keyring  # type: ignore  # noqa: PLC0415

    keyring.set_password(service, key, json.dumps(data))


def load_secret(service: str, key: str) -> dict[str, Any] | None:
    """Read a secret dict from the system keyring; ``None`` if absent."""
    import keyring  # type: ignore  # noqa: PLC0415

    raw = keyring.get_password(service, key)
    return json.loads(raw) if raw else None


def delete_secret(service: str, key: str) -> None:
    """Remove a secret from the system keyring (quiet no-op if absent)."""
    import keyring  # type: ignore  # noqa: PLC0415

    try:
        keyring.delete_password(service, key)
    except Exception:  # noqa: BLE001
        pass


def migrate_file_to_keyring(
    service: str,
    key: str,
    *,
    legacy_file: str,
    transform: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Import a legacy cleartext secret file into the keyring, once.

    Returns the migrated dict, or ``None`` if there was nothing to migrate.
    After a successful keyring write the cleartext file is deleted so the
    secret no longer lingers on disk. ``transform`` lets a caller normalise the
    file's shape before storing (e.g. unwrap Google's ``installed``/``web``
    envelope).
    """
    if not os.path.exists(legacy_file):
        return None
    try:
        with open(legacy_file, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:  # noqa: BLE001
        log.warning("legacy secret file unreadable: %s", e)
        return None
    if transform is not None:
        data = transform(data)
    save_secret(service, key, data)
    try:
        os.remove(legacy_file)
    except OSError:
        pass
    log.info("migrated legacy secret file into keyring: %s", legacy_file)
    return data

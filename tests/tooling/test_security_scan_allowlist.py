"""The pre-public PII gate (``scripts/security_scan.py``) stays airtight.

The repo ships publicly under the owner's GitHub account, so that handle inside
the canonical ``github.com`` URL (README download link, clone command) is the
project's real public location, not a leak. ``security_scan.py`` allow-lists
exactly that URL. These tests pin every arm of the gate so a future widening
can't open a hole:

* the owner-URL allow-list in ``_scan_content`` (owner URL passes, the name in
  any other context still trips a personal marker),
* ``_scan_content``'s content detectors (French mobile numbers, secret-style
  assignments) and ``_scan_path``'s sensitive-path guard,
* ``_scan_history``'s author/committer-email allow-list (with the git call
  stubbed so the test never touches a real repo), and
* ``main``'s path-traversal guard (a target outside the repo root is skipped,
  never read).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCAN = REPO_ROOT / "scripts" / "security_scan.py"


def _load():
    spec = importlib.util.spec_from_file_location("security_scan_under_test", SCAN)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def scan():
    """A freshly-imported copy of the security-scan module under test."""
    return _load()


def _content_findings(mod, line: str, *, name: str = "doc.md") -> list[str]:
    """Run the real content detector over a single line, under ``mod.ROOT``."""
    return mod._scan_content(mod.ROOT / name, line)


# --------------------------------------------------------------------------- #
# Owner GitHub-URL allow-list (exercised through the real _scan_content path). #
# --------------------------------------------------------------------------- #


def test_owner_github_url_is_allowed(scan):
    """The canonical github.com/<owner> URL must pass through _scan_content."""
    handle = scan._OWNER_HANDLE  # built by the scanner, never spelled literally here
    download = (
        f'<a href="https://github.com/{handle}/Estormi/releases/latest/download/Estormi.dmg">'
    )
    clone = f"git clone https://github.com/{handle}/Estormi.git"
    assert _content_findings(scan, download) == []
    assert _content_findings(scan, clone) == []


def test_owner_name_outside_github_url_is_still_flagged(scan):
    """The owner handle anywhere but the allow-listed URL trips a marker."""
    handle = scan._OWNER_HANDLE
    # Same name, but not inside the allow-listed github.com/<handle> URL.
    assert _content_findings(scan, f"# maintained by {handle}")
    assert _content_findings(scan, f"contact: {handle}@example.com")
    # A look-alike host must not be treated as the canonical repo URL.
    assert _content_findings(scan, f"https://evil.example/github.com/{handle}-mirror")


# --------------------------------------------------------------------------- #
# _scan_content detectors.                                                     #
# --------------------------------------------------------------------------- #


# A non-fixture French mobile, assembled from fragments so this file — which the
# repo's own pre-commit phone scanner reads — holds no contiguous real-looking
# number; reconstructed at runtime it is complete and the detector must flag it.
# Tail 627884109 is deliberately NOT in FIXTURE_PHONE_DIGITS.
_REAL_TAIL = "6" + "27884109"
_REAL_MOBILE_LINES = [
    f"call him at +33 {_REAL_TAIL[0]} {_REAL_TAIL[1:3]} {_REAL_TAIL[3:5]}"
    f" {_REAL_TAIL[5:7]} {_REAL_TAIL[7:9]} tomorrow",
    f"WhatsApp: 33{_REAL_TAIL}",
    f"mobile 0{_REAL_TAIL}",
]


@pytest.mark.parametrize("line", _REAL_MOBILE_LINES)
def test_scan_content_flags_real_french_mobile(scan, line):
    """A non-fixture French mobile number is flagged as a real-PII leak."""
    findings = _content_findings(scan, line)
    assert findings, f"expected a finding for {line!r}"
    assert "French mobile number" in findings[0]


@pytest.mark.parametrize(
    "tail",
    sorted(_load().FIXTURE_PHONE_DIGITS),
)
def test_scan_content_allows_fixture_phone_digits(scan, tail):
    """Canonical-fake fixture numbers never trip the phone detector."""
    # Render the 9-digit national tail as the bare WhatsApp form 33<6|7>XXXXXXXX.
    line = f"test fixture number 33{tail}"
    phone_findings = [f for f in _content_findings(scan, line) if "French mobile" in f]
    assert phone_findings == []


# Secret-style lines assembled from fragments for the same reason: no contiguous
# `<field> = "<long value>"` literal sits in this scanned file. Reconstructed at
# runtime they are complete and the secret-assignment detector must flag them.
_SECRET_LINES = [
    "api" + "_key = " + '"' + "aBcDeF1234567890" + "ZyXwVu" + '"',
    "secret" + ": " + "0123456789abcdef" + "0123456789",
    "password" + "=" + '"' + "hunter2hunter2" + "hunter2hunter2" + '"',
]


@pytest.mark.parametrize("line", _SECRET_LINES)
def test_scan_content_flags_secret_assignment(scan, line):
    """A long high-entropy value on a key/token-named field is flagged."""
    findings = _content_findings(scan, line)
    assert any("possible secret assignment" in f for f in findings), findings


@pytest.mark.parametrize(
    "line",
    [
        'api_key = "your-api-key-here-goes-something"',
        'token = "<your-token>"',
        'secret = "REPLACE_ME_WITH_THE_REAL_SECRET"',
        "password = secrets.token_urlsafe(32)",  # a call, not a literal
    ],
)
def test_scan_content_does_not_flag_placeholders_or_calls(scan, line):
    """Placeholder values and constructor calls must not look like secrets."""
    findings = _content_findings(scan, line)
    assert not any("possible secret assignment" in f for f in findings), findings


def test_scan_path_flags_sensitive_path(scan):
    """A sensitive file path is flagged by _scan_path without reading the file."""
    findings = scan._scan_path(scan.ROOT / "config" / "id_rsa")
    assert findings
    assert "sensitive file path" in findings[0]


@pytest.mark.parametrize(
    "rel",
    [
        "private.pem",
        "deep/nested/secret.key",
        "docker/.env",
        "config/.netrc",
        "Estormi.mobileprovision",
    ],
)
def test_scan_path_flags_sensitive_path_variants(scan, rel):
    """Each SENSITIVE_PATHS pattern (key, env, netrc, provisioning) trips."""
    findings = scan._scan_path(scan.ROOT / rel)
    assert findings, f"expected {rel!r} to be flagged"


def test_scan_path_allows_safe_path(scan):
    """An explicitly safe path (.env.example) is not flagged."""
    assert scan._scan_path(scan.ROOT / ".env.example") == []


# --------------------------------------------------------------------------- #
# _scan_history author/committer-email allow-list (git call stubbed).          #
# --------------------------------------------------------------------------- #


def _stub_git_log(monkeypatch, scan, stdout: str) -> dict[str, object]:
    """Replace the module's subprocess.run so _scan_history never hits git.

    Returns a dict capturing the args the scanner passed, so the test can
    assert the command shape it relies on.
    """
    captured: dict[str, object] = {}

    class _Proc:
        def __init__(self, out: str) -> None:
            self.stdout = out
            self.returncode = 0

    def fake_run(cmd, *args, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _Proc(stdout)

    monkeypatch.setattr(scan.subprocess, "run", fake_run)
    return captured


def test_scan_history_flags_corporate_email(monkeypatch, scan):
    """A corporate-style author email in history is flagged (exit-1 path)."""
    owner = scan._OWNER_GMAIL
    log = "\n".join(
        [
            f"{owner}|{owner}",
            # Non-allowlisted address (stands in for a corporate/employer email);
            # example.com keeps it out of the test-tree real-data guard.
            "employee@example.com|noreply@github.com",
        ]
    )
    captured = _stub_git_log(monkeypatch, scan, log)

    findings = scan._scan_history()

    assert len(findings) == 1
    assert "employee@example.com" in findings[0]
    assert "non-allowlisted commit email" in findings[0]
    # It must walk all refs and read both author + committer emails.
    assert captured["cmd"] == ["git", "log", "--all", "--format=%ae|%ce"]


def test_scan_history_flags_corporate_committer_too(monkeypatch, scan):
    """A clean author but corporate *committer* address is still flagged."""
    owner = scan._OWNER_GMAIL
    log = f"{owner}|committer@example.org"
    _stub_git_log(monkeypatch, scan, log)

    findings = scan._scan_history()

    assert len(findings) == 1
    assert "committer@example.org" in findings[0]


def test_scan_history_passes_all_allowlisted(monkeypatch, scan):
    """Every owner/co-author/GitHub-noreply address yields no findings."""
    log = "\n".join(
        [
            f"{scan._OWNER_GMAIL}|{scan._OWNER_PRIVATERELAY}",
            "noreply@anthropic.com|noreply@github.com",
            # GitHub no-reply (per-account + legacy forms). Placeholder local
            # parts keep the test-tree real-data guard happy; the allow-list
            # regex only matches the @users.noreply.github.com domain suffix.
            "user@users.noreply.github.com|me@users.noreply.github.com",
            # Case-insensitivity: the allow-list normalizes to lower-case.
            f"{scan._OWNER_GMAIL.upper()}|{scan._OWNER_PRIVATERELAY.upper()}",
        ]
    )
    _stub_git_log(monkeypatch, scan, log)

    assert scan._scan_history() == []


def test_scan_history_deduplicates_repeated_offenders(monkeypatch, scan):
    """The same bad email across many commits collapses to one finding."""
    log = "\n".join(["bad@example.com|bad@example.com"] * 5)
    _stub_git_log(monkeypatch, scan, log)

    findings = scan._scan_history()

    assert len(findings) == 1
    assert "bad@example.com" in findings[0]


def test_main_history_mode_returns_exit_code(monkeypatch, scan, capsys):
    """--history wires _scan_history to a 1/0 exit code through main()."""
    monkeypatch.setattr("sys.argv", ["security_scan.py", "--history"])

    bad = "\n".join(["flagged@example.com|flagged@example.com"])
    _stub_git_log(monkeypatch, scan, bad)
    assert scan.main() == 1
    assert "Security scan failed" in capsys.readouterr().err

    _stub_git_log(monkeypatch, scan, f"{scan._OWNER_GMAIL}|{scan._OWNER_GMAIL}")
    assert scan.main() == 0


# --------------------------------------------------------------------------- #
# main() path-traversal guard.                                                 #
# --------------------------------------------------------------------------- #


def test_main_skips_paths_outside_repo_root(monkeypatch, scan, tmp_path):
    """A target that resolves outside the repo root is skipped, never scanned.

    The guard at the top of main()'s loop does ``path.resolve()`` then
    ``relative_to(root)``; an out-of-tree path raises ValueError and is
    ``continue``-skipped. We plant a file that *would* trip the gate and prove
    main() never reads it (exit 0, and _read_text is never called for it).
    """
    leak = tmp_path / "leak.md"
    leak.write_text(f"maintained by {scan._OWNER_HANDLE}\n", encoding="utf-8")

    read_calls: list[Path] = []
    real_read = scan._read_text

    def spy_read(path):
        read_calls.append(path)
        return real_read(path)

    monkeypatch.setattr(scan, "_read_text", spy_read)
    monkeypatch.setattr("sys.argv", ["security_scan.py", str(leak)])

    assert scan.main() == 0
    assert leak.resolve() not in read_calls


def test_main_scans_in_tree_path_and_flags_it(monkeypatch, scan, capsys):
    """The same kind of leak inside the repo root IS scanned and flagged.

    The mirror of the traversal test: prove the guard rejects only out-of-tree
    targets, not legitimate in-tree ones. We point main() at this very test
    file's directory marker via a synthetic in-tree path whose content we feed
    through a stubbed reader, so no real file on disk needs the leak.
    """
    in_tree = scan.ROOT / "scratch_leak_probe.md"

    def fake_read(path):
        if path == in_tree.resolve():
            return f"maintained by {scan._OWNER_HANDLE}\n"
        return None

    # Make the synthetic path pass _should_scan's is_file()/suffix checks.
    monkeypatch.setattr(scan.Path, "is_file", lambda self: True)
    monkeypatch.setattr(scan, "_read_text", fake_read)
    monkeypatch.setattr("sys.argv", ["security_scan.py", str(in_tree)])

    assert scan.main() == 1
    assert "personal marker found" in capsys.readouterr().err

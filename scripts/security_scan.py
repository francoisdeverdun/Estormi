#!/usr/bin/env python3
"""Repo-local security guardrails for accidental private data commits."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PERSONAL_MARKERS = [
    re.compile("fran" + "cois", re.IGNORECASE),
    re.compile("fran" + "çois", re.IGNORECASE),
    re.compile("ver" + "dun", re.IGNORECASE),
    re.compile("lu" + "cile", re.IGNORECASE),
    re.compile(r"dev\.ver" + r"dun\.estormi", re.IGNORECASE),
]

# The project ships publicly under the owner's GitHub account, so the owner
# handle inside the canonical github.com URL (README download link, clone
# command) is the repo's real public location — not a private-data leak. Allow
# *only* that exact URL prefix; any other appearance of the name still trips
# the markers above. The handle is split the same way the markers are so this
# scanner never flags its own source.
_OWNER_HANDLE = "fran" + "cois" + "de" + "ver" + "dun"
OWNER_URL_ALLOWLIST = re.compile(r"https://github\.com/" + _OWNER_HANDLE, re.IGNORECASE)

# shields.io renders the repo's public badges (the README "Download for macOS"
# release badge, docs/release.md) straight from the canonical
# ``img.shields.io/github/<metric>/<owner>/<repo>`` path, so the owner handle
# there is the same public location as the github.com URL above — not a leak.
# Allow it *only* inside a shields.io GitHub badge path that ends in the exact
# ``<handle>/Estormi``; the handle anywhere else still trips the markers.
OWNER_BADGE_ALLOWLIST = re.compile(
    r"img\.shields\.io/github/[^\s\"')]*?" + _OWNER_HANDLE + r"/Estormi",
    re.IGNORECASE,
)

# Git-history author/committer email allowlist (used by --history mode). Every
# commit's author and committer email must reduce to one of these, or the
# history carries a corporate-style identity leak. The literals are split the
# same way the personal markers are so this scanner never flags its own source
# when it is itself scanned in the working tree.
_OWNER_GMAIL = "fran" + "cois" + "de" + "ver" + "dun" + "@" + "gmail.com"
_OWNER_PRIVATERELAY = "v2m74grzs4" + "@" + "privaterelay.appleid.com"
HISTORY_EMAIL_ALLOWLIST = frozenset(
    {
        _OWNER_GMAIL.lower(),
        _OWNER_PRIVATERELAY.lower(),
        "noreply" + "@" + "anthropic.com",
        # GitHub's *generic* no-reply address, stamped as the committer on
        # commits authored through the web UI and on squash/merge commits. Not
        # tied to any person — shared across all of GitHub — so it is no more a
        # PII leak than the per-account form below.
        "noreply" + "@" + "github.com",
    }
)
# GitHub's per-account no-reply commit address — `<id>+<user>@users.noreply…`
# or the legacy `<user>@users.noreply…`. Allowed for any user.
HISTORY_NOREPLY_DOMAIN = re.compile(r"@users\.noreply\.github\.com$", re.IGNORECASE)

# French mobile numbers in any common formatting. Matches both ``+33 6 12 …``
# / ``06 12 …`` patterns and the bare WhatsApp form ``336…``. We accept the
# match, extract the trailing 9 digits, and only flag if those digits aren't
# in the fixture allowlist below — so tests can keep using canonical-fake
# numbers without tripping the scan.
PHONE_FR_MOBILE = re.compile(
    r"(?<![\w])"
    r"(?:\+33[\s.-]?|0)"
    r"[67](?:[\s.-]?\d{2}){4}"
    r"(?![\w])"
)
PHONE_FR_WA = re.compile(r"(?<!\d)33[67]\d{8}(?!\d)")

# Canonical-fake 9-digit national tails used across tests and docstrings.
# Any committed French mobile that does NOT reduce to one of these is treated
# as a real-PII leak.
FIXTURE_PHONE_DIGITS = frozenset(
    {
        "600000000",  # "unknown caller" placeholder
        "611223344",
        "612345678",  # primary "Maman" test fixture
        "687654321",  # WhatsApp privacy-mask test fixture
        "698765432",  # secondary About-text test fixture
    }
)

SENSITIVE_PATHS = [
    re.compile(r"(^|/)\.env($|[.])"),
    re.compile(r"(^|/)docker/\.env$"),
    re.compile(r"(^|/)packages/estormi_server/estormi\.db$"),
    re.compile(r"(^|/)\.npmrc$"),
    re.compile(r"(^|/)\.pypirc$"),
    re.compile(r"(^|/)\.netrc$"),
    re.compile(r"(^|/)id_(rsa|dsa|ecdsa|ed25519)$"),
    re.compile(r"\.(pem|key|p12|p8|pfx|mobileprovision)$", re.IGNORECASE),
]

SECRET_ASSIGNMENTS = [
    # Match a long high-entropy-looking value assigned to a key/token-named
    # field. The previous version embedded a negative lookahead trying to
    # filter "example", "placeholder", etc. — but the lookahead only
    # anchored at the start of the 16-char run, so a value like
    # ``example-aBcDeF1234567890XYZ`` slipped through because only the
    # first 7 characters were checked against the deny-list. The
    # PLACEHOLDER_PATTERN whole-line check (below) is the right filter
    # and runs against every flagged line. Keep this regex strictly
    # about shape; let placeholder detection happen out-of-band.
    re.compile(
        r"(?i)\b(api[_-]?key|secret|token|password|passwd|private[_-]?key)\b"
        r"\s*[:=]\s*['\"]?[A-Za-z0-9_./+=:@-]{16,}"
    ),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
]

TEXT_EXTENSIONS = {
    ".cfg",
    ".command",
    ".conf",
    ".css",
    ".csv",
    ".env",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".plist",
    ".py",
    ".rs",
    ".sh",
    ".sql",
    ".svg",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
}


def _git_files(include_untracked: bool) -> list[Path]:
    cmd = ["git", "ls-files", "-z"]
    if include_untracked:
        cmd.extend(["--cached", "--others", "--exclude-standard"])
    proc = subprocess.run(cmd, cwd=ROOT, check=True, capture_output=True)
    return [ROOT / p.decode() for p in proc.stdout.split(b"\0") if p]


def _should_scan(path: Path) -> bool:
    if not path.is_file():
        return False
    rel = path.relative_to(ROOT).as_posix()
    if rel.startswith(
        (
            "apps/estormi-macos/target/",
            "python/",
            ".venv/",
            "dist/",
            "node_modules/",
            # Auto-generated graphify cache. Gitignored, regenerated on
            # demand from the working tree, never committed.
            "graphify-out/",
        )
    ):
        return False
    return path.suffix in TEXT_EXTENSIONS or path.name in {
        ".gitignore",
        ".pre-commit-config.yaml",
        "Makefile",
        "CLAUDE.md",
    }


def _read_text(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


# Paths that match a SENSITIVE_PATHS pattern but are explicitly known-safe.
# Add files here only after verifying they cannot leak secrets.
SAFE_PATHS = frozenset(
    {
        ".env.example",
        "packages/web-ui/.env.example",
        # Root pnpm config — contains `node-linker=hoisted`, no auth tokens.
        ".npmrc",
    }
)

# Patterns that look like SECRET_ASSIGNMENTS hits but are obvious placeholders.
PLACEHOLDER_PATTERN = re.compile(
    r"(?i)(your[-_]|sample[-_]|example[-_]|placeholder|change[-_]?me|replace[-_]?me|"
    # The `<...>` placeholder must sit in value position (after `:`/`=`), e.g.
    # `token = "<your-token>"`. Matching `<...>` anywhere would let any line with
    # angle brackets (TS generics, JSX, XML) skip the secret-assignment scan.
    r"insert[-_]|todo[-_]|[:=]\s*['\"]?<[^>]*>|fake[-_]?token)",
)


def _is_placeholder_line(line: str) -> bool:
    """Return True if the line obviously contains a placeholder, not a real secret."""
    return bool(PLACEHOLDER_PATTERN.search(line.strip()))


def _scan_path(path: Path) -> list[str]:
    rel = path.relative_to(ROOT).as_posix()
    findings: list[str] = []
    if rel in SAFE_PATHS:
        return findings
    if any(pattern.search(rel) for pattern in SENSITIVE_PATHS):
        findings.append(f"{rel}: sensitive file path must not be committed")
    return findings


def _phone_tail9(match: str) -> str:
    """Return the trailing 9-digit national tail of a French mobile match."""
    digits = re.sub(r"\D", "", match)
    return digits[-9:]


def _scan_content(path: Path, text: str) -> list[str]:
    rel = path.relative_to(ROOT).as_posix()
    findings: list[str] = []

    for lineno, line in enumerate(text.splitlines(), start=1):
        if any(pattern.search(line) for pattern in PERSONAL_MARKERS):
            # Drop the allow-listed owner GitHub URL and shields.io badge, then
            # re-check: a marker that survives the scrub is a genuine leak; one
            # that doesn't was only the canonical public repo URL / badge.
            scrubbed = OWNER_URL_ALLOWLIST.sub("", line)
            scrubbed = OWNER_BADGE_ALLOWLIST.sub("", scrubbed)
            if any(pattern.search(scrubbed) for pattern in PERSONAL_MARKERS):
                findings.append(f"{rel}:{lineno}: personal marker found")
        for pattern in (PHONE_FR_MOBILE, PHONE_FR_WA):
            for raw in pattern.findall(line):
                if _phone_tail9(raw) not in FIXTURE_PHONE_DIGITS:
                    findings.append(
                        f"{rel}:{lineno}: real-looking French mobile number "
                        f"(use a canonical-fake fixture from "
                        f"scripts/security_scan.FIXTURE_PHONE_DIGITS)"
                    )
                    break
        if _is_placeholder_line(line):
            continue
        for pattern in SECRET_ASSIGNMENTS:
            m = pattern.search(line)
            if not m:
                continue
            # A value immediately followed by "(" is a function/constructor call
            # (e.g. `token = secrets.token_urlsafe(32)` or
            # `token = apns_push._provider_jwt(cfg, key)`), not an embedded secret
            # literal. The high-entropy regex stops at "(" because it isn't in the
            # value char class, so the next char tells calls apart from literals.
            if m.end() < len(line) and line[m.end()] == "(":
                continue
            findings.append(f"{rel}:{lineno}: possible secret assignment found")
            break

    return findings


def _scan_history() -> list[str]:
    """Flag any author/committer email in git history outside the allowlist.

    The working-tree scan can't see who authored each commit, so a corporate
    address baked into the commit metadata (the leak class that prompted this
    mode) survives a clean tree. We walk every ref's author + committer emails
    and flag each distinct one that isn't an owner address, the privaterelay
    alias, a GitHub per-account no-reply, or the Claude no-reply co-author.
    """
    proc = subprocess.run(
        ["git", "log", "--all", "--format=%ae|%ce"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    flagged: set[str] = set()
    for line in proc.stdout.splitlines():
        author, _, committer = line.partition("|")
        for email in (author, committer):
            email = email.strip()
            if not email:
                continue
            normalized = email.lower()
            if normalized in HISTORY_EMAIL_ALLOWLIST:
                continue
            if HISTORY_NOREPLY_DOMAIN.search(normalized):
                continue
            flagged.add(email)
    return [
        f"git history: non-allowlisted commit email {email!r} "
        f"(scrub authorship before going public)"
        for email in sorted(flagged)
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--include-untracked",
        action="store_true",
        help="also scan untracked files that are not ignored",
    )
    parser.add_argument(
        "--history",
        action="store_true",
        help=(
            "scan git commit history (--all refs) for non-allowlisted "
            "author/committer emails instead of the working tree; for "
            "CI/release, not the per-commit hook"
        ),
    )
    parser.add_argument("paths", nargs="*")
    args = parser.parse_args()

    if args.history:
        findings = _scan_history()
        if findings:
            print("Security scan failed:", file=sys.stderr)
            for finding in findings:
                print(f"  - {finding}", file=sys.stderr)
            return 1
        return 0

    paths = (
        [ROOT / path for path in args.paths] if args.paths else _git_files(args.include_untracked)
    )
    findings: list[str] = []

    root = ROOT.resolve()
    for path in paths:
        candidate = path if path.is_absolute() else ROOT / path
        try:
            path = candidate.resolve()
            path.relative_to(root)
        except ValueError:
            continue
        findings.extend(_scan_path(path))
        if not _should_scan(path):
            continue
        text = _read_text(path)
        if text is None:
            continue
        findings.extend(_scan_content(path, text))

    if findings:
        print("Security scan failed:", file=sys.stderr)
        for finding in findings:
            print(f"  - {finding}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

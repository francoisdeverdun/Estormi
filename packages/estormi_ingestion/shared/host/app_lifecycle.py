"""Launch macOS apps before AppleScript export and quit them afterward if we started them.

CLI usage:
    python3 -m estormi_ingestion.shared.host.app_lifecycle --app Notes -- osascript export_notes.applescript 7

The '--' separator is required; everything after it is the command to run.
"""

from __future__ import annotations

import subprocess
import sys
import time
from types import TracebackType

_MAIL_READINESS_SCRIPT = (
    'tell application "Mail"\n'
    '  if (count every account) = 0 then return "0"\n'
    "  set n to 0\n"
    "  repeat with a in every account\n"
    "    try\n"
    "      set n to n + (count every mailbox of a)\n"
    "    end try\n"
    "  end repeat\n"
    '  if n = 0 then return "0"\n'
    "  -- Probe INBOX messages to trigger IMAP header download before the main export.\n"
    "  -- This ensures Mail has synced at least one mailbox so the export won't block.\n"
    "  try\n"
    "    repeat with a in every account\n"
    "      try\n"
    '        set msgCount to count messages of (mailbox "INBOX" of a)\n'
    "        return (n + msgCount) as string\n"
    "      end try\n"
    "    end repeat\n"
    "  end try\n"
    "  return n as string\n"
    "end tell"
)

APP_CONFIG: dict[str, dict] = {
    "Notes": {
        "bundle_id": "com.apple.Notes",
        "process_name": "Notes",
        "warmup_seconds": 5,
    },
    "Mail": {
        "bundle_id": "com.apple.Mail",
        "process_name": "Mail",
        "warmup_seconds": 15,
        # Poll until Mail has loaded message headers (IMAP sync done).
        # Run even when Mail was already running: a pre-existing Mail process may not
        # have re-synced since the last network sleep, and the export AppleScript would
        # then block for the full 18 000 s timeout waiting for IMAP.
        "readiness_check": _MAIL_READINESS_SCRIPT,
        "readiness_timeout": 600,
        "readiness_check_always": True,
    },
    "Calendar": {
        "bundle_id": "com.apple.iCal",
        "process_name": "Calendar",
        "warmup_seconds": 8,
    },
}


def _is_running(process_name: str) -> bool:
    result = subprocess.run(
        ["pgrep", "-x", process_name],
        capture_output=True,
    )
    return result.returncode == 0


def _launch(bundle_id: str) -> None:
    subprocess.run(
        ["open", "-gj", "-b", bundle_id],
        check=False,
    )


def _quit(process_name: str) -> None:
    subprocess.run(
        ["osascript", "-e", f'quit app "{process_name}"'],
        check=False,
        capture_output=True,
    )


def _wait_ready(script: str, timeout_secs: int, process_name: str) -> None:
    """Poll an osascript probe every 5 s until it returns a positive integer."""
    deadline = time.monotonic() + timeout_secs
    while time.monotonic() < deadline:
        try:
            r = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=15,
            )
            val = r.stdout.strip()
            if r.returncode == 0 and val.isdigit() and int(val) > 0:
                return
        except subprocess.TimeoutExpired:
            pass
        time.sleep(5)
    print(
        f"[lifecycle] WARNING: {process_name} readiness check timed out after {timeout_secs}s",
        flush=True,
    )


class AppLifecycle:
    def __init__(self, app_name: str) -> None:
        cfg = APP_CONFIG.get(app_name)
        if cfg is None:
            raise ValueError(f"Unknown app: {app_name!r}. Valid: {list(APP_CONFIG)}")
        self._cfg = cfg
        self._we_launched = False

    def __enter__(self) -> "AppLifecycle":
        process = self._cfg["process_name"]
        bundle = self._cfg["bundle_id"]
        warmup = self._cfg["warmup_seconds"]

        if not _is_running(process):
            print(f"[lifecycle] Launching {process}...", flush=True)
            _launch(bundle)
            self._we_launched = True
            deadline = time.monotonic() + 30
            while not _is_running(process):
                if time.monotonic() > deadline:
                    print(
                        f"[lifecycle] WARNING: {process} did not start in 30s",
                        flush=True,
                    )
                    break
                time.sleep(1)
            if warmup > 0:
                print(
                    f"[lifecycle] Waiting {warmup}s for {process} to initialise...",
                    flush=True,
                )
                time.sleep(warmup)

        # Run the readiness check when we launched the app OR when the config requests
        # it unconditionally (readiness_check_always=True). The always flag is needed
        # for Mail: a pre-existing Mail process may not have re-synced IMAP since the
        # last network sleep, so the export would block for the full timeout.
        should_check = "readiness_check" in self._cfg and (
            self._we_launched or self._cfg.get("readiness_check_always", False)
        )
        if should_check:
            rtimeout = self._cfg.get("readiness_timeout", 60)
            print(
                f"[lifecycle] Polling {process} for readiness (up to {rtimeout}s)...",
                flush=True,
            )
            _wait_ready(self._cfg["readiness_check"], rtimeout, process)
            print(f"[lifecycle] {process} is ready.", flush=True)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._we_launched:
            process = self._cfg["process_name"]
            print(f"[lifecycle] Quitting {process} (we launched it).", flush=True)
            _quit(process)


def main() -> None:
    args = sys.argv[1:]
    try:
        sep = args.index("--")
    except ValueError:
        print(
            "Usage: python3 -m estormi_ingestion.shared.host.app_lifecycle --app <Name> -- <cmd...>",
            file=sys.stderr,
        )
        sys.exit(1)

    lifecycle_args = args[:sep]
    cmd = args[sep + 1 :]

    if not cmd:
        print("No command specified after '--'.", file=sys.stderr)
        sys.exit(1)

    app_name: str | None = None
    i = 0
    while i < len(lifecycle_args):
        if lifecycle_args[i] == "--app" and i + 1 < len(lifecycle_args):
            app_name = lifecycle_args[i + 1]
            i += 2
        else:
            i += 1

    if app_name is None:
        print("--app <Name> is required.", file=sys.stderr)
        sys.exit(1)

    with AppLifecycle(app_name):
        result = subprocess.run(cmd)

    sys.exit(result.returncode)


if __name__ == "__main__":
    main()

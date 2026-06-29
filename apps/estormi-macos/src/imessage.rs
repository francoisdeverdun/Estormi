//! iMessage Full Disk Access bridge.
//!
//! macOS grants Full Disk Access to the app's *main* binary (bundle id
//! `app.estormi.local`), but **not** to the bundled Python sidecar: the
//! re-signed `python3` is treated by TCC as its own responsible process, which
//! is absent from the FDA list, so it is denied even after the user grants the
//! app and relaunches. Empirically confirmed — see the FDA onboarding work.
//!
//! So the access that needs FDA must happen *here*, in the main binary. We
//! snapshot `~/Library/Messages/chat.db` (+ its `-wal`/`-shm` sidecars) into the
//! per-user data dir, and the Python ingestion reads that copy — no FDA needed
//! on its side. `chat.db` is *not* small — a heavy iMessage history runs to
//! hundreds of MB or more — so the copy is potentially expensive: it runs on a
//! background thread (never on the UI/setup thread, see `snapshot_async`) and is
//! debounced. It is refreshed on demand by the loopback `/api/imessage/snapshot`
//! route just before each ingestion run, so the snapshot is current.
//!
//! Consistency of a live WAL-mode copy: we copy the main `chat.db` *first*, then
//! the `-wal`/`-shm` sidecars. Messages may checkpoint (fold `-wal` frames into
//! the db and truncate the wal) at any time, so a raw multi-file copy has an
//! inherent tear window. Ordering db-then-wal keeps that window safe: the wal we
//! capture is always a superset of (or equal to) the db's state, never older
//! than it. SQLite's wal recovery is keyed on the page versions / wal-index
//! salt, so replaying an over-complete wal against the db is idempotent — the
//! sidecar opens the snapshot read-only and recovers a consistent view. Copying
//! wal-then-db would invert this (a fresh wal against an older db) and can
//! corrupt the read; do not reorder. A fully checkpointed source simply has no
//! wal to copy.

use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};

/// Minimum gap between actual `chat.db` copies. Back-to-back ingestion runs (or
/// a misbehaving caller) would otherwise re-copy the whole DB on every POST to
/// `/api/imessage/snapshot`. The snapshot is a single shared resource, so a
/// process-global debounce window is the correct scope.
const SNAPSHOT_DEBOUNCE_SECS: u64 = 10;

/// Unix-seconds of the last *successful-or-attempted* snapshot copy, plus the
/// flag that copy returned. `0` means "never snapshotted this launch", which
/// always lets the first call through.
static LAST_SNAPSHOT_TS: AtomicU64 = AtomicU64::new(0);
static LAST_SNAPSHOT_FLAG: std::sync::Mutex<&'static str> = std::sync::Mutex::new("0");

/// Pure debounce predicate: `true` when a copy made at `last_ts` is recent
/// enough relative to `now` (both Unix seconds) that the snapshot on disk is
/// still current and the copy should be skipped. `last_ts == 0` ("never
/// snapshotted this launch") always lets the call through; a clock regression
/// saturates to 0 elapsed and throttles rather than panicking.
fn within_debounce(last_ts: u64, now: u64) -> bool {
    last_ts != 0 && now.saturating_sub(last_ts) < SNAPSHOT_DEBOUNCE_SECS
}

fn now_secs() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

/// Per-user data dir, mirroring `data_dir()` in `main.rs` and the Python
/// sidecar (`ESTORMI_DATA_DIR` env → relocation pointer → the default
/// `~/Library/Application Support/Estormi`).
fn resolve_data_dir() -> Option<PathBuf> {
    if let Some(p) = std::env::var_os("ESTORMI_DATA_DIR") {
        return Some(PathBuf::from(p));
    }
    std::env::var_os("HOME")
        .map(|h| crate::datadir::resolve(crate::datadir::default_data_dir(PathBuf::from(h))))
}

fn messages_dir() -> Option<PathBuf> {
    std::env::var_os("HOME").map(|h| PathBuf::from(h).join("Library/Messages"))
}

/// Snapshot `chat.db` into `DATA_DIR/imessage/` and (re)write the FDA flag the
/// Python sidecar and the overview read.
///
/// Returns the flag value:
///   - `"1"`      → copied: FDA is granted and the sidecar can read the copy
///   - `"0"`      → open/copy denied: FDA missing, the user must grant it
///   - `"absent"` → no `chat.db` at all (no Messages history): FDA is moot
pub fn snapshot() -> &'static str {
    let state = do_snapshot();
    record_snapshot(state);
    state
}

/// Run [`snapshot`] on a detached background thread. `chat.db` can be large, so
/// the multi-MB copy must never block the caller — at app setup that caller is
/// the main/UI thread. Fire-and-forget: the result is persisted to the FDA flag
/// file by `snapshot` itself, which is all the sidecar reads.
pub fn snapshot_async() {
    std::thread::spawn(|| {
        let _ = snapshot();
    });
}

/// Persist the outcome of a snapshot attempt: update the debounce timestamp and
/// the cached flag the throttled path serves. Split out so `snapshot` stays a
/// thin wrapper and the bookkeeping has one home.
fn record_snapshot(state: &'static str) {
    if let Some(data) = resolve_data_dir() {
        let _ = std::fs::create_dir_all(&data);
        let _ = std::fs::write(data.join("imessage-fda.flag"), state);
    }
    LAST_SNAPSHOT_TS.store(now_secs(), Ordering::SeqCst);
    if let Ok(mut last) = LAST_SNAPSHOT_FLAG.lock() {
        *last = state;
    }
}

/// Debounced snapshot for the loopback `/api/imessage/snapshot` route.
///
/// If a copy ran within the last [`SNAPSHOT_DEBOUNCE_SECS`] seconds, skip the
/// (multi-MB) `chat.db` copy and return the previous flag with `throttled =
/// true`; otherwise run a fresh [`snapshot`]. The existing snapshot on disk is
/// still current within the window, so a throttled caller reads valid data —
/// the route just avoids the redundant I/O.
pub fn snapshot_throttled() -> (&'static str, bool) {
    let last_ts = LAST_SNAPSHOT_TS.load(Ordering::SeqCst);
    if within_debounce(last_ts, now_secs()) {
        let flag = LAST_SNAPSHOT_FLAG.lock().map(|g| *g).unwrap_or("0");
        return (flag, true);
    }
    (snapshot(), false)
}

/// Map a snapshot flag (see [`snapshot`]) to the permission status string the
/// SPA renders. `"1"` (copied) and `"absent"` (no history, nothing to grant)
/// both mean ingestion can proceed → `"authorized"`; `"0"` (denied) means the
/// user must grant Full Disk Access → `"manual"`. Pure so the loopback handler
/// stays a thin wrapper and the mapping is unit-tested.
pub fn snapshot_status(flag: &str) -> &'static str {
    if flag == "1" || flag == "absent" {
        "authorized"
    } else {
        "manual"
    }
}

fn do_snapshot() -> &'static str {
    let (Some(messages), Some(data)) = (messages_dir(), resolve_data_dir()) else {
        return "0";
    };
    let src = messages.join("chat.db");
    let dst_dir = data.join("imessage");

    // Drop any previous snapshot so we never serve stale data when access goes
    // away. A user who had history and then deletes all Messages must not keep
    // ingesting the old copy (fetch_imessages reads the copy if it exists).
    let purge_copy = || {
        for ext in ["chat.db", "chat.db-wal", "chat.db-shm"] {
            let _ = std::fs::remove_file(dst_dir.join(ext));
        }
    };

    // Probe with a real open: a stat() is not gated by TCC the way read access
    // is, so it would report a false "granted" while the sidecar's read fails.
    match std::fs::File::open(&src) {
        Ok(_) => {}
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            purge_copy();
            return "absent";
        }
        // Access went away (TCC denied after a prior grant, or any other open
        // error). Purge the stale copy too — otherwise fetch_imessages keeps
        // re-ingesting the old snapshot instead of surfacing the lost access.
        Err(_) => {
            purge_copy();
            return "0";
        }
    }

    // FDA is granted (the open above succeeded); a failure here is a disk error
    // (e.g. ENOSPC), not denied access. Purge the previous snapshot so we don't
    // re-ingest a now-stale copy, and log the cause to aid diagnosis. We still
    // return "0" — changing the sentinel would require a coordinated change in
    // the Python consumer.
    if let Err(e) = std::fs::create_dir_all(&dst_dir) {
        eprintln!("imessage snapshot: create_dir_all failed (disk error, not FDA): {e}");
        purge_copy();
        return "0";
    }
    if let Err(e) = std::fs::copy(&src, dst_dir.join("chat.db")) {
        eprintln!("imessage snapshot: chat.db copy failed (disk error, not FDA): {e}");
        purge_copy();
        return "0";
    }
    // WAL/SHM sidecars: rows not yet checkpointed live in `-wal`, so copy them
    // alongside for a consistent read-only open of the snapshot. They are copied
    // *after* the main db above — see the module header for why db-then-wal is
    // the consistency-safe order. Best-effort — a checkpointed DB has none. Drop
    // any stale copy when the source is gone, so the snapshot never mixes a fresh
    // db with an old WAL.
    for ext in ["chat.db-wal", "chat.db-shm"] {
        let from = messages.join(ext);
        let to = dst_dir.join(ext);
        if from.exists() {
            let _ = std::fs::copy(&from, &to);
        } else {
            let _ = std::fs::remove_file(&to);
        }
    }
    "1"
}

#[cfg(test)]
mod tests {
    use super::{snapshot_status, within_debounce, SNAPSHOT_DEBOUNCE_SECS};

    #[test]
    fn snapshot_status_maps_copied_and_absent_to_authorized() {
        // A successful copy and "no Messages history at all" both let ingestion
        // proceed — neither needs the user to grant anything.
        assert_eq!(snapshot_status("1"), "authorized");
        assert_eq!(snapshot_status("absent"), "authorized");
    }

    #[test]
    fn snapshot_status_maps_denied_to_manual() {
        // FDA missing/denied: the user must grant access manually.
        assert_eq!(snapshot_status("0"), "manual");
    }

    #[test]
    fn snapshot_status_unknown_flag_defaults_to_manual() {
        // Any unexpected sentinel fails safe toward "needs attention" rather
        // than falsely reporting the source as ready.
        assert_eq!(snapshot_status(""), "manual");
        assert_eq!(snapshot_status("garbage"), "manual");
    }

    #[test]
    fn first_call_is_never_throttled() {
        // 0 is the "never snapshotted this launch" sentinel.
        assert!(!within_debounce(0, 0));
        assert!(!within_debounce(0, u64::MAX));
    }

    #[test]
    fn copy_inside_the_window_is_throttled() {
        assert!(within_debounce(1_000, 1_000));
        assert!(within_debounce(1_000, 1_000 + SNAPSHOT_DEBOUNCE_SECS - 1));
    }

    #[test]
    fn window_boundary_lets_a_fresh_copy_through() {
        assert!(!within_debounce(1_000, 1_000 + SNAPSHOT_DEBOUNCE_SECS));
        assert!(!within_debounce(1_000, u64::MAX));
    }

    #[test]
    fn clock_regression_throttles_instead_of_panicking() {
        // `now` before `last_ts` saturates to 0 elapsed, which is inside the
        // window: the existing snapshot is served rather than underflowing.
        assert!(within_debounce(1_000, 999));
        assert!(within_debounce(u64::MAX, 0));
    }
}

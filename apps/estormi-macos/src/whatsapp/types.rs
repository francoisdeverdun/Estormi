//! Shared state handles, structs, and tuning constants for the WhatsApp
//! sidecar. Kept separate from behaviour so the bot loop, HTTP API, and sync
//! window all import the same vocabulary.
use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicI64, AtomicUsize};
use std::sync::Arc;
use std::time::Instant;

use serde::Deserialize;
use tokio::sync::{Mutex, RwLock};
use whatsapp_rust::client::Client;

pub(super) const WA_API_PORT: u16 = 9877;

/// Header carrying the sidecar API token.
pub(super) const WA_TOKEN_HEADER: &str = "x-estormi-wa-token";

pub(super) type QrState = Arc<RwLock<Option<Vec<u8>>>>;
pub(super) type StatusState = Arc<RwLock<String>>;
pub(super) type GroupCache = Arc<RwLock<HashMap<String, String>>>;
/// Wall-clock instant of the last sync-relevant event (Connected, Message,
/// JoinedGroup). ``sync_once`` watches this to exit early once HistorySync has
/// drained and the live stream goes quiet, instead of always sleeping the full
/// window. ``None`` = sync window is not active.
pub(super) type ActivityTracker = Arc<RwLock<Option<Instant>>>;
/// Set when the server signals it has finished flushing the offline message
/// queue (``Event::OfflineSyncCompleted`` — the "you now have everything queued
/// while you were offline" marker). ``sync_once`` waits on this so the bounded
/// window drains the *full* backlog each run instead of cutting off after a
/// fixed idle gap (which is how messages sent while idle — notably the user's
/// own replies — were being missed). Reset to ``false`` at the start of every
/// sync window.
pub(super) type OfflineFlag = Arc<AtomicBool>;
/// Last-resort label for DMs we couldn't name from Contacts, push_name or
/// HistorySync — populated from the contact's own ``About`` (status) text via
/// ``Client::contacts().get_user_info()`` while a sync window is open. The
/// status may not actually be a name (it can be "Available", a quote, etc.),
/// so consumers must use it strictly as a tail fallback.
pub(super) type StatusCache = Arc<RwLock<HashMap<String, String>>>;
/// Live ``Arc<Client>`` from the running bot, stashed on ``Event::Connected``
/// so ``sync_once`` can drive a one-shot ``get_user_info`` call before tearing
/// the bot down. ``None`` when no sync window is active.
pub(super) type ClientCell = Arc<RwLock<Option<Arc<Client>>>>;

/// On-demand history backfill state for a sync window.
///
/// WhatsApp's linked-device pairing only volunteers a thin recent window. When
/// the bridge (re-)pairs, the bot drives `Client::fetch_message_history` to page
/// progressively older history per chat back to ``cutoff_ms``. This struct is
/// the shared state the JoinedGroup handler reads to decide whether — and from
/// which anchor — to request the next older batch. See ``helpers::should_request_older``.
pub(super) struct BackfillState {
    /// True only during a fresh-pair window whose marker is absent and whose
    /// depth window is non-zero — outside that the handler never requests older.
    pub(super) active: AtomicBool,
    /// Unix-ms horizon: messages older than this are not requested. 0 = off.
    pub(super) cutoff_ms: AtomicI64,
    /// Global request counter, a hard backstop against a runaway paging loop.
    pub(super) requests_made: AtomicUsize,
    /// chat_jid → oldest message ts (ms) we've already paged from, so a
    /// resent/overlapping batch that makes no progress can't loop forever.
    pub(super) requested: Mutex<HashMap<String, i64>>,
}

impl BackfillState {
    pub(super) fn new() -> Self {
        Self {
            active: AtomicBool::new(false),
            cutoff_ms: AtomicI64::new(0),
            requests_made: AtomicUsize::new(0),
            requested: Mutex::new(HashMap::new()),
        }
    }
}

pub(super) type Backfill = Arc<BackfillState>;

#[derive(Clone)]
pub(super) struct BotPaths {
    pub(super) staging: PathBuf,
    pub(super) wa_db: PathBuf,
}

#[derive(Clone)]
pub(super) struct AppState {
    pub(super) qr: QrState,
    pub(super) status: StatusState,
    pub(super) groups: GroupCache,
    pub(super) statuses: StatusCache,
    pub(super) client_cell: ClientCell,
    pub(super) bot_paths: BotPaths,
    pub(super) sync_lock: Arc<Mutex<()>>,
    pub(super) activity: ActivityTracker,
    pub(super) offline_complete: OfflineFlag,
    pub(super) backfill: Backfill,
    pub(super) wa_token: Arc<str>,
}

#[derive(Deserialize)]
pub(super) struct SyncOnceRequest {
    pub(super) seconds: Option<u64>,
    /// History horizon in days for the on-demand backfill (the source's
    /// historic-depth setting, forwarded by watch_and_ingest.sh). Absent on a
    /// manual QR scan, where the sidecar falls back to ``default_backfill_days``.
    pub(super) backfill_days: Option<u64>,
}

pub(super) const DEFAULT_SYNC_SECONDS: u64 = 300;
pub(super) const MIN_SYNC_SECONDS: u64 = 30;
pub(super) const MAX_SYNC_SECONDS: u64 = 1800;
/// Early-exit when the bot is PAIRED and no Message / JoinedGroup event has
/// fired for this long. Picked so HistorySync chunks (which usually arrive
/// within a few seconds of each other) don't trigger a premature exit, while
/// an empty sync window still ends well before the upper cap.
pub(super) const SYNC_IDLE_GAP_SECONDS: u64 = 20;
/// Hard cap on how long sync_once will wait before pairing succeeds. If the
/// session is broken or the user revoked it on the phone, ``CONNECTING`` is
/// the steady state; bail rather than burn the whole window in vain.
pub(super) const SYNC_PAIR_TIMEOUT_SECONDS: u64 = 60;

/// Messages requested per on-demand history page. WhatsApp returns up to this
/// many older messages per `fetch_message_history` call per chat.
pub(super) const BACKFILL_BATCH_COUNT: i32 = 50;
/// Hard backstop on total on-demand requests in one window — a paging loop that
/// somehow never converges still can't hammer the phone indefinitely.
pub(super) const MAX_BACKFILL_REQUESTS: usize = 5000;
/// Upper bound for a backfill window. Paging years of history across many chats
/// takes longer than a routine drain, so it gets its own (larger) cap; the idle
/// gap still ends it early the moment the history stream goes quiet.
pub(super) const BACKFILL_CAP_SECONDS: u64 = 1500;
/// Idle gap for a backfill window — slightly longer than the routine one since
/// each on-demand page is a round-trip to the phone, which can lag a few seconds.
pub(super) const BACKFILL_IDLE_GAP_SECONDS: u64 = 30;

#[cfg(test)]
mod tests {
    use super::SyncOnceRequest;

    #[test]
    fn sync_once_request_seconds_is_optional() {
        let req: SyncOnceRequest = serde_json::from_str("{}").unwrap();
        assert_eq!(req.seconds, None);
        assert_eq!(req.backfill_days, None);
    }

    #[test]
    fn sync_once_request_parses_seconds() {
        let req: SyncOnceRequest = serde_json::from_str(r#"{"seconds": 120}"#).unwrap();
        assert_eq!(req.seconds, Some(120));
    }

    #[test]
    fn sync_once_request_parses_backfill_days() {
        let req: SyncOnceRequest =
            serde_json::from_str(r#"{"seconds": 300, "backfill_days": 730}"#).unwrap();
        assert_eq!(req.seconds, Some(300));
        assert_eq!(req.backfill_days, Some(730));
    }

    #[test]
    fn sync_once_request_rejects_non_u64_seconds() {
        assert!(serde_json::from_str::<SyncOnceRequest>(r#"{"seconds": "soon"}"#).is_err());
        assert!(serde_json::from_str::<SyncOnceRequest>(r#"{"seconds": -5}"#).is_err());
    }
}

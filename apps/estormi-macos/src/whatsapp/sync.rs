//! Bounded sync-window orchestration: arm the activity/offline trackers, spawn
//! the bot, wait for the smart completion signal (offline-drained / idle / cap
//! / pair-timeout), do the one-shot About-text pass, then tear the bot down.
//! Drives both the explicit `/sync-once` path and the lazy QR-poll trigger.
use std::sync::atomic::Ordering;
use std::time::{Duration, Instant};

use anyhow::Result;
use axum::Json;
use chrono::Utc;
use serde_json::{json, Value};

use super::bot::run_whatsapp_bot;
use super::helpers::{
    backfilled_marker_for, cutoff_ms_for_days, fetch_dm_statuses, whatsapp_always_on,
};
use super::types::{
    ActivityTracker, AppState, Backfill, OfflineFlag, StatusState, BACKFILL_CAP_SECONDS,
    BACKFILL_IDLE_GAP_SECONDS, SYNC_IDLE_GAP_SECONDS, SYNC_PAIR_TIMEOUT_SECONDS,
};

pub(super) async fn start_background_sync_if_idle(
    state: AppState,
    seconds: u64,
    backfill_days: u64,
) {
    if whatsapp_always_on() {
        return;
    }

    // Try to acquire the sync lock right here. The SPA polls /api/whatsapp/qr.png
    // (which calls this function) on a tight cadence; without serialising the
    // read-then-write below, two pollers could both see status=IDLE, both flip
    // to CONNECTING, and both spawn workers that race for the SqliteStore on
    // wa.db. try_lock keeps the cheap path cheap (no await) and avoids that
    // double-spawn window entirely.
    let guard = match state.sync_lock.clone().try_lock_owned() {
        Ok(g) => g,
        Err(_) => return, // a sync is already in flight
    };

    let current = state.status.read().await.clone();
    if matches!(current.as_str(), "CONNECTING" | "PAIRED") {
        return;
    }

    *state.status.write().await = "CONNECTING".to_string();

    tokio::spawn(async move {
        let _guard = guard;
        // Same machinery as the explicit sync path — activity tracker, the
        // offline-complete early exit, and the About-text fallback pass — so a
        // QR pairing window stops as soon as the freshly-paired session has
        // drained, and shares one teardown. The pair-timeout is set to the full
        // window so a slow human scan isn't abandoned at 60s (see
        // wait_for_sync_completion).
        let _ = run_sync_window(&state, seconds, seconds, backfill_days).await;
    });
}

/// Arm (or clear) the on-demand backfill for a window. The horizon (`cutoff_ms`)
/// is always set from `backfill_days` so a fresh-QR re-arm mid-window (see the
/// `PairingQrCode` handler) pages to the right depth. `active` starts true only
/// when a positive horizon is configured *and* the `wa.backfilled` marker is
/// absent (so it runs once per pairing); a genuine new QR link re-arms it
/// regardless. Resets the per-chat anchors and request counter so each window
/// starts clean.
async fn arm_backfill(state: &AppState, backfill_days: u64) {
    let cutoff = if backfill_days > 0 {
        cutoff_ms_for_days(backfill_days, Utc::now().timestamp_millis())
    } else {
        0
    };
    let armed = backfill_days > 0 && !backfilled_marker_for(&state.bot_paths.wa_db).exists();
    state.backfill.cutoff_ms.store(cutoff, Ordering::SeqCst);
    state.backfill.requests_made.store(0, Ordering::SeqCst);
    state.backfill.requested.lock().await.clear();
    state.backfill.active.store(armed, Ordering::SeqCst);
    if armed {
        eprintln!("[estormi/wa] backfill armed: horizon {backfill_days}d (cutoff {cutoff}ms)");
    }
}

/// Close out a backfill window: disarm, and stamp the `wa.backfilled` marker
/// **only if we actually paged history this window** (`requests_made > 0`).
///
/// Gating on real progress is load-bearing: a window that armed but received no
/// HistorySync — a reconnect of an existing session, or the first sync after an
/// upgrade still holding the old session (WhatsApp re-pushes history only to a
/// *freshly* linked device) — must NOT stamp the marker, or it would
/// permanently gate the genuine backfill that comes with the next QR pairing.
/// That stale-marker trap is exactly what stranded a re-pair before. Marker is
/// stamped even if the window hit its cap mid-page (re-paging from scratch every
/// run would hammer the phone); reset clears it for a deeper re-pull.
async fn finish_backfill(state: &AppState) {
    let pages = state.backfill.requests_made.load(Ordering::SeqCst);
    state.backfill.active.store(false, Ordering::SeqCst);
    if pages == 0 {
        return;
    }
    let marker = backfilled_marker_for(&state.bot_paths.wa_db);
    match tokio::fs::write(&marker, b"").await {
        Ok(()) => eprintln!("[estormi/wa] backfill done: {pages} page request(s); marker written"),
        Err(e) => eprintln!("[estormi/wa] backfill: failed to write marker {marker:?}: {e}"),
    }
}

/// Outcome of joining the bot task after a sync window: the timeout wrapper
/// around the spawn's join, where the innermost value is the bot's own result.
type BotJoinOutcome = std::result::Result<
    std::result::Result<Result<()>, tokio::task::JoinError>,
    tokio::time::error::Elapsed,
>;

/// Run one bounded WhatsApp sync window with the sync lock already held by the
/// caller: arm the activity tracker + offline-complete flag, spawn the bot, wait
/// for the smart completion signal (offline-drained / idle / cap / pair-timeout),
/// do the one-shot About-text pass, then tear the bot down. Returns the stop
/// reason, elapsed seconds, and the bot task's join outcome so a caller that
/// wants a JSON response can build one. ``pair_timeout_secs`` bounds the pre-pair
/// wait — pass the full window to effectively disable it (QR pairing).
async fn run_sync_window(
    state: &AppState,
    seconds: u64,
    pair_timeout_secs: u64,
    backfill_days: u64,
) -> (SyncStopReason, u64, BotJoinOutcome) {
    // Arm the activity tracker. The event handler watches this slot and only
    // bumps when ``Some(_)`` — outside a sync window the tracker is ``None`` so
    // the always-on path pays nothing. Clear the offline-complete flag so this
    // run's exit reflects *this* run's queue drain, not a stale signal.
    *state.activity.write().await = Some(Instant::now());
    state.offline_complete.store(false, Ordering::SeqCst);
    // Arm on-demand backfill (sets the horizon; active unless the marker says a
    // prior pairing already backfilled). Must happen before the bot spawns so the
    // very first HistorySync pages already drive older-history requests; a fresh
    // QR link re-arms it mid-window even when the marker is present.
    arm_backfill(state, backfill_days).await;

    let (tx, rx) = tokio::sync::watch::channel(false);
    let bot_paths = state.bot_paths.clone();
    let qr = state.qr.clone();
    let status = state.status.clone();
    let groups = state.groups.clone();
    let client_cell = state.client_cell.clone();
    let activity = state.activity.clone();
    let offline_complete = state.offline_complete.clone();
    let backfill = state.backfill.clone();

    let handle = tokio::spawn(async move {
        run_whatsapp_bot(
            bot_paths,
            qr,
            status,
            groups,
            client_cell,
            activity,
            offline_complete,
            backfill,
            rx,
        )
        .await
    });

    let started = Instant::now();
    let reason = wait_for_sync_completion(
        state.status.clone(),
        state.activity.clone(),
        state.offline_complete.clone(),
        seconds,
        pair_timeout_secs,
        state.backfill.clone(),
    )
    .await;
    let elapsed_s = started.elapsed().as_secs();

    // Before tearing down the bot, do a one-shot ``get_user_info`` pass for any
    // DM the cache has surfaced but couldn't name. The contact's About text is
    // saved here as a last-resort label — display code ranks it below every
    // other source because it's often noisy ("Available", a quote, etc.).
    if matches!(
        reason,
        SyncStopReason::OfflineComplete | SyncStopReason::Idle | SyncStopReason::Cap
    ) {
        let _ = tokio::time::timeout(
            Duration::from_secs(15),
            fetch_dm_statuses(
                state.client_cell.clone(),
                state.groups.clone(),
                state.statuses.clone(),
            ),
        )
        .await;
    }

    let _ = tx.send(true);
    // Disarm so the always-on / idle paths skip the activity write again.
    *state.activity.write().await = None;

    let outcome = tokio::time::timeout(Duration::from_secs(15), handle).await;
    // Bot is gone now — drop the client Arc so nothing tries to drive a dead
    // session before the next sync window stashes a fresh one on Connected.
    *state.client_cell.write().await = None;
    // Disarm backfill and, if it actually paged history this window, stamp the
    // marker so the next nightly sync doesn't re-page the whole history.
    finish_backfill(state).await;

    (reason, elapsed_s, outcome)
}

/// Reason ``sync_once`` released the bot before the upper cap, used in the JSON
/// response so the DAG log can explain itself ("idle" vs "timeout" vs "pairing
/// never completed" are very different operational signals).
enum SyncStopReason {
    /// Server signalled the offline queue is fully drained — the clean exit.
    OfflineComplete,
    /// Bot reached PAIRED and the live stream went quiet — fallback when the
    /// offline-complete marker never arrives (e.g. nothing was queued).
    Idle,
    /// The upper cap fired before the stream ever fell quiet.
    Cap,
    /// Bot never reached PAIRED within ``SYNC_PAIR_TIMEOUT_SECONDS``.
    PairTimeout,
}

/// Watch the bot's status + offline-sync flag and return as soon as the sync
/// window can be ended. The primary exit is ``OfflineSyncCompleted`` (the server
/// has flushed everything queued while we were offline); the idle gap is kept
/// only as a fallback for the case where that marker never fires, and the cap is
/// the hard upper bound.
async fn wait_for_sync_completion(
    status: StatusState,
    activity: ActivityTracker,
    offline_complete: OfflineFlag,
    seconds: u64,
    pair_timeout_secs: u64,
    backfill: Backfill,
) -> SyncStopReason {
    let started = Instant::now();
    let poll = Duration::from_millis(500);

    loop {
        tokio::time::sleep(poll).await;
        // Read the backfill state *live*: a fresh QR link can flip it on
        // mid-window (see the PairingQrCode handler). Once on, it widens the cap
        // and idle gap — paging years of history across many chats takes longer
        // than a routine drain, and each on-demand page is a round-trip to the
        // phone — and suppresses the offline-queue early exit, since the
        // on-demand pages keep streaming (as JoinedGroup events that bump
        // activity) well after the queue drains. The idle gap is then the only
        // signal that paging has truly finished.
        let backfilling = backfill.active.load(Ordering::SeqCst);
        let cap_secs = if backfilling {
            BACKFILL_CAP_SECONDS.max(seconds)
        } else {
            seconds
        };
        let idle_gap = Duration::from_secs(if backfilling {
            BACKFILL_IDLE_GAP_SECONDS
        } else {
            SYNC_IDLE_GAP_SECONDS
        });
        let elapsed = started.elapsed();
        if elapsed >= Duration::from_secs(cap_secs) {
            return SyncStopReason::Cap;
        }

        let current = status.read().await.clone();
        let last_activity = *activity.read().await;

        // Bot never paired within the pair-timeout window: it isn't going to.
        // (Common when the session was revoked from the phone — the bot stays
        // CONNECTING forever, burning the full cap for nothing.) The QR pairing
        // window passes the full cap as pair_timeout so a human has the whole
        // window to scan — it must not abort at 60s mid-scan.
        if current != "PAIRED" && elapsed >= Duration::from_secs(pair_timeout_secs.min(cap_secs)) {
            return SyncStopReason::PairTimeout;
        }

        if current == "PAIRED" {
            if !backfilling && offline_complete.load(Ordering::SeqCst) {
                return SyncStopReason::OfflineComplete;
            }
            // The live stream (offline drain and/or backfill paging) has been
            // quiet for the idle gap — everything available has arrived.
            if let Some(t) = last_activity {
                if t.elapsed() >= idle_gap {
                    return SyncStopReason::Idle;
                }
            }
        }
    }
}

pub(super) async fn sync_once(state: AppState, seconds: u64, backfill_days: u64) -> Json<Value> {
    if whatsapp_always_on() {
        return Json(json!({
            "status": "skipped",
            "message": "WhatsApp bot is already running in always-on mode",
        }));
    }

    let _guard = state.sync_lock.lock().await;

    // An explicit re-sync bounds the pre-pair wait: a session that can't re-pair
    // is dead, so don't burn the whole cap on it.
    let (reason, elapsed_s, outcome) =
        run_sync_window(&state, seconds, SYNC_PAIR_TIMEOUT_SECONDS, backfill_days).await;

    let stop_reason = match reason {
        SyncStopReason::OfflineComplete => "offline_complete",
        SyncStopReason::Idle => "idle",
        SyncStopReason::Cap => "cap",
        SyncStopReason::PairTimeout => "pair_timeout",
    };

    match outcome {
        Ok(Ok(Ok(()))) => Json(json!({
            "status": "ok",
            "seconds": seconds,
            "elapsed_s": elapsed_s,
            "stop_reason": stop_reason,
            "message": "WhatsApp sync window completed",
        })),
        Ok(Ok(Err(e))) => Json(json!({
            "status": "error",
            "seconds": seconds,
            "elapsed_s": elapsed_s,
            "stop_reason": stop_reason,
            "message": e.to_string(),
        })),
        Ok(Err(e)) => Json(json!({
            "status": "error",
            "seconds": seconds,
            "elapsed_s": elapsed_s,
            "stop_reason": stop_reason,
            "message": format!("sync task failed: {e}"),
        })),
        Err(_) => Json(json!({
            "status": "timeout",
            "seconds": seconds,
            "elapsed_s": elapsed_s,
            "stop_reason": stop_reason,
            "message": "WhatsApp sync stop timed out after the sync window",
        })),
    }
}

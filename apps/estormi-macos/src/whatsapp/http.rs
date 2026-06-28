//! The loopback Axum API at 127.0.0.1:9877 — router, token middleware wiring,
//! and the request handlers (status, reset, QR PNG, chat list, sync-once, and
//! the iMessage snapshot bridge).
use std::path::Path;

use axum::{
    extract::State,
    http::{header, StatusCode},
    middleware,
    response::IntoResponse,
    routing::{get, post},
    Json, Router,
};
use serde_json::{json, Value};

use super::auth::require_token;
use super::helpers::{
    backfilled_marker_for, bounded_sync_seconds, default_backfill_days, paired_marker_for,
    whatsapp_always_on,
};
use super::sync::{start_background_sync_if_idle, sync_once};
use super::types::{AppState, SyncOnceRequest, DEFAULT_SYNC_SECONDS, WA_API_PORT};

pub(super) async fn run_axum(state: AppState) {
    let router = Router::new()
        .route("/api/whatsapp/status", get(handle_status))
        .route("/api/whatsapp/reset", post(handle_reset))
        .route("/api/whatsapp/qr.png", get(handle_qr_png))
        .route("/api/whatsapp/chats", get(handle_chats))
        .route("/api/whatsapp/sync-once", post(handle_sync_once))
        .route("/api/imessage/snapshot", post(handle_imessage_snapshot))
        .layer(middleware::from_fn_with_state(state.clone(), require_token))
        .with_state(state);

    let addr = std::net::SocketAddr::from(([127, 0, 0, 1], WA_API_PORT));
    // Bounded retry: a just-killed previous instance can still be releasing the
    // loopback port, so a single bind attempt would lose the race. Retry a few
    // times before giving up. If every attempt fails, log loudly — this listener
    // backs BOTH the WhatsApp loopback API *and* the iMessage snapshot bridge
    // (`/api/imessage/snapshot`), so a silent failure here means iMessage and
    // WhatsApp ingestion both go dark with no other signal.
    let mut listener = None;
    for attempt in 1..=5u8 {
        match tokio::net::TcpListener::bind(addr).await {
            Ok(l) => {
                listener = Some(l);
                break;
            }
            Err(e) => {
                eprintln!("estormi-wa: cannot bind :{WA_API_PORT} (attempt {attempt}/5): {e}");
                tokio::time::sleep(std::time::Duration::from_millis(500)).await;
            }
        }
    }
    let Some(listener) = listener else {
        eprintln!(
            "estormi-wa: FATAL — gave up binding :{WA_API_PORT} after 5 attempts; \
             the WhatsApp loopback API and the iMessage snapshot bridge are both \
             unavailable, so WhatsApp and iMessage ingestion will not run this launch"
        );
        return;
    };
    eprintln!("estormi-wa: Axum listening on {addr}");
    if let Err(e) = axum::serve(listener, router).await {
        eprintln!("estormi-wa: Axum error: {e}");
    }
}

/// Refresh the iMessage snapshot, then report whether the sidecar can ingest.
///
/// The main binary holds Full Disk Access; the bundled Python sidecar does not.
/// This copies `chat.db` into the data dir under the FDA-covered identity so the
/// sidecar reads a current copy. Called by the ingestion run just before it
/// reads, and by the FDA re-check. The blocking file copy runs off the async
/// runtime. `status` mirrors the permission vocabulary the SPA already renders.
///
/// Debounced: back-to-back POSTs within a few seconds reuse the existing
/// (still-current) snapshot instead of re-copying the multi-MB DB each time —
/// the response then carries `"throttled": true`.
async fn handle_imessage_snapshot() -> Json<Value> {
    let (flag, throttled) = match tokio::task::spawn_blocking(crate::imessage::snapshot_throttled)
        .await
    {
        Ok(result) => result,
        Err(e) => {
            eprintln!("estormi: iMessage snapshot task failed: {e}");
            ("0", false)
        }
    };
    let status = crate::imessage::snapshot_status(flag);
    Json(json!({ "status": status, "flag": flag, "throttled": throttled }))
}

async fn handle_status(State(s): State<AppState>) -> Json<Value> {
    let status = s.status.read().await.clone();
    let connected = status == "PAIRED";
    // `connected` only reflects "bot is live right now". The bridge goes IDLE
    // between bounded nightly syncs, so we also surface a sticky `paired` bit
    // (driven by the `wa.paired` marker written on the Connected event) so the
    // UI keeps showing the source as set up rather than flipping back to
    // "Awaiting scan" between syncs.
    //
    // But NOT when the session is UNPAIRED: WhatsApp drops inactive linked
    // devices (~14 days), and the bot reports "UNPAIRED" when its connect attempt
    // finds the session gone. The `wa.paired` marker is stale at that point —
    // honouring it would keep the UI saying "connected" while ingestion is
    // silently dead, hiding the need to re-scan the QR. Treat UNPAIRED as
    // not-paired so the source flips to "Awaiting scan" and prompts a re-pair.
    let paired =
        connected || (status != "UNPAIRED" && paired_marker_for(&s.bot_paths.wa_db).exists());
    Json(json!({
        "connected": connected,
        "paired": paired,
        "session_state": status,
        "always_on": whatsapp_always_on(),
    }))
}

/// Remove ``path`` if it exists. ``Ok(true)`` means a file was deleted,
/// ``Ok(false)`` means it was already absent — a missing file is not an error,
/// so reset stays idempotent.
async fn remove_if_present(path: &Path) -> std::io::Result<bool> {
    match tokio::fs::remove_file(path).await {
        Ok(()) => Ok(true),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(e) => Err(e),
    }
}

async fn handle_reset(State(s): State<AppState>) -> Json<Value> {
    // Serialize against any in-flight bounded sync so we never delete the
    // session store out from under a live SqliteStore connection.
    let _guard = s.sync_lock.lock().await;

    *s.status.write().await = "UNPAIRED".to_string();
    *s.qr.write().await = None;
    s.groups.write().await.clear();
    // Drop any stashed live client so nothing holds a handle on wa.db while we
    // remove it below.
    *s.client_cell.write().await = None;

    let marker = paired_marker_for(&s.bot_paths.wa_db);
    if let Err(e) = remove_if_present(&marker).await {
        eprintln!("[estormi/wa] handle_reset: remove marker failed: {e}");
    }
    // Clear the backfill marker too: re-pairing is exactly when WhatsApp will
    // re-push HistorySync, so the next window must re-page the full depth.
    let backfilled = backfilled_marker_for(&s.bot_paths.wa_db);
    if let Err(e) = remove_if_present(&backfilled).await {
        eprintln!("[estormi/wa] handle_reset: remove backfill marker failed: {e}");
    }

    // Removing only ``wa.paired`` leaves the whatsmeow session in wa.db intact,
    // so the next launch silently reconnects the *same* device — and WhatsApp
    // never re-pushes HistorySync to an already-known device. That made "reset"
    // unable to recover message history: the backfill only ever lands at a
    // genuine new-device pairing. So drop the session store (and its WAL
    // sidecars) too, forcing the next sync to start fresh from a QR scan.
    // Skip in always-on mode, where a continuously running bot holds the
    // connection and the file can't be safely yanked.
    let mut session_cleared = false;
    if !whatsapp_always_on() {
        let wa_db = &s.bot_paths.wa_db;
        for path in [
            wa_db.clone(),
            wa_db.with_extension("db-wal"),
            wa_db.with_extension("db-shm"),
        ] {
            match remove_if_present(&path).await {
                Ok(removed) => session_cleared |= removed,
                Err(e) => {
                    eprintln!("[estormi/wa] handle_reset: remove {path:?} failed: {e}")
                }
            }
        }
    }

    Json(json!({"reset": true, "session_cleared": session_cleared}))
}

async fn handle_qr_png(State(s): State<AppState>) -> impl IntoResponse {
    let qr_png = s.qr.read().await.clone();

    match qr_png {
        Some(png) => (
            StatusCode::OK,
            [
                (header::CONTENT_TYPE, "image/png"),
                (header::CACHE_CONTROL, "no-cache"),
            ],
            png,
        )
            .into_response(),
        None => {
            // In idle mode, the QR can only be produced while the bot is running.
            // The setup modal polls this endpoint, so use that poll as a lazy trigger
            // for a bounded pairing/sync window and return 204 until the QR arrives.
            // A fresh pairing kicked off here backfills to the default horizon.
            start_background_sync_if_idle(s, DEFAULT_SYNC_SECONDS, default_backfill_days()).await;
            StatusCode::NO_CONTENT.into_response()
        }
    }
}

async fn handle_chats(State(s): State<AppState>) -> Json<Value> {
    let cache = s.groups.read().await;
    let statuses = s.statuses.read().await;
    let chats: Vec<Value> = cache
        .iter()
        .map(|(id, name)| {
            // ``status`` is the contact's ``About`` text, fetched once per sync
            // window for DMs we have no name for. It's a last-resort label only
            // — most users leave it as "Available" / a quote, so the consumer
            // must rank it below every other name source.
            json!({
                "id": id,
                "name": name,
                "status": statuses.get(id).cloned().unwrap_or_default(),
                "is_group": id.ends_with("@g.us"),
            })
        })
        .collect();
    Json(json!(chats))
}

async fn handle_sync_once(
    State(s): State<AppState>,
    Json(req): Json<SyncOnceRequest>,
) -> Json<Value> {
    // The DAG forwards the source's historic-depth window; a manual call without
    // it falls back to the 2y default.
    let backfill_days = req.backfill_days.unwrap_or_else(default_backfill_days);
    sync_once(s, bounded_sync_seconds(req.seconds), backfill_days).await
}

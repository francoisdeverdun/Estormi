//! WhatsApp session manager — Feature 11.
//!
//! Runs a whatsapp-rust Bot as a Tokio task inside the Tauri binary.
//! Exposes a minimal Axum HTTP API at 127.0.0.1:9877 for status, QR PNG,
//! chat list, and manual history sync.
//!
//! Staging files (.txt + .meta.json) are written under the Tauri app data
//! dir (`~/Library/Application Support/app.estormi.local/staging/whatsapp/`
//! on macOS, resolved via `app.path().app_data_dir()`), in the
//! `<id>.txt` + `<id>.meta.json` layout that
//! estormi_ingestion/whatsapp/ingest_conversations.py consumes.
//!
//! The module is split by responsibility:
//! - [`types`] — shared state handles, structs, and tuning constants.
//! - [`auth`] — loopback API token generation and the request middleware.
//! - [`bot`] — the whatsapp-rust `Bot` task and event loop.
//! - [`http`] — the Axum router and HTTP handlers.
//! - [`sync`] — bounded sync-window orchestration.
//! - [`staging`] — message → staging-file writers and QR PNG render.
//! - [`helpers`] — small cross-cutting helpers.
use std::collections::HashMap;
use std::sync::atomic::AtomicBool;
use std::sync::Arc;

use anyhow::Result;
use tokio::sync::{Mutex, RwLock};

mod auth;
mod bot;
mod helpers;
mod http;
mod staging;
mod sync;
mod types;

pub use auth::generate_api_token;

use bot::run_whatsapp_bot;
use helpers::whatsapp_always_on;
use http::run_axum;
use types::{
    ActivityTracker, AppState, Backfill, BackfillState, BotPaths, ClientCell, GroupCache,
    OfflineFlag, QrState, StatusCache, StatusState,
};

// ── Public entry point ────────────────────────────────────────────────────────

pub fn start(
    app: tauri::AppHandle,
    shutdown: tokio::sync::watch::Receiver<bool>,
    wa_token: String,
) -> tauri::Result<()> {
    tauri::async_runtime::spawn(async move {
        if let Err(e) = run(app, shutdown, wa_token).await {
            eprintln!("estormi-wa: fatal error: {e:#}");
        }
    });
    Ok(())
}

// ── Core loop ─────────────────────────────────────────────────────────────────

async fn run(
    app: tauri::AppHandle,
    mut shutdown: tokio::sync::watch::Receiver<bool>,
    wa_token: String,
) -> Result<()> {
    use tauri::Manager;
    let data_dir = app
        .path()
        .app_data_dir()
        .map_err(|e| anyhow::anyhow!("cannot resolve data dir: {e}"))?;

    let staging = data_dir.join("staging/whatsapp");
    let wa_db = data_dir.join("wa.db");

    tokio::fs::create_dir_all(&staging).await?;

    let qr_state: QrState = Arc::new(RwLock::new(None));
    let status_state: StatusState = Arc::new(RwLock::new("IDLE".to_string()));
    let group_cache: GroupCache = Arc::new(RwLock::new(HashMap::new()));
    let status_cache: StatusCache = Arc::new(RwLock::new(HashMap::new()));
    let client_cell: ClientCell = Arc::new(RwLock::new(None));
    let activity: ActivityTracker = Arc::new(RwLock::new(None));
    let offline_complete: OfflineFlag = Arc::new(AtomicBool::new(false));
    let backfill: Backfill = Arc::new(BackfillState::new());
    let bot_paths = BotPaths { staging, wa_db };

    // Launch Axum server even when the WhatsApp bot is not running continuously.
    // The DAG can then trigger a bounded reconnect through /api/whatsapp/sync-once.
    {
        let state = AppState {
            qr: qr_state.clone(),
            status: status_state.clone(),
            groups: group_cache.clone(),
            statuses: status_cache.clone(),
            client_cell: client_cell.clone(),
            bot_paths: bot_paths.clone(),
            sync_lock: Arc::new(Mutex::new(())),
            activity: activity.clone(),
            offline_complete: offline_complete.clone(),
            backfill: backfill.clone(),
            wa_token: Arc::from(wa_token.as_str()),
        };
        tokio::spawn(run_axum(state));
    }

    if whatsapp_always_on() {
        eprintln!("estormi-wa: always-on mode enabled via ESTORMI_WHATSAPP_ALWAYS_ON");
        run_whatsapp_bot(
            bot_paths,
            qr_state,
            status_state,
            group_cache,
            client_cell,
            activity,
            offline_complete,
            backfill,
            shutdown,
        )
        .await
    } else {
        eprintln!(
            "estormi-wa: idle mode; call POST /api/whatsapp/sync-once for bounded nightly sync"
        );
        let _ = shutdown.changed().await;
        Ok(())
    }
}

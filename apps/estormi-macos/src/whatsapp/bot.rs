//! The whatsapp-rust `Bot` task and its event loop: pairing QR, connection
//! lifecycle, offline-sync markers, live messages, and HistorySync backfill —
//! all funnelling into the shared caches and the staging writers.
use std::str::FromStr;
use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::time::Duration;

use anyhow::Result;
use wacore::stanza::groups::GroupNotificationAction;
use whatsapp_rust::bot::Bot;
use whatsapp_rust::client::Client;
use whatsapp_rust::store::SqliteStore;
use whatsapp_rust::types::events::Event;
use whatsapp_rust::{Jid, TokioRuntime};
use whatsapp_rust_tokio_transport::TokioWebSocketTransportFactory;
use whatsapp_rust_ureq_http_client::UreqHttpClient;

use super::helpers::{bump_activity, paired_marker_for, should_request_older};
use super::staging::{
    backfill_single_conv, conv_oldest_anchor, render_qr_png, write_staging_files,
};
use super::types::{
    ActivityTracker, Backfill, BotPaths, ClientCell, GroupCache, OfflineFlag, QrState, StatusState,
    BACKFILL_BATCH_COUNT, MAX_BACKFILL_REQUESTS,
};

// Eight shared-state handles, each a distinct Arc the bot task needs to clone;
// bundling them into a context struct would not reduce the coupling, only hide it.
#[allow(clippy::too_many_arguments)]
pub(super) async fn run_whatsapp_bot(
    bot_paths: BotPaths,
    qr_state: QrState,
    status_state: StatusState,
    group_cache: GroupCache,
    client_cell: ClientCell,
    activity: ActivityTracker,
    offline_complete: OfflineFlag,
    backfill: Backfill,
    mut shutdown: tokio::sync::watch::Receiver<bool>,
) -> Result<()> {
    *status_state.write().await = "CONNECTING".to_string();

    let staging_c = bot_paths.staging.clone();
    let wa_db = bot_paths.wa_db.clone();
    let qr_c = qr_state.clone();
    let status_c = status_state.clone();
    let cache_c = group_cache.clone();
    let client_cell_c = client_cell.clone();
    let activity_c = activity.clone();
    let offline_complete_c = offline_complete.clone();
    let backfill_c = backfill.clone();
    let marker_c = paired_marker_for(&wa_db);

    let mut bot_task = tokio::spawn(async move {
        let backend = Arc::new(SqliteStore::new(&wa_db.to_string_lossy()).await?);

        let staging_t = staging_c.clone();
        let qr_t = qr_c.clone();
        let status_t = status_c.clone();
        let cache_t = cache_c.clone();
        let client_cell_t = client_cell_c.clone();
        let activity_t = activity_c.clone();
        let offline_complete_t = offline_complete_c.clone();
        let backfill_t = backfill_c.clone();
        let marker_t = marker_c.clone();

        let mut bot = Bot::builder()
            .with_backend(backend)
            .with_transport_factory(TokioWebSocketTransportFactory::new())
            .with_http_client(UreqHttpClient::new())
            .with_runtime(TokioRuntime)
            .on_event(
                move |event, _client: std::sync::Arc<whatsapp_rust::client::Client>| {
                    let staging = staging_t.clone();
                    let qr = qr_t.clone();
                    let status = status_t.clone();
                    let cache = cache_t.clone();
                    let client_cell = client_cell_t.clone();
                    let activity = activity_t.clone();
                    let offline_complete = offline_complete_t.clone();
                    let backfill = backfill_t.clone();
                    let marker = marker_t.clone();
                    async move {
                        match event {
                            Event::PairingQrCode { code, .. } => {
                                if let Ok(png) = render_qr_png(&code) {
                                    *qr.write().await = Some(png);
                                }
                                *status.write().await = "UNPAIRED".to_string();
                                // A QR means a *fresh* device link — WhatsApp
                                // re-pushes HistorySync to a new link, so re-arm
                                // the backfill even if the marker is set (e.g. a
                                // bare re-scan that didn't go through Disconnect).
                                // No-op when backfill is off (cutoff 0) or already
                                // armed. The horizon was set in arm_backfill.
                                if backfill.cutoff_ms.load(Ordering::Relaxed) != 0
                                    && !backfill.active.swap(true, Ordering::SeqCst)
                                {
                                    eprintln!("[estormi/wa] backfill re-armed by fresh QR link");
                                }
                            }
                            Event::Connected(_) => {
                                *status.write().await = "PAIRED".to_string();
                                *qr.write().await = None;
                                // tokio::fs avoids parking a Tokio worker thread on the
                                // ~5 ms sync write; on a heavily-loaded runtime the
                                // sync variant can starve other tasks while waiting
                                // on disk.
                                if let Err(e) = tokio::fs::write(&marker, b"").await {
                                    eprintln!(
                                        "[estormi/wa] failed to write paired marker: {e}"
                                    );
                                }
                                bump_activity(&activity).await;
                                // Stash the live Client so ``sync_once`` can drive a
                                // bulk ``get_user_info`` call before tearing the bot
                                // down. Cleared after the run ends in ``sync_once``.
                                *client_cell.write().await = Some(_client.clone());
                                // Bulk-populate the group cache with all group subjects so that
                                // subsequent JoinedGroup/HistorySync events get real names.
                                let cache_c2 = cache.clone();
                                let client_ref = _client.clone();
                                tokio::spawn(async move {
                                    match client_ref.groups().get_participating().await {
                                        Ok(groups) => {
                                            let mut map = cache_c2.write().await;
                                            for (jid, meta) in &groups {
                                                if !meta.subject.is_empty() {
                                                    map.insert(jid.clone(), meta.subject.clone());
                                                }
                                            }
                                            eprintln!(
                                                "[estormi/wa] cached {} group subjects",
                                                groups.len()
                                            );
                                        }
                                        Err(e) => {
                                            eprintln!("[estormi/wa] get_participating failed: {e}")
                                        }
                                    }
                                });
                            }
                            Event::OfflineSyncPreview(preview) => {
                                // Server announces how many messages it queued
                                // while we were offline. Pure observability — the
                                // messages themselves arrive as Event::Message.
                                bump_activity(&activity).await;
                                eprintln!(
                                    "[estormi/wa] offline sync: {} message(s) queued, {} notification(s)",
                                    preview.messages, preview.notifications
                                );
                            }
                            Event::OfflineSyncCompleted(_) => {
                                // The server has finished flushing the offline
                                // queue — we now hold everything sent while idle.
                                // sync_once watches this flag to end the window
                                // (instead of cutting off after a fixed idle gap).
                                bump_activity(&activity).await;
                                offline_complete.store(true, Ordering::SeqCst);
                                eprintln!("[estormi/wa] offline sync complete");
                            }
                            Event::LoggedOut(_) => {
                                *status.write().await = "UNPAIRED".to_string();
                                if let Err(e) = tokio::fs::remove_file(&marker).await {
                                    // ENOENT is the expected case on first logout —
                                    // we never wrote the marker. Anything else means
                                    // a real filesystem error and is worth logging.
                                    if e.kind() != std::io::ErrorKind::NotFound {
                                        eprintln!(
                                            "[estormi/wa] failed to remove paired marker: {e}"
                                        );
                                    }
                                }
                            }
                            Event::Message(msg, info) => {
                                bump_activity(&activity).await;
                                let chat_jid = info.source.chat.to_string();
                                // For an incoming DM, cache the sender's push_name
                                // *before* staging so even the first message from a
                                // new contact carries a real name — otherwise the
                                // raw JID surfaces in the Settings chat list. Skip
                                // groups (push_name there is the sender, not the
                                // chat) and our own messages (push_name would be us).
                                if !info.source.is_from_me
                                    && !info.push_name.is_empty()
                                    && !chat_jid.ends_with("@g.us")
                                {
                                    cache
                                        .write()
                                        .await
                                        .entry(chat_jid.clone())
                                        .or_insert_with(|| info.push_name.clone());
                                }
                                let chat_name = {
                                    let name_map = cache.read().await;
                                    name_map.get(&chat_jid).cloned().unwrap_or_default()
                                };
                                match write_staging_files(&staging, &msg, &info, &chat_name).await {
                                    Ok(true) => eprintln!(
                                        "[estormi/wa] staged real-time message from {}",
                                        info.source.chat
                                    ),
                                    Ok(false) => eprintln!(
                                        "[estormi/wa] skipped non-text real-time message from {}",
                                        info.source.chat
                                    ),
                                    Err(e) => {
                                        eprintln!("[estormi/wa] failed to stage message: {}", e)
                                    }
                                }
                            }
                            // The library dispatches JoinedGroup (LazyConversation) once per
                            // conversation during HistorySync — Event::HistorySync is never fired.
                            // get_with_messages() decodes the raw bytes keeping messages intact;
                            // plain get() strips them to save memory.
                            Event::JoinedGroup(lazy_conv) => {
                                bump_activity(&activity).await;
                                let conv = match lazy_conv.get_with_messages() {
                                    Some(c) if !c.messages.is_empty() => c,
                                    _ => return, // real group join or empty conversation
                                };
                                // Skip archived chats — the user archived them in
                                // WhatsApp precisely to get them out of sight, so
                                // they shouldn't be retrieved into memory (neither
                                // staged for ingestion nor surfaced in the chat
                                // list, which serializes the groups cache below).
                                if conv.archived.unwrap_or(false) {
                                    return;
                                }
                                // Populate cache from the name carried in HistorySync.
                                // Groups carry their subject in `name`; 1:1 chats
                                // leave `name` empty and put the contact's name in
                                // `display_name` — fall back to it so DMs resolve too.
                                let resolved = conv
                                    .name
                                    .as_deref()
                                    .filter(|n| !n.is_empty())
                                    .or_else(|| {
                                        conv.display_name.as_deref().filter(|n| !n.is_empty())
                                    });
                                // Insert with the empty string when nothing resolved so
                                // the cache enumerates *every* chat HistorySync touches
                                // — sync_once's ``get_user_info`` pass needs the JID
                                // list of nameless DMs to query the contact's About text
                                // as a last-resort label. ``or_insert`` keeps any real
                                // name we got earlier (e.g. via Event::Message).
                                let fallback = resolved.unwrap_or("").to_string();
                                cache
                                    .write()
                                    .await
                                    .entry(conv.id.clone())
                                    .or_insert(fallback);
                                let chat_name = cache
                                    .read()
                                    .await
                                    .get(&conv.id)
                                    .cloned()
                                    .unwrap_or_default();
                                eprintln!(
                                "[estormi/wa] JoinedGroup/HistorySync: chat={} name={:?} msgs={}",
                                conv.id, chat_name, conv.messages.len()
                            );
                                // Capture the page's oldest-message anchor before
                                // ``conv`` is moved into the staging task, so the
                                // backfill can ask the phone for still-older
                                // messages from that point.
                                let oldest = conv_oldest_anchor(&conv);
                                let chat_for_backfill = conv.id.clone();
                                let staging2 = staging.clone();
                                tokio::spawn(backfill_single_conv(staging2, conv, chat_name));
                                if let Some(anchor) = oldest {
                                    tokio::spawn(maybe_page_older(
                                        _client.clone(),
                                        backfill.clone(),
                                        chat_for_backfill,
                                        anchor,
                                    ));
                                }
                            }
                            Event::GroupUpdate(update) => {
                                if let GroupNotificationAction::Subject { subject, .. } =
                                    &update.action
                                {
                                    cache
                                        .write()
                                        .await
                                        .insert(update.group_jid.to_string(), subject.clone());
                                }
                            }
                            _ => {}
                        }
                    }
                },
            )
            .build()
            .await?;

        let handle = bot.run().await?;
        handle.await?;
        Ok::<(), anyhow::Error>(())
    });

    let bot_result: Result<()> = tokio::select! {
        result = &mut bot_task => {
            result.map_err(|e| anyhow::anyhow!("bot task panicked: {e}"))
                .and_then(|r| r)
        }
        _ = shutdown.changed() => {
            eprintln!("estormi-wa: shutdown signal received — stopping WhatsApp bot");
            bot_task.abort();
            match tokio::time::timeout(Duration::from_secs(10), &mut bot_task).await {
                Ok(Ok(result)) => result,
                Ok(Err(e)) if e.is_cancelled() => Ok(()),
                Ok(Err(e)) => Err(anyhow::anyhow!("bot task failed while stopping: {e}")),
                Err(_) => Ok(()),
            }
        }
    };

    if let Err(ref e) = bot_result {
        eprintln!("estormi-wa: bot failed: {e:#}");
        *status_state.write().await = "ERROR".to_string();
    } else {
        *status_state.write().await = "IDLE".to_string();
    }

    bot_result
}

/// Page one step further back in a chat's history, if backfill is active and
/// this conversation page hasn't yet reached the horizon.
///
/// Driven reactively from each `JoinedGroup`/HistorySync page: the response to
/// `fetch_message_history` arrives as another `JoinedGroup` for the same chat
/// with older messages, which re-enters this path — so a single request per page
/// walks the whole chat backward until the horizon is hit or the phone returns
/// nothing older (no progress). The decision + anchor record happen under one
/// lock so concurrent pages for the same chat can't double-fire, and a global
/// counter caps total requests as a runaway backstop.
async fn maybe_page_older(
    client: Arc<Client>,
    backfill: Backfill,
    chat_id: String,
    anchor: (String, i64, bool),
) {
    let (oldest_id, oldest_ms, from_me) = anchor;
    let active = backfill.active.load(Ordering::Relaxed);
    let cutoff = backfill.cutoff_ms.load(Ordering::Relaxed);
    {
        let mut requested = backfill.requested.lock().await;
        if !should_request_older(active, cutoff, oldest_ms, requested.get(&chat_id).copied()) {
            return;
        }
        requested.insert(chat_id.clone(), oldest_ms);
    }
    if backfill.requests_made.fetch_add(1, Ordering::Relaxed) >= MAX_BACKFILL_REQUESTS {
        eprintln!("[estormi/wa] backfill: request cap reached; stopping further paging");
        return;
    }
    let jid = match Jid::from_str(&chat_id) {
        Ok(j) => j,
        Err(e) => {
            eprintln!("[estormi/wa] backfill: bad chat jid {chat_id:?}: {e}");
            return;
        }
    };
    match client
        .fetch_message_history(&jid, &oldest_id, from_me, oldest_ms, BACKFILL_BATCH_COUNT)
        .await
    {
        Ok(_) => eprintln!(
            "[estormi/wa] backfill: requested {BACKFILL_BATCH_COUNT} older msg(s) for {chat_id} before {oldest_ms}ms"
        ),
        Err(e) => {
            eprintln!("[estormi/wa] backfill: fetch_message_history failed for {chat_id}: {e}")
        }
    }
}

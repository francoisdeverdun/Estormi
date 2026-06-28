//! Small cross-cutting helpers: the always-on toggle, the paired marker path,
//! sync-window clamping, the activity stamp, and the About-text DM fallback
//! pass. None own a phase of the flow on their own; they support the bot loop,
//! HTTP API, and sync window.
use std::path::{Path, PathBuf};
use std::str::FromStr;

use whatsapp_rust::Jid;

use super::types::{ActivityTracker, ClientCell, GroupCache, StatusCache};
use super::types::{DEFAULT_SYNC_SECONDS, MAX_SYNC_SECONDS, MIN_SYNC_SECONDS};

/// Continuous-connection escape hatch — deliberately OFF by default.
///
/// Keeping the bridge connected captures every message live (including the
/// user's own replies, closing the `pending_reply` staleness gap), but a linked
/// device that stays online is treated by WhatsApp as actively viewing the
/// chats: it marks messages seen and **suppresses the push notifications on the
/// user's real phone**. The companion is a read-only memory feed and must never
/// degrade the user's primary WhatsApp app, so idle mode is the default. The
/// missed-message gap is narrowed instead by draining the offline queue to
/// completion each bounded run (waiting on `Event::OfflineSyncCompleted` rather
/// than a fixed idle gap), not by staying online. See
/// `docs/specs/whatsapp-rust-sidecar.md` → "Why idle mode is the default".
pub(super) fn whatsapp_always_on() -> bool {
    std::env::var("ESTORMI_WHATSAPP_ALWAYS_ON")
        .map(|v| matches!(v.as_str(), "1" | "true" | "TRUE" | "yes" | "YES"))
        .unwrap_or(false)
}

/// Marker file written next to wa.db once the bridge has paired successfully.
///
/// `status` flips back to "IDLE" between bounded nightly syncs, so it can't
/// stand in for "user has set this up". The marker is the sticky bit the UI
/// reads to decide whether to keep showing the pairing QR or hide it.
pub(super) fn paired_marker_for(wa_db: &Path) -> PathBuf {
    wa_db.with_file_name("wa.paired")
}

/// Marker written next to wa.db once a fresh-pair history backfill has run.
///
/// The on-demand backfill pages years of history and must run exactly **once**
/// per pairing — re-paging it on every nightly sync would hammer the phone for
/// nothing. Its presence tells the next sync window to skip the deep backfill;
/// `handle_reset` deletes it alongside the session so a re-pair backfills afresh.
pub(super) fn backfilled_marker_for(wa_db: &Path) -> PathBuf {
    wa_db.with_file_name("wa.backfilled")
}

/// History horizon (days) the QR-pairing path requests when no explicit
/// `backfill_days` was passed. The scheduled DAG run forwards the source's
/// historic-depth setting; a manual QR scan has no such context, so it falls
/// back here. `ESTORMI_WA_BACKFILL_DAYS` overrides; default 2y to match the
/// connector spec's `default_depth`.
pub(super) fn default_backfill_days() -> u64 {
    std::env::var("ESTORMI_WA_BACKFILL_DAYS")
        .ok()
        .and_then(|v| v.trim().parse::<u64>().ok())
        .filter(|d| *d > 0)
        .unwrap_or(730)
}

/// The unix-ms horizon `days` before `now_ms`, saturating at 0 so an absurd
/// `days` can't underflow (a huge window — e.g. the "all" depth — collapses to
/// the epoch, i.e. unbounded). The backfill never requests messages older than
/// this. The span is computed in `u64` so casting `days` can't wrap negative.
pub(super) fn cutoff_ms_for_days(days: u64, now_ms: i64) -> i64 {
    let span = days.saturating_mul(86_400_000);
    let now = now_ms.max(0) as u64;
    now.saturating_sub(span) as i64
}

/// Whether to request a still-older history page for a chat. True only while
/// backfill is active, the batch's oldest message is still newer than the
/// horizon (older ones may exist), and we have not already paged from this
/// point or older — the last clause stops a resent/overlapping batch (which
/// makes no progress) from looping forever.
pub(super) fn should_request_older(
    active: bool,
    cutoff_ms: i64,
    batch_oldest_ms: i64,
    last_requested_ms: Option<i64>,
) -> bool {
    active && batch_oldest_ms > cutoff_ms && last_requested_ms.is_none_or(|r| batch_oldest_ms < r)
}

pub(super) fn bounded_sync_seconds(seconds: Option<u64>) -> u64 {
    seconds
        .unwrap_or(DEFAULT_SYNC_SECONDS)
        .clamp(MIN_SYNC_SECONDS, MAX_SYNC_SECONDS)
}

/// Stamp ``activity`` with ``Instant::now()`` iff a sync window is currently
/// armed. Outside a window the tracker holds ``None`` and we don't allocate,
/// so the event handler stays effectively free for the always-on path.
pub(super) async fn bump_activity(activity: &ActivityTracker) {
    if let Some(slot) = activity.write().await.as_mut() {
        *slot = std::time::Instant::now();
    }
}

/// `true` when a cached chat is a DM whose name is unusable, so the About-text
/// fallback should be queried for it: a phone-number JID (group ``@g.us`` and
/// ``@lid`` JIDs are out — ``get_user_info`` can't resolve them) that is
/// truly nameless, OR carries WhatsApp's privacy mask (`+33∙∙∙∙∙11`) as its
/// push_name. A masked name is non-empty but conveys nothing, so without the
/// mask clause those contacts would never get their About queried and would
/// stay bare phone numbers in the UI.
fn needs_about_fallback(jid: &str, name: &str) -> bool {
    jid.ends_with("@s.whatsapp.net") && (name.is_empty() || name.contains('\u{2219}'))
}

/// Bulk-fetch the contact's ``About`` text for every DM the group cache knows
/// about but couldn't name from push_name / HistorySync / Contacts.
///
/// Caches results on ``statuses``. Group JIDs (``@g.us``) and ``@lid`` JIDs
/// are skipped — ``get_user_info`` requires a phone-number JID. Errors are
/// logged and swallowed: a failed lookup just leaves the chat with its
/// formatted-phone fallback at display time.
pub(super) async fn fetch_dm_statuses(
    client_cell: ClientCell,
    groups: GroupCache,
    statuses: StatusCache,
) {
    let client = match client_cell.read().await.clone() {
        Some(c) => c,
        None => return,
    };

    let candidates: Vec<Jid> = {
        let names = groups.read().await;
        let known = statuses.read().await;
        names
            .iter()
            .filter(|(jid, name)| needs_about_fallback(jid, name) && !known.contains_key(*jid))
            .filter_map(|(jid, _)| Jid::from_str(jid).ok())
            .collect()
    };

    if candidates.is_empty() {
        return;
    }

    eprintln!(
        "[estormi/wa] fetching About-text fallback for {} nameless DM(s)",
        candidates.len()
    );

    match client.contacts().get_user_info(&candidates).await {
        Ok(info_by_jid) => {
            let mut out = statuses.write().await;
            for (jid, info) in info_by_jid {
                if let Some(text) = info.status.filter(|s| !s.is_empty()) {
                    out.insert(jid.to_string(), text);
                }
            }
        }
        Err(e) => {
            eprintln!("[estormi/wa] get_user_info fallback failed: {e}");
        }
    }
}

#[cfg(test)]
mod tests {
    use std::path::Path;

    use super::super::types::{DEFAULT_SYNC_SECONDS, MAX_SYNC_SECONDS, MIN_SYNC_SECONDS};
    use super::{
        backfilled_marker_for, bounded_sync_seconds, cutoff_ms_for_days, needs_about_fallback,
        paired_marker_for, should_request_older,
    };

    #[test]
    fn sync_seconds_default_when_absent() {
        assert_eq!(bounded_sync_seconds(None), DEFAULT_SYNC_SECONDS);
    }

    #[test]
    fn sync_seconds_clamped_to_floor_and_cap() {
        assert_eq!(bounded_sync_seconds(Some(0)), MIN_SYNC_SECONDS);
        assert_eq!(bounded_sync_seconds(Some(u64::MAX)), MAX_SYNC_SECONDS);
    }

    #[test]
    fn sync_seconds_in_range_pass_through() {
        assert_eq!(
            bounded_sync_seconds(Some(MIN_SYNC_SECONDS)),
            MIN_SYNC_SECONDS
        );
        assert_eq!(bounded_sync_seconds(Some(600)), 600);
        assert_eq!(
            bounded_sync_seconds(Some(MAX_SYNC_SECONDS)),
            MAX_SYNC_SECONDS
        );
    }

    #[test]
    fn paired_marker_sits_next_to_wa_db() {
        assert_eq!(
            paired_marker_for(Path::new("/data/whatsapp/wa.db")),
            Path::new("/data/whatsapp/wa.paired")
        );
    }

    #[test]
    fn backfilled_marker_sits_next_to_wa_db() {
        assert_eq!(
            backfilled_marker_for(Path::new("/data/whatsapp/wa.db")),
            Path::new("/data/whatsapp/wa.backfilled")
        );
    }

    #[test]
    fn cutoff_is_days_before_now_and_saturates() {
        let now = 1_700_000_000_000; // ms
        assert_eq!(cutoff_ms_for_days(1, now), now - 86_400_000);
        assert_eq!(cutoff_ms_for_days(730, now), now - 730 * 86_400_000);
        // A 0-day window puts the horizon at "now" → nothing older qualifies.
        assert_eq!(cutoff_ms_for_days(0, now), now);
        // An absurd day count saturates instead of underflowing past i64::MIN.
        assert_eq!(cutoff_ms_for_days(u64::MAX, now), 0);
    }

    #[test]
    fn should_request_older_gates_on_active_horizon_and_progress() {
        let cutoff = 1_000;
        // Inactive → never request, regardless of how much older history exists.
        assert!(!should_request_older(false, cutoff, 9_999, None));
        // First page for a chat (no prior anchor), still above the horizon.
        assert!(should_request_older(true, cutoff, 5_000, None));
        // Subsequent page made progress (older than last requested) → keep going.
        assert!(should_request_older(true, cutoff, 3_000, Some(5_000)));
        // No progress (same or newer than last requested) → stop, don't loop.
        assert!(!should_request_older(true, cutoff, 5_000, Some(5_000)));
        assert!(!should_request_older(true, cutoff, 6_000, Some(5_000)));
        // Reached the horizon → stop (cutoff is exclusive).
        assert!(!should_request_older(true, cutoff, 1_000, Some(2_000)));
        assert!(!should_request_older(true, cutoff, 500, None));
    }

    #[test]
    fn nameless_dm_needs_about_fallback() {
        assert!(needs_about_fallback("33612345678@s.whatsapp.net", ""));
    }

    #[test]
    fn privacy_masked_push_name_needs_about_fallback() {
        assert!(needs_about_fallback(
            "33612345678@s.whatsapp.net",
            "+33\u{2219}\u{2219}\u{2219}\u{2219}\u{2219}11"
        ));
    }

    #[test]
    fn named_dm_does_not_need_fallback() {
        assert!(!needs_about_fallback("33612345678@s.whatsapp.net", "Alice"));
    }

    #[test]
    fn non_phone_jids_never_need_fallback() {
        // get_user_info can only resolve phone-number JIDs.
        assert!(!needs_about_fallback("12036304@g.us", ""));
        assert!(!needs_about_fallback("987654321@lid", ""));
        assert!(!needs_about_fallback("", ""));
        assert!(!needs_about_fallback("not-a-jid", ""));
    }
}

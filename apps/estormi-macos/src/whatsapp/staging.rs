//! Turning WhatsApp messages into the `.txt` + `.meta.json` staging pairs that
//! `estormi_ingestion/whatsapp/ingest_conversations.py` consumes — plus the QR
//! PNG render used by the pairing flow.
use std::path::{Path, PathBuf};

use anyhow::Result;
use chrono::Utc;
use image::codecs::png::PngEncoder;
use image::{GrayImage, Luma};
use qrcode::QrCode;
use serde_json::json;
use sha2::{Digest, Sha256};
use whatsapp_rust::proto_helpers::MessageExt;
use whatsapp_rust::types::message::MessageInfo;
use whatsapp_rust::waproto::whatsapp as wa;

pub(super) fn render_qr_png(code: &str) -> Result<Vec<u8>> {
    let qr = QrCode::new(code.as_bytes())?;
    let img: GrayImage = qr
        .render::<Luma<u8>>()
        .min_dimensions(200, 200)
        .quiet_zone(true)
        .build();

    let mut png_bytes: Vec<u8> = Vec::new();
    let encoder = PngEncoder::new(std::io::Cursor::new(&mut png_bytes));
    img.write_with_encoder(encoder)?;
    Ok(png_bytes)
}

fn sha256_hex(data: &[u8]) -> String {
    let mut h = Sha256::new();
    h.update(data);
    // sha2 0.11's digest output is a hybrid-array `Array`, which (unlike the
    // old `GenericArray`) doesn't implement `LowerHex` — hex-encode the bytes
    // ourselves. `sha256_hex_matches_known_vectors` pins the output format.
    h.finalize().iter().map(|b| format!("{b:02x}")).collect()
}

/// RFC 3339 (UTC) timestamp for a HistorySync message: the message's own Unix
/// seconds when present and within chrono's representable range, else "now".
/// A missing or absurd timestamp must not sink the message to 1970 or abort
/// the backfill, so the fallback keeps it ingestable at the cost of accuracy.
fn rfc3339_from_unix_or_now(unix_secs: Option<u64>) -> String {
    unix_secs
        .and_then(|t| chrono::DateTime::from_timestamp(t as i64, 0).map(|dt| dt.to_rfc3339()))
        .unwrap_or_else(|| Utc::now().to_rfc3339())
}

pub(super) async fn write_staging_files(
    staging: &Path,
    msg: &wa::Message,
    info: &MessageInfo,
    chat_name: &str,
) -> Result<bool> {
    let text = match msg.text_content() {
        Some(t) => t.to_string(),
        None => return Ok(false),
    };
    if text.trim().is_empty() {
        return Ok(false);
    }

    let chat_id = info.source.chat.to_string();
    let sender_jid = info.source.sender.to_string();
    let name = if info.source.is_from_me {
        "Me".to_string()
    } else if !info.push_name.is_empty() {
        info.push_name.clone()
    } else {
        sender_jid
            .split('@')
            .next()
            .unwrap_or("unknown")
            .to_string()
    };

    let msg_id = info.id.clone();
    let safe_id = &sha256_hex(msg_id.as_bytes())[..32];
    // Use the message's own timestamp, not wall-clock now — the backfill path
    // does the same so staged messages stay chronologically correct.
    let ts_iso = info.timestamp.to_rfc3339();

    tokio::fs::create_dir_all(staging).await?;
    tokio::fs::write(staging.join(format!("{safe_id}.txt")), &text).await?;
    tokio::fs::write(
        staging.join(format!("{safe_id}.meta.json")),
        serde_json::to_string(&json!({
            "id":            msg_id,
            "chat_id":       chat_id,
            "chat_name":     chat_name,
            "name":          name,
            "timestamp_iso": ts_iso,
            "is_group":      info.source.is_group,
        }))?,
    )
    .await?;
    Ok(true)
}

/// The oldest message in a HistorySync conversation, as the
/// `(msg_id, unix_ms, from_me)` anchor `Client::fetch_message_history` needs to
/// request still-older messages. `None` when the conversation carries no message
/// with both a non-empty id and a timestamp (nothing we could page back from).
pub(super) fn conv_oldest_anchor(conv: &wa::Conversation) -> Option<(String, i64, bool)> {
    conv.messages
        .iter()
        .filter_map(|hm| {
            let wi = hm.message.as_ref()?;
            let id = wi.key.id.as_deref().filter(|s| !s.is_empty())?;
            let ts = wi.message_timestamp?; // unix seconds
            Some((
                id.to_string(),
                (ts as i64).saturating_mul(1000),
                wi.key.from_me.unwrap_or(false),
            ))
        })
        .min_by_key(|(_, ts_ms, _)| *ts_ms)
}

/// Backfill staging files for a single conversation received via JoinedGroup/HistorySync.
pub(super) async fn backfill_single_conv(
    staging: PathBuf,
    conv: wa::Conversation,
    chat_name: String,
) {
    let chat_id = &conv.id;
    let mut total_written = 0usize;
    let mut total_skipped_no_text = 0usize;

    if let Err(e) = tokio::fs::create_dir_all(&staging).await {
        eprintln!(
            "[estormi/wa] backfill: failed to create staging dir {:?}: {}",
            staging, e
        );
        return;
    }

    for hist_msg in &conv.messages {
        let wi = match &hist_msg.message {
            Some(w) => w,
            None => continue,
        };

        // text_content() covers conversation (plain text) + extendedTextMessage
        let text = match wi.message.as_ref().and_then(|m| m.text_content()) {
            Some(t) if !t.trim().is_empty() => t.to_string(),
            _ => {
                total_skipped_no_text += 1;
                continue;
            }
        };

        let msg_id = wi.key.id.as_deref().unwrap_or("");
        let key = format!("{}:{}", chat_id, msg_id);
        let safe_id = &sha256_hex(key.as_bytes())[..32];
        let ts = rfc3339_from_unix_or_now(wi.message_timestamp);

        let sender_name = if wi.key.from_me.unwrap_or(false) {
            "Me".to_string()
        } else {
            wi.participant
                .as_deref()
                .filter(|s| !s.is_empty())
                .unwrap_or("unknown")
                .to_string()
        };

        if let Err(e) = tokio::fs::write(staging.join(format!("{safe_id}.txt")), &text).await {
            eprintln!("[estormi/wa] backfill: write txt failed: {}", e);
            continue;
        }
        let _ = tokio::fs::write(
            staging.join(format!("{safe_id}.meta.json")),
            serde_json::to_string(&json!({
                "id":            key,
                "chat_id":       chat_id,
                "chat_name":     chat_name,
                "name":          sender_name,
                "timestamp_iso": ts,
                "is_group":      chat_id.ends_with("@g.us"),
            }))
            .unwrap_or_default(),
        )
        .await;
        total_written += 1;
    }

    if total_written > 0 || total_skipped_no_text > 0 {
        eprintln!(
            "[estormi/wa] backfill conv {}: {} written, {} skipped (no text)",
            chat_id, total_written, total_skipped_no_text
        );
    }
}

#[cfg(test)]
mod tests {
    use super::{render_qr_png, rfc3339_from_unix_or_now, sha256_hex};

    #[test]
    fn sha256_hex_matches_known_vectors() {
        // FIPS 180-4 test vectors.
        assert_eq!(
            sha256_hex(b""),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
        assert_eq!(
            sha256_hex(b"abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }

    #[test]
    fn staging_safe_id_is_32_hex_chars() {
        // Staging filenames truncate the digest to its first 32 hex chars;
        // the same key must always map to the same file (dedup relies on it).
        let safe_id = &sha256_hex("123@g.us:ABCDEF".as_bytes())[..32];
        assert_eq!(safe_id.len(), 32);
        assert!(safe_id.chars().all(|c| c.is_ascii_hexdigit()));
        assert_eq!(safe_id, &sha256_hex("123@g.us:ABCDEF".as_bytes())[..32]);
    }

    #[test]
    fn unix_timestamp_renders_as_utc_rfc3339() {
        assert_eq!(
            rfc3339_from_unix_or_now(Some(0)),
            "1970-01-01T00:00:00+00:00"
        );
        assert_eq!(
            rfc3339_from_unix_or_now(Some(1_700_000_000)),
            "2023-11-14T22:13:20+00:00"
        );
    }

    #[test]
    fn missing_or_out_of_range_timestamp_falls_back_to_now() {
        // i64::MAX seconds is beyond chrono's representable range, so
        // from_timestamp yields None and the fallback kicks in.
        for ts in [None, Some(i64::MAX as u64)] {
            let now = rfc3339_from_unix_or_now(ts);
            let parsed = chrono::DateTime::parse_from_rfc3339(&now)
                .expect("fallback must still be valid RFC 3339");
            // "Now", not the epoch.
            assert!(parsed.timestamp() > 1_700_000_000);
        }
    }

    #[test]
    fn qr_render_produces_a_png() {
        let png = render_qr_png("2@AB12cd34EF,estormi-pairing-ref").expect("QR render");
        assert_eq!(&png[..8], b"\x89PNG\r\n\x1a\n");
    }
}

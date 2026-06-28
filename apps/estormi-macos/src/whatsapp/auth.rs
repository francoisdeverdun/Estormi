//! Loopback API authentication: per-launch token generation and the Axum
//! middleware that fails closed unless every request carries the matching
//! `ESTORMI_WA_TOKEN`.
use axum::{
    extract::{Request, State},
    http::StatusCode,
    middleware::Next,
    response::{IntoResponse, Response},
};

use super::types::{AppState, WA_TOKEN_HEADER};

/// Generate the per-launch shared secret for the loopback sidecar API.
///
/// The Tauri host calls this once at startup, exports it as `ESTORMI_WA_TOKEN`,
/// and the in-process Axum API below requires it on every request — so another
/// local process cannot read the pairing QR or drive the WhatsApp session.
///
/// Returns the io error rather than panicking so the surrounding setup can
/// degrade gracefully (e.g. skip starting the WhatsApp sidecar entirely) if
/// `/dev/urandom` is somehow unavailable.
pub fn generate_api_token() -> std::io::Result<String> {
    use std::io::Read;
    let mut buf = [0u8; 32];
    std::fs::File::open("/dev/urandom")?.read_exact(&mut buf)?;
    Ok(buf.iter().map(|b| format!("{b:02x}")).collect())
}

// Constant-time byte-slice comparison. The pairing token is loopback-only,
// but any local process can probe; `==` short-circuits on the first
// mismatching byte and would leak token bytes via timing.
fn ct_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    let mut diff: u8 = 0;
    for (x, y) in a.iter().zip(b.iter()) {
        diff |= x ^ y;
    }
    diff == 0
}

/// Decide whether a request bearing `provided` (the value of the token header,
/// or `""` when the header is missing/non-ASCII) may pass, given the host's
/// `expected` secret. Fail closed: an empty `expected` (env unset) rejects
/// everyone. Pure so it is unit-tested without standing up the Axum stack or
/// mutating the process environment.
fn token_ok(expected: &str, provided: &str) -> bool {
    if expected.is_empty() {
        return false;
    }
    ct_eq(provided.as_bytes(), expected.as_bytes())
}

pub(super) async fn require_token(
    State(state): State<AppState>,
    req: Request,
    next: Next,
) -> Response {
    let expected: &str = &state.wa_token;
    if expected.is_empty() {
        eprintln!("estormi-wa: ESTORMI_WA_TOKEN unset — refusing request");
        return (StatusCode::UNAUTHORIZED, "unauthorized").into_response();
    }
    let provided = req
        .headers()
        .get(WA_TOKEN_HEADER)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    if !token_ok(expected, provided) {
        return (StatusCode::UNAUTHORIZED, "unauthorized").into_response();
    }
    next.run(req).await
}

#[cfg(test)]
mod tests {
    use super::{ct_eq, token_ok};

    #[test]
    fn ct_eq_true_for_equal_slices() {
        assert!(ct_eq(b"", b""));
        assert!(ct_eq(b"deadbeef", b"deadbeef"));
        let secret = [0u8, 1, 2, 255, 128, 64];
        assert!(ct_eq(&secret, &secret));
    }

    #[test]
    fn ct_eq_false_for_differing_slices() {
        assert!(!ct_eq(b"deadbeef", b"deadbeer"));
        // Differs only in the final byte — `==`'s short-circuit is exactly the
        // timing leak ct_eq exists to avoid; it must still report unequal.
        assert!(!ct_eq(b"aaaaaaab", b"aaaaaaaa"));
        assert!(!ct_eq(b"\x00", b"\x01"));
    }

    #[test]
    fn ct_eq_false_for_differing_lengths() {
        assert!(!ct_eq(b"abc", b"abcd"));
        assert!(!ct_eq(b"abcd", b"abc"));
        assert!(!ct_eq(b"", b"a"));
    }

    #[test]
    fn token_ok_accepts_the_matching_token() {
        let secret = "example-unit-test-secret-token0";
        assert!(token_ok(secret, secret));
    }

    #[test]
    fn token_ok_rejects_a_wrong_token() {
        let secret = "example-unit-test-secret-token0";
        // Wrong value, same length.
        assert!(!token_ok(secret, "example-unit-test-secret-token1"));
        // Wrong length.
        assert!(!token_ok(secret, "short"));
    }

    #[test]
    fn token_ok_rejects_a_missing_or_malformed_header() {
        let secret = "example-unit-test-secret-token0";
        // require_token maps a missing or non-ASCII header to "" before the
        // check — the empty provided token must never authenticate.
        assert!(!token_ok(secret, ""));
    }

    #[test]
    fn token_ok_fails_closed_when_secret_unset() {
        // Empty `expected` mirrors ESTORMI_WA_TOKEN being unset: nobody passes,
        // not even a caller that (absurdly) presents an empty token.
        assert!(!token_ok("", ""));
        assert!(!token_ok("", "anything"));
    }
}

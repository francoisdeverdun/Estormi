//! Honour the relocation pointer written by `memory_core.datadir`.
//!
//! The library may have been moved off the default Application Support path (the
//! "storage location" setting). This mirrors `resolve_data_dir()` on the Python
//! side — `ESTORMI_DATA_DIR` env → pointer file → default — for the handful of
//! files the native shell itself writes (the dock-hidden flag, the iMessage
//! `chat.db` snapshot, the FDA flag), so they never desync from the relocated
//! library. The pointer always lives at the fixed default path even after a move.

use std::path::{Path, PathBuf};

/// The default library location under a user's home, relative to it. Mirrors the
/// Python side (`memory_core.datadir`) and the config-home default the doorbell
/// helper installs into. Hoisted here so the three native resolvers
/// (`main.rs`, `imessage.rs`, `doorbell.rs`) share one literal.
const DATA_DIR_SUFFIX: &str = "Library/Application Support/Estormi";

/// Join the default-library suffix onto a user's `home`. Each caller resolves
/// `home` differently (Tauri's `AppHandle` on the setup thread vs. the `HOME`
/// env var on a detached thread without one), so only the suffix is shared.
pub fn default_data_dir(home: PathBuf) -> PathBuf {
    home.join(DATA_DIR_SUFFIX)
}

/// Given the *default* data dir, return the actual library dir: the relocation
/// pointer's target when present and its volume is mounted, else the default.
///
/// Note: the pointer is flipped by the Python sidecar only after it finishes the
/// copy at startup, so during the single session in which a queued move is
/// applied the shell may still resolve the old path; it converges on the next
/// launch. The shell only writes peripheral files, so the one-session lag is
/// cosmetic and self-healing.
pub fn resolve(default: PathBuf) -> PathBuf {
    let pointer = default.join("data_dir.path");
    if let Ok(raw) = std::fs::read_to_string(&pointer) {
        let target = raw.trim();
        if !target.is_empty() {
            let p = PathBuf::from(target);
            if volume_ready(&p) {
                return p;
            }
        }
    }
    default
}

/// True when `path`'s nearest existing ancestor is a directory — i.e. its volume
/// is mounted. Guards against a pointer aimed at an unplugged external disk.
fn volume_ready(path: &Path) -> bool {
    let mut p = path;
    loop {
        if p.exists() {
            return p.is_dir();
        }
        match p.parent() {
            Some(parent) if parent != p => p = parent,
            _ => return false,
        }
    }
}

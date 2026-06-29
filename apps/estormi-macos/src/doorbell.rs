//! CloudKit doorbell helper auto-install.
//!
//! A distributed `Estormi.app` bundles the maintainer's Developer-ID-signed,
//! notarized `EstormiCloud.app` — the CloudKit doorbell helper (see
//! `apps/estormi-cloud/` and `docs/cloudkit-doorbell.md`). The Python sidecar and
//! the briefing engine resolve the helper at the **config home**
//! (`~/Library/Application Support/Estormi/bin`), which never relocates with the
//! data library (see `memory_core.datadir.config_home` and
//! `estormi_ingestion.shared.delivery.cloudkit_doorbell`). This module copies the
//! bundled helper out to the config home on first run, so the doorbell works for
//! download users with no manual `make doorbell` step.
//!
//! The helper ships as `EstormiCloud.app.zip` (an opaque data resource), NOT an
//! unpacked nested `.app`: a loose nested app sealed by the parent `codesign`
//! has version-dependent nested-code semantics, whereas a zip is unambiguously
//! data the parent just hashes. At first run we extract it with `ditto -x -k`,
//! which preserves the helper's own Developer ID signature + stapled
//! notarization ticket. A sibling `EstormiCloud.version` marker carries the
//! bundled `CFBundleVersion` so an upgrade can be detected without unzipping.
//!
//! It runs **synchronously** during setup, *before* the Python sidecar is
//! spawned. The helper is tiny (~300 KB), so — unlike the iMessage `chat.db`
//! snapshot — there is no reason to background it; and finishing before the
//! sidecar starts means the Python startup migration
//! (`migrate_helper_to_config_home`) finds the config-home helper already present
//! and correctly no-ops, with no install race.
//!
//! Everything is best-effort: a missing bundled helper (a dev run, or a build
//! without `make doorbell-dist`), an unreadable home, or an extract failure all
//! leave the doorbell un-installed and the app otherwise unaffected.

use std::path::{Path, PathBuf};

/// The fixed config home — never relocates with the data library. Mirrors
/// `memory_core.datadir.config_home()` and the `make doorbell` install dest:
/// `$ESTORMI_CONFIG_HOME` → `~/Library/Application Support/Estormi`.
fn config_home() -> Option<PathBuf> {
    if let Some(p) = std::env::var_os("ESTORMI_CONFIG_HOME") {
        return Some(PathBuf::from(p));
    }
    std::env::var_os("HOME").map(|h| crate::datadir::default_data_dir(PathBuf::from(h)))
}

/// `CFBundleVersion` of an installed `.app` via its `Info.plist`, or `None` when
/// absent/unreadable.
fn bundle_version(app: &Path) -> Option<String> {
    // `defaults read` wants the plist path WITHOUT the `.plist` extension.
    let info = app.join("Contents/Info");
    let out = std::process::Command::new("/usr/bin/defaults")
        .arg("read")
        .arg(&info)
        .arg("CFBundleVersion")
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    let v = String::from_utf8_lossy(&out.stdout).trim().to_string();
    if v.is_empty() {
        None
    } else {
        Some(v)
    }
}

/// Decide whether to (re)install the bundled helper over what's at the config
/// home. Pure so it is unit-tested: install when absent; when present, reinstall
/// only if the versions are both known and differ (a shipped helper upgrade);
/// otherwise leave the existing install alone (covers `make doorbell` dev
/// installs and unreadable versions).
fn should_install(dest_exists: bool, installed: Option<&str>, bundled: Option<&str>) -> bool {
    match (dest_exists, installed, bundled) {
        (false, _, _) => true,
        (true, Some(cur), Some(new)) => cur != new,
        (true, _, _) => false,
    }
}

/// Copy the bundled helper out to the config home if appropriate. `resource_dir`
/// is the app bundle's `Contents/Resources` (where `make bundle` embeds
/// `EstormiCloud.app.zip`, `EstormiCloud.version`, and the team-pinned
/// `doorbell_config.json`). Best effort; logs and returns on any problem.
pub fn install_from_bundle(resource_dir: &Path) {
    if let Err(e) = try_install(resource_dir) {
        eprintln!("doorbell: bundled-helper install skipped: {e}");
    }
}

fn try_install(resource_dir: &Path) -> Result<(), String> {
    let bundled_zip = resource_dir.join("EstormiCloud.app.zip");
    if !bundled_zip.exists() {
        // Not a distribution build (dev run, or `make doorbell-dist` was never
        // embedded) — nothing to install.
        return Ok(());
    }
    let home = config_home().ok_or("no config home (HOME unset)")?;
    let bin = home.join("bin");
    let dest_app = bin.join("EstormiCloud.app");

    // Bundled version from the sibling marker (avoids unzipping just to compare).
    let bundled_ver = std::fs::read_to_string(resource_dir.join("EstormiCloud.version"))
        .ok()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty());

    if should_install(
        dest_app.exists(),
        bundle_version(&dest_app).as_deref(),
        bundled_ver.as_deref(),
    ) {
        std::fs::create_dir_all(&bin).map_err(|e| format!("mkdir {bin:?}: {e}"))?;
        let _ = std::fs::remove_dir_all(&dest_app);
        // `ditto -x -k` extracts the zip (built with --keepParent, so it contains
        // `EstormiCloud.app/…`) into `bin/`, preserving the helper's signature +
        // stapled notarization ticket.
        let status = std::process::Command::new("/usr/bin/ditto")
            .args(["-x", "-k"])
            .arg(&bundled_zip)
            .arg(&bin)
            .status()
            .map_err(|e| format!("ditto -x: {e}"))?;
        if !status.success() || !dest_app.exists() {
            let _ = std::fs::remove_dir_all(&dest_app);
            return Err("ditto extract failed".into());
        }
        // Verify the extracted signature; a broken copy is dropped so the Python
        // resolver falls back to a legacy / `make doorbell` install rather than
        // trusting a corrupt helper (which `_verify_team` would refuse anyway).
        let ok = std::process::Command::new("/usr/bin/codesign")
            .args(["--verify", "--deep", "--strict"])
            .arg(&dest_app)
            .status()
            .map(|s| s.success())
            .unwrap_or(false);
        if !ok {
            let _ = std::fs::remove_dir_all(&dest_app);
            return Err("codesign verify failed on the extracted helper".into());
        }
    }

    // Team-pinned default config shipped beside the helper. NEVER clobber an
    // existing config-home file — a user (or a `make doorbell` dev setup) may
    // have pinned a different team or toggled `enabled`.
    let bundled_cfg = resource_dir.join("doorbell_config.json");
    let dest_cfg = home.join("doorbell_config.json");
    if bundled_cfg.exists() && !dest_cfg.exists() {
        let _ = std::fs::copy(&bundled_cfg, &dest_cfg);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::should_install;

    #[test]
    fn installs_when_absent() {
        assert!(should_install(false, None, None));
        assert!(should_install(false, None, Some("7")));
    }

    #[test]
    fn reinstalls_only_on_version_change() {
        // Present + versions differ → a shipped upgrade → reinstall.
        assert!(should_install(true, Some("7"), Some("8")));
        // Present + same version → idempotent no-op.
        assert!(!should_install(true, Some("8"), Some("8")));
    }

    #[test]
    fn leaves_existing_install_when_version_unknown() {
        // Can't compare (e.g. a `make doorbell` dev install with no readable
        // CFBundleVersion) → never clobber it.
        assert!(!should_install(true, None, Some("8")));
        assert!(!should_install(true, Some("7"), None));
        assert!(!should_install(true, None, None));
    }
}

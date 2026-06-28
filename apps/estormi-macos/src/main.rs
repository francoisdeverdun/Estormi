mod datadir;
mod doorbell;
mod imessage;
mod tray;
mod whatsapp;

use std::{sync::Arc, time::Duration};

use tauri::{Manager, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_shell::ShellExt;

const HEALTH_INTERVAL_SECS: u64 = 30;
const RESTART_GRACE_SECS: u64 = 30;
const MAX_RESTART_ATTEMPTS: u8 = 3;
// If far more wall-clock time than the health interval elapses between ticks,
// the Mac was asleep (the in-process scheduler can't fire a cron during sleep).
// Nudge the server to catch up any run missed across the gap.
const WAKE_GAP_SECS: u64 = HEALTH_INTERVAL_SECS * 4;
pub(crate) const MAIN_WINDOW_LABEL: &str = "main";

// Port resolution: env override (MCP_SERVER_PORT) → 8000. Single helper so the
// env var is read consistently across setup, the health-check restart path, and
// the shutdown SIGTERM.
//
// NOTE: the WebView CSP in tauri.conf.json is a static build-time string and
// cannot read an env var, so it hard-pins http://127.0.0.1:8000. That CSP
// governs only the tauri:// SPLASH document (packages/web-ui/dist/index.html);
// once the WebView navigates to the FastAPI origin the SPA runs under the
// server-stamped CSP (`_SPA_CSP` in estormi_server/server/static.py,
// `connect-src 'self'`). The splash itself issues no requests — the readiness
// poll runs here in Rust (the health-check task below uses reqwest, then
// `win.eval`s a `location.replace`), not as JS in the splash document. The SPA
// only ever talks to this FastAPI sidecar — the loopback WhatsApp Axum API on
// :9877 is reached server-side by Python, never from the WebView. So overriding
// MCP_SERVER_PORT in production would point the sidecar off the CSP-pinned
// origin; the override is a dev-only escape hatch.
pub(crate) fn server_port() -> u16 {
    std::env::var("MCP_SERVER_PORT")
        .ok()
        .and_then(|s| s.trim().parse().ok())
        .unwrap_or(8000)
}

fn health_url(port: u16) -> String {
    format!("http://127.0.0.1:{port}/health")
}

// Per-user Application Support dir. ESTORMI_DATA_DIR overrides — same env the
// Python sidecar honours (estormi_server/tools.py) — then the relocation pointer
// (the "storage location" setting), else the default Application Support path.
fn data_dir(app: &tauri::AppHandle) -> Option<std::path::PathBuf> {
    if let Some(p) = std::env::var_os("ESTORMI_DATA_DIR") {
        return Some(std::path::PathBuf::from(p));
    }
    app.path()
        .home_dir()
        .ok()
        .map(|home| datadir::resolve(datadir::default_data_dir(home)))
}

fn dock_hidden_flag_path(app: &tauri::AppHandle) -> Option<std::path::PathBuf> {
    data_dir(app).map(|d| d.join("dock-hidden.flag"))
}

// Read the persisted dock-hidden state. Hidden is the *default* — the dock
// icon only appears when the flag file says exactly "0" (the user
// unticked "Hide Dock Icon" from the tray menu). A missing or unreadable
// flag means we behave like a menu-bar app on first launch.
pub(crate) fn dock_hidden_flag(app: &tauri::AppHandle) -> bool {
    dock_hidden_flag_path(app)
        .and_then(|p| std::fs::read_to_string(p).ok())
        .map(|s| s.trim() != "0")
        .unwrap_or(true)
}

pub(crate) fn set_dock_hidden_flag(app: &tauri::AppHandle, hidden: bool) -> std::io::Result<()> {
    let Some(path) = dock_hidden_flag_path(app) else {
        return Err(std::io::Error::new(
            std::io::ErrorKind::NotFound,
            "could not resolve Estormi data dir",
        ));
    };
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::write(path, if hidden { "1" } else { "0" })
}

fn root_url(port: u16) -> String {
    // Estormi's SPA shell lives at /app. The FastAPI shell redirects
    // / → /app/ when the bundle is present, so this URL is the single
    // canonical entry point regardless of build state.
    format!("http://127.0.0.1:{port}/app/")
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            // Create tray icon
            tray::create_tray(app)?;

            // Dock-icon visibility is hidden by default — Estormi behaves
            // as a menu-bar app. The tray's "Hide Dock Icon" toggle can
            // un-hide it by writing "0" to dock-hidden.flag; honour that
            // here so the setting survives across launches.
            let dock_hidden = dock_hidden_flag(&app.handle().clone());
            if dock_hidden {
                app.set_activation_policy(tauri::ActivationPolicy::Accessory);
            }

            // Build the main window but keep it hidden when the dock is
            // hidden — the user opens it via tray → "Open Estormi", or by
            // clicking the dock icon if visible. The bundled URL still
            // loads (splash from packages/web-ui/dist/index.html) so the
            // health-check task below can navigate it to the FastAPI URL
            // as soon as the sidecar is up; first show then paints the
            // server-served app instantly.
            // Window opens compact in the top-right of the primary monitor
            // so it feels like a genie unfurling from the tray "E" icon
            // rather than a full-screen take-over. `set_position` runs
            // after build because the primary monitor isn't available
            // until the window has an associated screen.
            // The one-pager is designed for a quarter-screen footprint so it
            // can dock alongside the user's primary workflow rather than
            // dominate the desktop. ~520×900 fits a typical Mac monitor
            // (1440–2560 wide) at roughly 1/4 of the screen, and the SPA's
            // CardinalSection / ParametersSection are laid out for this column
            // width.
            const WIN_W: f64 = 520.0;
            const WIN_H: f64 = 900.0;
            let win = WebviewWindowBuilder::new(
                app,
                MAIN_WINDOW_LABEL,
                WebviewUrl::App("index.html".into()),
            )
            .title("Estormi")
            .inner_size(WIN_W, WIN_H)
            .min_inner_size(440.0, 640.0)
            .visible(!dock_hidden)
            // The SPA paints its own torn-parchment frame, so the host
            // window is undecorated + transparent + shadowless. Dragging
            // is handled via `data-tauri-drag-region` on the TopBar; the
            // tray menu's "Open Estormi" / Cmd-W (CloseRequested → hide)
            // cover the show/hide affordances normally provided by the
            // missing traffic-light buttons.
            .decorations(false)
            .transparent(true)
            .shadow(false)
            .build()?;
            if let Ok(Some(monitor)) = win.primary_monitor() {
                let scale = win.scale_factor().unwrap_or(1.0).max(1.0);
                let mon_w = (monitor.size().width as f64) / scale;
                let mon_x = (monitor.position().x as f64) / scale;
                // 24px from the right edge, 36px below the menu bar — keeps
                // the window visually anchored under the tray icon area
                // without nudging into the screen corner.
                let target_x = mon_x + mon_w - WIN_W - 24.0;
                let target_y = 36.0_f64;
                let _ = win.set_position(tauri::LogicalPosition::new(target_x, target_y));
            }

            // Locate bundled Python runtime. Returning the error from setup()
            // surfaces it through Tauri's logging rather than silently aborting
            // the app (which is what `expect` did in sandboxed builds).
            let resource_dir = app.path().resource_dir()?;

            // Tauri encodes ../ resources as _up_/ in the bundle.
            // Dev fallback root: the ESTORMI_REPO_ROOT env var must point at the
            // checkout. We refuse to guess a path (no `~/src/Estormi`-style
            // hardcoded fallback) so this binary stays portable across machines.
            let repo_root = std::env::var_os("ESTORMI_REPO_ROOT").map(std::path::PathBuf::from);
            let python_bin = {
                // Tauri encodes ../ resources as _up_/ — "../../python" → "_up_/_up_/python"
                // (src-tauri lives at apps/estormi-macos/, so resources are two levels up).
                let bundled = resource_dir.join("_up_/_up_/python/bin/python3");
                if bundled.exists() {
                    bundled
                } else {
                    let root = repo_root.clone().ok_or_else(|| {
                        "ESTORMI_REPO_ROOT is not set and no bundled Python was found — \
                         in dev, export ESTORMI_REPO_ROOT=/path/to/checkout"
                            .to_string()
                    })?;
                    root.join(".venv/bin/python3")
                }
            };
            // The estormi_server package's PARENT dir (`packages/`) — used as the
            // sidecar's CWD so `uvicorn estormi_server.main:app` can import the
            // package (it lives at <server_root>/estormi_server).
            let server_root = {
                // `../../packages/*` resources are encoded under `_up_/_up_/packages/`.
                let bundled = resource_dir.join("_up_/_up_/packages");
                if bundled.join("estormi_server").exists() {
                    bundled
                } else {
                    repo_root
                        .clone()
                        .ok_or_else(|| {
                            "ESTORMI_REPO_ROOT is not set and no bundled estormi_server was found — \
                             in dev, export ESTORMI_REPO_ROOT=/path/to/checkout"
                                .to_string()
                        })?
                        .join("packages")
                }
            };

            // Snapshot iMessage's chat.db from the main bundle binary, which is
            // the only process covered by the Full Disk Access grant (the bundled
            // Python sidecar is its own TCC responsible process and stays denied
            // even after the user grants the app — see imessage.rs). The copy
            // lands in the per-user data dir for the sidecar to read, and the FDA
            // flag is written to match. Seeding it here means the first ingestion
            // has data even before the loopback /api/imessage/snapshot refresh.
            //
            // Runs on a background thread: chat.db can be hundreds of MB, and this
            // is the UI/setup thread — blocking it would stall the window paint.
            // Fire-and-forget; the sidecar reads the FDA flag the copy writes.
            imessage::snapshot_async();

            // Install the bundled CloudKit doorbell helper to the config home, so
            // a download user gets new-briefing pushes with no setup. SYNCHRONOUS
            // and BEFORE the sidecar spawn below: the helper is tiny, and finishing
            // first means the Python startup migration finds it already in place
            // and no-ops (no install race). Best-effort, no-op on a dev run where
            // no helper is embedded. See doorbell.rs.
            doorbell::install_from_bundle(&resource_dir);

            // Resolve port once: MCP_SERVER_PORT env override → 8000 default.
            let port = server_port();
            let port_str = port.to_string();

            // The WebView CSP in tauri.conf.json hard-pins connect-src
            // http://127.0.0.1:8000 (a static build-time string — see server_port's
            // note). Overriding MCP_SERVER_PORT in a bundled build therefore points
            // the sidecar off the CSP-pinned origin and the SPA's fetches are
            // blocked. The override is a dev-only escape hatch; warn (don't change
            // the port) when a packaged build resolves it to a non-8000 value. Reuse
            // the same bundled-resource probe as the python/server resolution above:
            // a populated `_up_/_up_/python` means this is a packaged .app, not a
            // dev checkout.
            let is_bundled = resource_dir.join("_up_/_up_/python/bin/python3").exists();
            if is_bundled && port != 8000 {
                eprintln!(
                    "estormi: MCP_SERVER_PORT={port} in a bundled build, but the WebView CSP \
                     pins http://127.0.0.1:8000 — SPA fetches to the sidecar will be blocked"
                );
            }

            // Per-launch shared secret for the loopback WhatsApp sidecar API
            // (port 9877). Generated here; injected into the two consumers as
            // narrowly as each one allows (see below).
            //
            // If the entropy read fails the sidecar API fails closed anyway
            // (require_token rejects when the env is unset), so surface the
            // error rather than panicking on a sandbox quirk.
            let wa_token = whatsapp::generate_api_token()
                .map_err(|e| format!("failed to generate WhatsApp sidecar token: {e}"))?;

            // Redirect Python's bytecode cache outside the .app bundle. The
            // signed app runs under the hardened runtime, which seals the bundle;
            // if the sidecar wrote __pycache__/*.pyc back into Resources it would
            // break the code signature on every run (and later block
            // notarization). Pointing PYTHONPYCACHEPREFIX at the data dir keeps
            // the seal intact while still caching bytecode. Inherited by the
            // sidecar and every ingestion subprocess it spawns.
            let pycache_dir = data_dir(app.handle()).map(|dd| dd.join("pycache"));

            // Free the loopback port before spawning our sidecar. A previous
            // app instance's uvicorn can outlive its parent (force-quit, crash,
            // or a fast relaunch) and keep holding the port; the new sidecar
            // then fails to bind, and the health check below is fooled by the
            // orphan answering on the port — so it never restarts it. We end up
            // with a sidecar whose ESTORMI_WA_TOKEN is from the *old* launch,
            // which makes the loopback API (the chat.db snapshot the iMessage
            // ingestion depends on) reject this launch's requests. Killing the
            // squatter guarantees this launch's sidecar owns the port and the
            // shared token. lsof+kill is best-effort: absent listener → no-op.
            if let Ok(out) = std::process::Command::new("/usr/sbin/lsof")
                .args(["-ti", &format!("tcp:{port}")])
                .output()
            {
                for pid in String::from_utf8_lossy(&out.stdout).split_whitespace() {
                    // Only SIGKILL a PID that is actually our uvicorn sidecar —
                    // never a dev server or unrelated process that merely holds
                    // the port (the exit path documents this same caution).
                    let is_sidecar = std::process::Command::new("/bin/ps")
                        .args(["-p", pid, "-o", "command="])
                        .output()
                        .map(|o| String::from_utf8_lossy(&o.stdout).contains("uvicorn estormi_server.main:app"))
                        .unwrap_or(false);
                    if !is_sidecar {
                        continue;
                    }
                    let _ = std::process::Command::new("/bin/kill")
                        .args(["-9", pid])
                        .status();
                }
            }

            // Spawn FastAPI sidecar. Bind to loopback only — the desktop SPA is
            // the sole client, served same-host at /app/, so the API never needs
            // to be reachable from other machines on the LAN. (The native iOS
            // companion is read-only over iCloud Drive and never talks to this
            // server.) `to_string_lossy` avoids a panic on non-UTF-8 paths.
            let mut cmd = app
                .shell()
                .command(python_bin.to_string_lossy().to_string())
                .env("ESTORMI_WA_TOKEN", &wa_token)
                .args([
                    "-m",
                    "uvicorn",
                    "estormi_server.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    port_str.as_str(),
                    "--log-level",
                    "info",
                    "--loop",
                    "uvloop",
                    "--http",
                    "httptools",
                ])
                .current_dir(&server_root);
            if let Some(ref dir) = pycache_dir {
                cmd = cmd.env("PYTHONPYCACHEPREFIX", dir);
            }
            let (_rx, child) = cmd
                .spawn()
                .map_err(|e| format!("failed to spawn FastAPI sidecar: {e}"))?;

            // Store child handle so we can kill it on exit
            app.manage(Arc::new(tokio::sync::Mutex::new(Some(child))));

            // Shutdown channel for the WhatsApp bot — sender stored in state so
            // ExitRequested can signal a graceful disconnect before pkill fires.
            let (wa_tx, wa_rx) = tokio::sync::watch::channel(false);
            app.manage(Arc::new(wa_tx));

            let wa_token_hc = wa_token.clone();

            // Start WhatsApp task (Feature 11)
            whatsapp::start(app.handle().clone(), wa_rx, wa_token)?;

            // Startup navigation: poll until server ready, then navigate webview
            let app_handle_nav = app.handle().clone();
            let health_url_nav = health_url(port);
            let root_url_nav = root_url(port);
            tauri::async_runtime::spawn(async move {
                // Per-request timeout so a single poll can't hang on a sidecar
                // that accepts the connection but stalls on the response. The
                // loop itself is UNBOUNDED on purpose: this is the only thing
                // that moves the WebView off the bundled splash, so giving up
                // would strand the user on the splash forever. A cold start can
                // be delayed well past any fixed bound — e.g. behind a TCC
                // "removable volume" prompt when the data dir is relocated to an
                // external disk — so we keep polling until /health answers (the
                // watchdog below restarts a sidecar that actually died).
                let http_nav = reqwest::Client::builder()
                    .timeout(Duration::from_secs(5))
                    .build()
                    .unwrap_or_else(|_| reqwest::Client::new());
                loop {
                    tokio::time::sleep(Duration::from_millis(300)).await;
                    let ok = http_nav
                        .get(&health_url_nav)
                        .send()
                        .await
                        .map(|r| r.status().is_success())
                        .unwrap_or(false);
                    if ok {
                        if let Some(win) = app_handle_nav.get_webview_window(MAIN_WINDOW_LABEL) {
                            let _ = win.eval(format!("location.replace('{root_url_nav}')"));
                        }
                        break;
                    }
                }
            });

            // Health check loop with auto-restart
            let app_handle = app.handle().clone();
            let python_bin_c = python_bin.clone();
            let server_root_c = server_root.clone();
            let health_url_hc = health_url(port);
            let wake_url_hc = format!("http://127.0.0.1:{port}/api/jobs/wake-catchup");
            let port_str_hc = port_str.clone();
            let pycache_dir_hc = pycache_dir.clone();
            tauri::async_runtime::spawn(async move {
                // Single client with a request timeout: a sidecar that accepts
                // the TCP connection but stalls on the response (the event-loop
                // saturation the uvloop flag mitigates) must count as a failed
                // tick, not hang the watchdog and defeat auto-restart.
                let http = reqwest::Client::builder()
                    .timeout(Duration::from_secs(HEALTH_INTERVAL_SECS))
                    .build()
                    .unwrap_or_else(|_| reqwest::Client::new());
                let mut failures: u8 = 0;
                let mut last_tick = std::time::SystemTime::now();
                loop {
                    tokio::time::sleep(Duration::from_secs(HEALTH_INTERVAL_SECS)).await;
                    // Wake detection: a wall-clock jump far larger than the
                    // sleep we asked for means the machine slept across one or
                    // more scheduled crons. Fire-and-forget a catch-up POST
                    // (the server re-enqueues only genuinely-missed runs, and
                    // enqueue dedupes, so a false positive is harmless).
                    let gap = last_tick.elapsed().map(|d| d.as_secs()).unwrap_or(0);
                    last_tick = std::time::SystemTime::now();
                    if gap >= WAKE_GAP_SECS {
                        let wake_url = wake_url_hc.clone();
                        let wake_client = http.clone();
                        tauri::async_runtime::spawn(async move {
                            let _ = wake_client
                                .post(&wake_url)
                                .header("X-Estormi-Origin", "estormi-shell")
                                .send()
                                .await;
                        });
                    }
                    let ok = http
                        .get(&health_url_hc)
                        .send()
                        .await
                        .map(|r| r.status().is_success())
                        .unwrap_or(false);
                    if ok {
                        failures = 0;
                        continue;
                    }
                    failures += 1;
                    eprintln!(
                        "estormi: FastAPI health check failed (attempt {}/{})",
                        failures, MAX_RESTART_ATTEMPTS
                    );
                    if failures >= MAX_RESTART_ATTEMPTS {
                        eprintln!("estormi: max restart attempts reached — giving up");
                        break;
                    }
                    // Restart sidecar. Kill the tracked child BEFORE spawning
                    // a replacement so the new uvicorn doesn't race the old one
                    // for the loopback port — otherwise the second bind fails
                    // and we end up with the old (unhealthy) process still
                    // tracked.
                    if let Some(state) = app_handle.try_state::<Arc<
                        tokio::sync::Mutex<Option<tauri_plugin_shell::process::CommandChild>>,
                    >>() {
                        let mut guard = state.lock().await;
                        if let Some(old) = guard.take() {
                            let _ = old.kill();
                        }
                    }
                    // Brief grace window for the kernel to release the port
                    // before the next bind attempt.
                    tokio::time::sleep(Duration::from_millis(500)).await;
                    let mut restart_cmd = app_handle
                        .shell()
                        .command(python_bin_c.to_string_lossy().to_string())
                        .env("ESTORMI_WA_TOKEN", &wa_token_hc)
                        .args([
                            "-m",
                            "uvicorn",
                            "estormi_server.main:app",
                            "--host",
                            "127.0.0.1",
                            "--port",
                            port_str_hc.as_str(),
                            "--log-level",
                            "info",
                            "--loop",
                            "uvloop",
                            "--http",
                            "httptools",
                        ])
                        .current_dir(&server_root_c);
                    if let Some(ref dir) = pycache_dir_hc {
                        restart_cmd = restart_cmd.env("PYTHONPYCACHEPREFIX", dir);
                    }
                    if let Ok((_rx2, new_child)) = restart_cmd.spawn()
                    {
                        if let Some(state) = app_handle.try_state::<Arc<
                            tokio::sync::Mutex<Option<tauri_plugin_shell::process::CommandChild>>,
                        >>() {
                            let mut guard = state.lock().await;
                            *guard = Some(new_child);
                        }
                        eprintln!(
                            "estormi: FastAPI restarted — waiting {}s for startup",
                            RESTART_GRACE_SECS
                        );
                        tokio::time::sleep(Duration::from_secs(RESTART_GRACE_SECS)).await;
                    }
                }
            });

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error building Estormi")
        .run(|app, event| {
            match event {
                tauri::RunEvent::WindowEvent {
                    label,
                    event: tauri::WindowEvent::CloseRequested { api, .. },
                    ..
                } => {
                    api.prevent_close();
                    if let Some(win) = app.get_webview_window(&label) {
                        let _ = win.hide();
                    }
                }
                #[cfg(target_os = "macos")]
                tauri::RunEvent::Reopen {
                    has_visible_windows: false,
                    ..
                } => {
                    // Dock-icon click (or Cmd-Tab open) with no visible windows:
                    // re-show the main window we hid on CloseRequested. When the
                    // dock icon is hidden (Accessory mode) this branch never fires,
                    // and users reopen via the tray menu.
                    if let Some(win) = app.get_webview_window(MAIN_WINDOW_LABEL) {
                        let _ = win.show();
                        let _ = win.set_focus();
                    }
                }
                tauri::RunEvent::ExitRequested { .. } => {
                    // Signal the WhatsApp bot to disconnect cleanly. The bot selects
                    // on this channel and drops Bot (sending a WS close frame) before
                    // the process exits.
                    if let Some(wa) = app.try_state::<Arc<tokio::sync::watch::Sender<bool>>>() {
                        let _ = wa.send(true);
                    }
                    // Kill the tracked sidecar child directly — SIGTERM first
                    // (FastAPI cascades to its own sub-processes), then a short
                    // grace window, then force-kill via the tracked handle.
                    if let Some(state) =
                        app.try_state::<Arc<
                            tokio::sync::Mutex<Option<tauri_plugin_shell::process::CommandChild>>,
                        >>()
                    {
                        tauri::async_runtime::block_on(async {
                            let mut guard = state.lock().await;
                            if let Some(child) = guard.take() {
                                let _ = child.kill();
                            }
                        });
                    }
                    // Grace window for the WhatsApp WS close frame and the
                    // sidecar's SIGTERM handler to drain.
                    std::thread::sleep(std::time::Duration::from_secs(2));
                }
                _ => {}
            }
        });
}

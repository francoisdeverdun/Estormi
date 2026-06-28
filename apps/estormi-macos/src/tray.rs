use tauri::{
    image::Image,
    menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    ActivationPolicy, AppHandle, Manager,
};

fn open_estormi(app: &AppHandle) {
    if let Some(window) = app.get_webview_window(crate::MAIN_WINDOW_LABEL) {
        let _ = window.show();
        let _ = window.set_focus();
    }
}

// Left-click on the tray "E" toggles the main app — the whole window is now
// painted in the torn-parchment aesthetic (AppFrame in the SPA), so there is
// no separate floating launcher; the main window IS the ghost.
//
// Discriminator is *focus*, not visibility:
//   - visible + focused           → hide
//   - hidden, OR visible-unfocused → show + focus
//
// The previous `is_visible`-based toggle needed a double click after a hide
// because macOS reports the window as still visible for a beat after
// `hide()`, so a single re-click would `hide()` again (no-op) and the user
// had to click twice to land on the show branch. Focus state stays clean
// across that race because clicking the tray icon immediately drops the
// app's focus.
fn toggle_main(app: &AppHandle) {
    if let Some(window) = app.get_webview_window(crate::MAIN_WINDOW_LABEL) {
        let visible = window.is_visible().unwrap_or(false);
        let focused = window.is_focused().unwrap_or(false);
        if visible && focused {
            let _ = window.hide();
        } else {
            let _ = window.show();
            let _ = window.set_focus();
        }
    }
}

pub fn create_tray(app: &tauri::App) -> tauri::Result<()> {
    let dashboard = MenuItem::with_id(app, "dashboard", "Open Estormi", true, None::<&str>)?;
    let sep1 = PredefinedMenuItem::separator(app)?;
    let dock_hidden = crate::dock_hidden_flag(&app.handle().clone());
    let dock_toggle = CheckMenuItem::with_id(
        app,
        "dock_toggle",
        "Hide Dock Icon",
        true,
        dock_hidden,
        None::<&str>,
    )?;
    let sep2 = PredefinedMenuItem::separator(app)?;
    let quit = MenuItem::with_id(app, "quit", "Quit Estormi", true, None::<&str>)?;

    let menu = Menu::with_items(app, &[&dashboard, &sep1, &dock_toggle, &sep2, &quit])?;

    // Clone for the event closure so we can read the post-click state — muda
    // auto-flips Check items before firing the menu event.
    let dock_toggle_for_event = dock_toggle.clone();

    // The icon is embedded via include_bytes! so decoding cannot realistically
    // fail, but propagating the error keeps setup() consistent with main.rs's
    // "return errors from setup()" pattern rather than panicking.
    let icon = Image::from_bytes(include_bytes!("../icons/tray-icon-template.png"))?;

    TrayIconBuilder::new()
        .icon(icon)
        .icon_as_template(true)
        .menu(&menu)
        // Default behaviour on macOS shows the menu on left click too — we
        // want left click to summon (or hide) the main app window; the menu
        // stays available via right-click / ctrl-click.
        .show_menu_on_left_click(false)
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                toggle_main(tray.app_handle());
            }
        })
        .on_menu_event(move |app, event| match event.id.as_ref() {
            "dashboard" => {
                open_estormi(app);
            }
            "dock_toggle" => {
                // muda flipped the check state before firing this event, so
                // is_checked() already reflects the new desired state.
                let hide = dock_toggle_for_event.is_checked().unwrap_or(false);
                let policy = if hide {
                    ActivationPolicy::Accessory
                } else {
                    ActivationPolicy::Regular
                };
                if let Err(e) = app.set_activation_policy(policy) {
                    eprintln!("estormi-tray: failed to set activation policy: {e}");
                }
                if let Err(e) = crate::set_dock_hidden_flag(app, hide) {
                    eprintln!("estormi-tray: failed to persist dock-hidden flag: {e}");
                }
            }
            "quit" => {
                app.exit(0);
            }
            _ => {}
        })
        .build(app)?;

    Ok(())
}

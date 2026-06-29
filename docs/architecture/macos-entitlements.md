# macOS Entitlements

Entitlements required by the Tauri shell under hardened runtime
(`apps/estormi-macos/Estormi.entitlements`).

| Entitlement | Why |
|---|---|
| `com.apple.security.network.client` | The main binary makes HTTP calls to the loopback sidecar (health check, iMessage snapshot, WhatsApp bridge) via `reqwest`. Hardened runtime blocks outbound TCP without this. |
| `com.apple.security.automation.apple-events` | The Apple connectors (Reminders, Notes, Calendar, Mail) use AppleScript (`osascript`) from the Python sidecar to read user data. The sidecar inherits the parent app's sandbox profile; without this entitlement the system silently denies the Apple Events. |
| `com.apple.security.personal-information.addressbook` | The Contacts integration (`macos_contacts.py`) uses `CNContactStore` via PyObjC to resolve phone numbers to display names for WhatsApp threads. The TCC prompt is keyed to the parent bundle; this entitlement allows the hardened binary to request that access. |

Full Disk Access (for `~/Library/Messages/chat.db`) is a TCC user grant,
not an entitlement — the system prompt is triggered by the main binary
opening the file directly (`src/imessage.rs`).

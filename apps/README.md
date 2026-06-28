# apps

Runtime-specific native shells for Estormi. Three surfaces live here; each has
its own README with the full build/run notes.

| App (dir) | What it is | Build | Bundle id | Audience |
| --- | --- | --- | --- | --- |
| `estormi-macos/` | Tauri desktop shell — native WebView over the SPA, supervises the server | `make bundle` | `app.estormi.local` | end users + contributors |
| `estormi-ios/` | Read-only SwiftUI viewer (iOS 26+) over an iCloud Drive vault folder | `xcodegen generate` | `app.estormi.ios` | end users |
| `estormi-cloud/` | Faceless **EstormiCloud** CloudKit-doorbell helper | `make doorbell` | `app.estormi.doorbell` | contributors |

- **`estormi-macos/`** is the only app that ingests and composes — everything
  else reads what it writes. See `apps/estormi-macos/README.md`.
- **`estormi-ios/`** reads briefings and an engine-history log the Mac writes
  into a user-picked vault folder (`apps/estormi-ios/Sources/Vault/`). The
  read-only viewer needs no CloudKit and no paid Apple Developer account; the
  lone exception is new-briefing push notifications (CloudKit doorbell, APNs fallback), which require a paid
  account. The Xcode project is generated from `apps/estormi-ios/project.yml`.
- **`estormi-cloud/`** writes a `Briefing` record into the user's private
  CloudKit DB so Apple delivers a new-briefing banner without a push key. The
  directory name matches the built product. See `docs/cloudkit-doorbell.md`.

Estormi is **macOS + iOS only**; connectors run on the Mac and the iOS app is a
read-only viewer.

Rules:

- Apps are thin shells. All logic lives in the server (`packages/estormi_server/`) and
  `packages/`.
- Apps never touch storage directly — they go through the server's HTTP API
  (`packages/estormi_server/`), or, for the iOS companion, the iCloud Drive vault folder.
- Connector code lives in `packages/connectors/`, not in individual apps.

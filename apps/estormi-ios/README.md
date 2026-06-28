# Estormi iOS — native SwiftUI companion

Native iOS 26 companion to the Estormi macOS app. Two pages:

1. **Briefings** — news-style reader over the briefings the Mac writes to
   iCloud Drive. Opens on the latest, horizontal date strip steps back.
2. **Metrics** — read-only mirror of vault status, engine-run history, and
   settings.

Pages swap via the stock iOS 26 `TabView`, which the system renders as a
floating Liquid Glass bar (the same treatment Fitness / Health / Photos get)
— iOS 26 only.

This app **only reads** the vault — all ingestion and briefing composition
runs on the Mac. The vault is always the user-picked iCloud Drive folder
(never CloudKit), so the whole viewer works on a free Apple account. The sole
exception is new-briefing push alerts, which need the paid Apple Developer
Program; the rest of the app does not.

| Capability | Free account | Paid program | Channel / entitlement |
| --- | :---: | :---: | --- |
| Read briefings | ✅ | ✅ | iCloud Drive folder (no entitlement) |
| Read metrics / engine history | ✅ | ✅ | iCloud Drive folder (no entitlement) |
| Listen (narration `.m4a`) | ✅ | ✅ | iCloud Drive folder (no entitlement) |
| New-briefing push alerts | ❌ | ✅ | CloudKit doorbell (`CKQuerySubscription` in `iCloud.app.estormi.ios`, `com.apple.developer.icloud-services`) — the Mac rings it first, falling back to direct APNs (`aps-environment`) |

CloudKit here is only a notification doorbell, never a data store.

## Layout

```
apps/estormi-ios/
├── project.yml                 XcodeGen spec — regenerate the .xcodeproj from here
├── Estormi.xcodeproj           generated, gitignored
└── Sources/
    ├── EstormiApp.swift        @main App
    ├── RootView.swift          page switch + floating glass tab bar
    ├── Vault/                  VaultFolder + VaultReader + VaultStore
    ├── Design/                 tokens, typography, ornaments
    ├── Briefings/              Page 1 (date strip + WKWebView body)
    ├── Metrics/                Page 2 (status, runs, settings)
    ├── Notifications/          CloudKit doorbell (primary) + APNs registrar (fallback)
    ├── Resources/Fonts/        drop Cinzel + EB Garamond .ttf files here
    └── Assets.xcassets/        app icon, accent, launch screen
```

## Build

Prereqs:
- Xcode 26 (ships with iOS 26 SDK).
- `xcodegen` (`brew install xcodegen`).

```bash
cd apps/estormi-ios
xcodegen generate
open Estormi.xcodeproj
```

In Xcode: select the `Estormi` scheme, set your real iPhone as the run
destination, hit ⌘R. The app installs and launches.

After picking your iCloud Drive vault folder on first launch, the Mac's
writes show up automatically — refresh by pulling down on the Metrics page or
re-opening the app.

## Fonts

The design system uses Cinzel (display) and EB Garamond (body). All three
faces are already bundled under `Sources/Resources/Fonts/`:

- `Cinzel-Regular.ttf`
- `EBGaramond-Regular.ttf`
- `EBGaramond-Italic.ttf`

(Bold is derived from Cinzel's variable weight axis — no separate file.) The
`UIAppFonts` keys are already in `project.yml`, so no Info.plist edits are
needed.

To swap a face, replace the `.ttf` in place (keep the same filename). To add
one, drop the new `.ttf` into `Sources/Resources/Fonts/`, add its `UIAppFonts`
entry in `project.yml`, and re-run `xcodegen generate`.

## Narration

The Briefings page can read the briefing aloud, but **the phone does no
synthesis** — true to the read-only contract, the Mac first re-voices the
briefing into a "spoken edition" (an LLM rewrite — same facts, built for the ear
rather than the eye), synthesizes it with Voxtral TTS (see
`packages/memory_core/tts_local.py`), and writes a `briefings/<date>.m4a` next to the
briefing JSON in the iCloud vault. The companion just plays that file:

- `BriefingAudioPlayer` (`Sources/Briefings/`) — wraps `AVAudioPlayer` over the
  local `.m4a`, exposing exact position/duration/seek and driving the
  lock-screen Now Playing transport.
- `BriefingAudioBar` — the gilt "listen" control (play, scrubber, speed toggle).
- `VaultReader.prepareBriefingAudio(date:)` pulls the `.m4a` down from iCloud
  and copies it to a temp file the player can read outside the security scope.

The audio bar appears **only when the briefing carries narration** — the
briefing JSON's `audioPath` is set and the `.m4a` resolves. There is no longer
any bundled neural-voice runtime: the old sherpa-onnx / Piper stack and its
`Vendor/` + `TTSModels/` assets have been removed.

## Vault format

The vault file format is shared with the macOS app — see
[`packages/estormi_ingestion/shared/delivery/vault_sync.py`](../../packages/estormi_ingestion/shared/delivery/vault_sync.py)
for the canonical writer and
[`docs/specs/vault-schema.md`](../../docs/specs/vault-schema.md)
for the schema reference.

## Adding new Swift files

The `sources:` block in `project.yml` points at the whole `Sources/`
directory, so new `.swift` files are picked up automatically the next time
you run `xcodegen generate`. The `.xcodeproj` is regenerable — feel free to
delete and recreate it if it ever gets confused.

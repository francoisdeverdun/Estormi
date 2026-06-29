# EstormiCloud — CloudKit doorbell helper

A faceless, signed macOS helper (`LSUIElement`) that delivers the iOS
new-briefing push **without the Mac ever holding an APNs key**. When the Mac
finishes a briefing it runs this helper, which writes one tiny `Briefing` record
into the Mac's *private* CloudKit database; Apple then fans the banner out to the
iPhone through the iOS app's `CKQuerySubscription`. This is "Option B" of the
notification design — chosen so the free-tier viewer needs no APNs secret on the
Mac. See the canonical deep-dive in
[`docs/cloudkit-doorbell.md`](../../docs/cloudkit-doorbell.md).

The helper is the CloudKit-entitlements bearer: a bare executable cannot claim
restricted entitlements (TN3125), so it ships as a signed `.app` installed
*outside* the Estormi.app bundle (into the config home,
`~/Library/Application Support/Estormi/bin`) so the parent bundle's signature
seal is never broken. It installs to the config home — which never moves when
the data library is relocated — rather than into the movable library, so a
"Move Library" never strands the doorbell.

## Build

XcodeGen is the source of truth — `apps/estormi-cloud/project.yml` generates the
`.xcodeproj` (which is gitignored). Two build paths, both in
[`make/bundle.mk`](../../make/bundle.mk):

- **Dev** — `make doorbell DOORBELL_TEAM=ABCDE12345` builds an Apple-Development,
  device-locked, CloudKit-**Development** helper and installs it to the config
  home (`~/Library/Application Support/Estormi/bin`). For the maintainer's own
  dev iPhone testing.
- **Distribution** — `make doorbell-dist` (Developer ID + hardened +
  CloudKit-**Production**) then `make doorbell-notarize` (notarize + staple).
  `make bundle` then embeds the stapled helper (as a zip) into `Estormi.app`, and
  the Rust shell auto-installs it to the config home on first run. This is the
  "signed once, works for all download users" path — see
  [`docs/cloudkit-doorbell.md`](../../docs/cloudkit-doorbell.md).

## Where it connects

- Producer (Mac side): the helper's own [`Sources/main.swift`](Sources/main.swift).
- Caller: the Python delivery step that execs the helper,
  [`../../packages/estormi_ingestion/shared/delivery/cloudkit_doorbell.py`](../../packages/estormi_ingestion/shared/delivery/cloudkit_doorbell.py).
- Consumer (iOS side): the subscription that turns the record into a banner,
  [`../estormi-ios/Sources/Notifications/CloudKitDoorbell.swift`](../estormi-ios/Sources/Notifications/CloudKitDoorbell.swift).

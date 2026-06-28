import SwiftUI

struct SettingsSection: View {
    @EnvironmentObject private var store: VaultStore
    @Environment(\.scenePhase) private var scenePhase
    @State private var confirmClear = false
    @State private var notifyEnabled = RemotePushRegistrar.isEnabled
    // Mirrors RemotePushRegistrar.tokenDeliveryFailed — the alerts toggle can
    // read "on" while the device token never reached the vault, so the Mac can't
    // push. Surface that instead of failing silently.
    @State private var tokenDeliveryFailed = RemotePushRegistrar.tokenDeliveryFailed
    // Set while we push the system's authorization back into `notifyEnabled`,
    // so the toggle's onChange doesn't re-run enable()/disable() in response to
    // our own re-sync (which would loop, or re-prompt).
    @State private var syncingFromSystem = false
    @AppStorage(BriefingAudioPlayer.defaultSpeedKey) private var defaultSpeedIndex = 0

    var body: some View {
        GildedPanel(tone: .neutral) {
            VStack(alignment: .leading, spacing: 14) {
                Text("SETTINGS")
                    .font(EstormiFont.display(11, bold: true))
                    .tracking(3.4)
                    .foregroundStyle(EstormiColor.orSombre)

                // Shown while exploring the bundled sample vault: make the state
                // obvious and give a one-tap way back. Picking a real folder
                // (Re-pick, below) also leaves the sample.
                if store.isSampleMode {
                    VStack(alignment: .leading, spacing: 8) {
                        Text(
                            "You're exploring a sample vault — demo data, no Mac required. Re-pick the folder your Mac writes to in iCloud Drive to see your own briefings."
                        )
                        .font(EstormiTypeScale.micro)
                        .foregroundStyle(EstormiColor.parchemin.opacity(0.7))
                        .fixedSize(horizontal: false, vertical: true)
                        Button {
                            store.exitSampleMode()
                        } label: {
                            Text("Exit sample")
                                .font(EstormiFont.display(12, bold: true))
                                .tracking(1.6)
                        }
                        .buttonStyle(.glass)
                        .tint(EstormiColor.orSombre)
                    }
                }

                VStack(alignment: .leading, spacing: 4) {
                    Toggle(isOn: $notifyEnabled) {
                        Text("New-briefing alerts")
                            .font(EstormiTypeScale.bodySmall)
                            .foregroundStyle(EstormiColor.parcheminOs)
                    }
                    .tint(EstormiColor.orAncien)
                    .onChange(of: notifyEnabled) { _, on in
                        guard !syncingFromSystem else { return }
                        Task { await applyNotifications(on) }
                    }
                    Text("Pushed from your Mac the moment a new briefing is ready.")
                        .font(EstormiTypeScale.micro)
                        .foregroundStyle(EstormiColor.parchemin.opacity(0.5))
                        .fixedSize(horizontal: false, vertical: true)
                    if notifyEnabled && tokenDeliveryFailed {
                        Text(
                            "Couldn't save this device's push token to the vault — the Mac can't send alerts yet. Check iCloud Drive, then re-pick the vault folder."
                        )
                        .font(EstormiTypeScale.micro)
                        .foregroundStyle(EstormiColor.pourpre)
                        .fixedSize(horizontal: false, vertical: true)
                    }
                }

                VStack(alignment: .leading, spacing: 6) {
                    Text("Reading speed".uppercased())
                        .font(EstormiFont.display(10, bold: true))
                        .tracking(2)
                        .foregroundStyle(EstormiColor.orSombre)
                    Picker("Reading speed", selection: $defaultSpeedIndex) {
                        ForEach(BriefingAudioPlayer.speeds.indices, id: \.self) { i in
                            Text(BriefingAudioPlayer.label(forSpeed: BriefingAudioPlayer.speeds[i])).tag(i)
                        }
                    }
                    .pickerStyle(.segmented)
                    .tint(EstormiColor.orAncien)
                    Text("Default speed when reading a briefing aloud.")
                        .font(EstormiTypeScale.micro)
                        .foregroundStyle(EstormiColor.parchemin.opacity(0.5))
                        .fixedSize(horizontal: false, vertical: true)
                }

                row(label: "Vault folder", value: store.folderName ?? "—")
                Button {
                    Task { await store.pickFolder() }
                } label: {
                    Text("Re-pick folder")
                        .font(EstormiFont.display(13, bold: true))
                        .tracking(1.8)
                }
                .buttonStyle(.glass)
                .tint(EstormiColor.orAncien)

                row(label: "Build", value: bundleVersion)
                row(label: "iOS surface", value: "Estormi · read-only")
                row(label: "Sync", value: "iCloud Drive — folder bookmark")

                Button(role: .destructive) {
                    confirmClear = true
                } label: {
                    Text("Forget vault folder")
                        .font(EstormiFont.display(12, bold: true))
                        .tracking(1.6)
                }
                .buttonStyle(.glass)
                .confirmationDialog(
                    "Forget the chosen vault folder?",
                    isPresented: $confirmClear,
                    titleVisibility: .visible
                ) {
                    Button("Forget", role: .destructive) { store.clearFolder() }
                    Button("Cancel", role: .cancel) {}
                }
            }
        }
        // The toggle reflects a local UserDefaults flag that can lie once the
        // user grants/revokes permission in iOS Settings. Reconcile it with the
        // real authorization on first appear and every time we return to the
        // foreground.
        .task { await resyncNotifyToggle() }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active { Task { await resyncNotifyToggle() } }
        }
    }

    private func resyncNotifyToggle() async {
        let authorized = await RemotePushRegistrar.syncEnabledFromSystem()
        if notifyEnabled != authorized {
            syncingFromSystem = true
            notifyEnabled = authorized
            syncingFromSystem = false
        }
        tokenDeliveryFailed = RemotePushRegistrar.tokenDeliveryFailed
    }

    // Enabling requests notification permission and registers this device with
    // APNs (the token is written to the vault for the Mac) and saves the
    // CloudKit doorbell subscription; a denial reverts the toggle. Disabling
    // tears both channels down.
    @MainActor private func applyNotifications(_ on: Bool) async {
        if on {
            let granted = await RemotePushRegistrar.enable()
            if granted {
                await CloudKitDoorbell.bootstrapIfNeeded()
            } else {
                notifyEnabled = false
            }
        } else {
            RemotePushRegistrar.disable()
            await CloudKitDoorbell.teardown()
            tokenDeliveryFailed = false
        }
    }

    private func row(label: String, value: String) -> some View {
        HStack(alignment: .firstTextBaseline) {
            Text(label.uppercased())
                .font(EstormiFont.display(10, bold: true))
                .tracking(2)
                .foregroundStyle(EstormiColor.orSombre)
            Spacer()
            Text(value)
                .font(EstormiTypeScale.bodySmall)
                .foregroundStyle(EstormiColor.parcheminOs)
                .lineLimit(1)
                .truncationMode(.middle)
        }
    }

    private var bundleVersion: String {
        let info = Bundle.main.infoDictionary
        let v = info?["CFBundleShortVersionString"] as? String ?? "?"
        let b = info?["CFBundleVersion"] as? String ?? "?"
        return "\(v) (\(b))"
    }
}

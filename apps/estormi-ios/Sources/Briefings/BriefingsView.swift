import SwiftUI

// Page 1 — news-style briefing reader. Opens on the latest briefing; the
// horizontal date strip lets the user step back through the archive. The
// briefing body is rendered by BriefingHTMLView in a styled WKWebView.

struct BriefingsView: View {
    @EnvironmentObject private var store: VaultStore
    var scrollToTop: Int = 0
    @State private var selectedDate: String?
    @State private var briefing: VaultBriefing?
    @State private var isLoadingBody = false
    @StateObject private var player = BriefingAudioPlayer()

    var body: some View {
        ZStack {
            EstormiColor.charbon.ignoresSafeArea()
            content
        }
        .task(id: selectedDate) {
            await loadSelected()
        }
        .onAppear {
            if selectedDate == nil {
                selectedDate = store.latestBriefingDate
            }
        }
        .onChange(of: store.briefingIndex) { _, _ in
            if selectedDate == nil || !store.briefingIndex.contains(where: { $0.date == selectedDate }) {
                selectedDate = store.latestBriefingDate
            }
        }
        .onChange(of: store.revision) { _, _ in
            // The vault refreshed (foreground, 60s poll, or pull-to-refresh) —
            // re-read the briefing already on screen so an edit made on the Mac
            // appears without the user switching days. Foreground + pull force a
            // fresh iCloud fetch (the poll re-reads passively).
            Task { await reloadOpenBriefing(forceFresh: store.lastRefreshForcedBriefing) }
        }
        .onDisappear {
            // Leaving the Briefings tab (e.g. to Metrics) must halt narration —
            // otherwise it keeps playing, draining battery, with no transport
            // visible to stop it.
            player.stop()
        }
    }

    @ViewBuilder
    private var content: some View {
        if store.folderStatus != .ready {
            VaultEmptyState()
        } else if store.briefingIndex.isEmpty && !store.isRefreshing {
            EmptyBriefingsState()
        } else {
            VStack(spacing: 0) {
                header
                if store.isSampleMode {
                    SampleBadge()
                        .padding(.top, 2)
                        .padding(.bottom, 6)
                }
                BriefingDateStrip(
                    dates: store.briefingIndex.map(\.date),
                    selected: $selectedDate,
                    recenterToken: scrollToTop
                )
                .padding(.top, 6)
                .padding(.bottom, 12)
                Divider()
                    .background(EstormiColor.orSombre.opacity(0.4))
                if briefing != nil && player.isLoaded {
                    BriefingAudioBar(player: player)
                }
                bodyArea
            }
            // Stable id the XCUITest asserts on to confirm the Briefings page
            // rendered real content (vs an empty / onboarding state).
            // `.contain` exposes the VStack as a queryable container element
            // (a bare SwiftUI layout container isn't an accessibility element,
            // so the identifier alone wouldn't surface to XCUITest) while
            // keeping its children individually accessible.
            .accessibilityElement(children: .contain)
            .accessibilityIdentifier("briefings-content")
        }
    }

    // Brand masthead — the illuminated `E` lettrine flows into the rest of the
    // wordmark, with the tagline garlanded by flowers. The per-briefing title
    // lives in the body (its `<h1>`); the calendar strip says which day.
    private var header: some View {
        EstormiMasthead()
            .padding(.top, 8)
    }

    private var bodyArea: some View {
        ZStack {
            if let briefing {
                BriefingHTMLView(
                    html: briefing.htmlBody,
                    scrollToTopToken: scrollToTop,
                    onRefresh: { await store.refresh(forceBriefing: true) }
                )
                .transition(.opacity)
            } else if isLoadingBody {
                VStack(spacing: 8) {
                    ProgressView()
                        .tint(EstormiColor.orClair)
                    if store.isDownloadingFromCloud {
                        Text("Downloading from iCloud…")
                            .font(EstormiTypeScale.micro)
                            .foregroundStyle(EstormiColor.parchemin.opacity(0.5))
                    }
                }
            } else {
                Text("Open a briefing to read it.")
                    .font(EstormiTypeScale.bodyLarge)
                    .foregroundStyle(EstormiColor.parchemin.opacity(0.6))
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        // Run the body through the bottom safe area so the text scrolls behind
        // the floating Liquid Glass tab bar (matching the Metrics ScrollView)
        // instead of stopping above it with a dead charbon gap. The web CSS
        // already reserves a 96px bottom padding so the last line clears the bar.
        .ignoresSafeArea(.container, edges: .bottom)
    }

    private func loadSelected() async {
        // Switching days halts any narration of the previous briefing.
        player.stop()
        guard let date = selectedDate else { briefing = nil; return }
        isLoadingBody = true
        let loaded = await store.briefing(for: date)
        // The `.task(id: selectedDate)` cancels this task when the user switches
        // days mid-load, but the await above can still resolve afterwards. Guard
        // every state mutation on the load still being current, so a slow load
        // for an old day can't clobber the newly-selected briefing.
        guard !Task.isCancelled, selectedDate == date else { return }
        briefing = loaded
        isLoadingBody = false
        // Attach the narration audio only when the Mac synthesized one for this
        // briefing — the bar appears only once a track is loaded.
        if let loaded, loaded.audioPath != nil,
            let url = await store.briefingAudioURL(for: loaded.date) {
            guard !Task.isCancelled, selectedDate == date else { return }
            player.load(url: url, date: loaded.date)
        } else {
            player.unload()
        }
    }

    // Re-read the briefing currently on screen after a vault refresh, swapping
    // the body in only when it actually changed. Deliberately lighter than
    // `loadSelected`: no spinner and — crucially — it never touches the audio
    // player, so a background refresh can't interrupt in-progress narration.
    // A Mac-side edit changes `htmlBody` (not the audio), so re-reading the
    // body is all that's needed to surface it.
    private func reloadOpenBriefing(forceFresh: Bool) async {
        guard let date = selectedDate else { return }
        let fresh = await store.briefing(for: date, forceFresh: forceFresh)
        guard !Task.isCancelled, selectedDate == date, let fresh else { return }
        if fresh.htmlBody != briefing?.htmlBody {
            briefing = fresh
        }
    }
}

private struct EmptyBriefingsState: View {
    @EnvironmentObject private var store: VaultStore

    var body: some View {
        VStack(spacing: 12) {
            Fleuron(size: 48, color: EstormiColor.orSombre)
            Text("No briefings yet")
                .font(EstormiTypeScale.h3)
                .foregroundStyle(EstormiColor.parchemin)
            Text("Run the Mac pipeline to produce your first briefing.")
                .font(EstormiTypeScale.bodyLarge)
                .foregroundStyle(EstormiColor.parchemin.opacity(0.7))
                .multilineTextAlignment(.center)
            SampleButton { store.enterSampleMode() }
                .padding(.top, 4)
        }
        .padding(40)
    }
}

struct VaultEmptyState: View {
    @EnvironmentObject private var store: VaultStore

    var body: some View {
        VStack(spacing: 16) {
            EstormiLogoMark(size: 96)
            Text("Choose your vault")
                .font(EstormiTypeScale.h2)
                .foregroundStyle(EstormiColor.parcheminOs)
            Text(
                "Pick the folder where the Mac writes briefings into iCloud Drive. Usually iCloud Drive/Estormi."
            )
            .font(EstormiTypeScale.bodyLarge)
            .foregroundStyle(EstormiColor.parchemin.opacity(0.75))
            .multilineTextAlignment(.center)
            .padding(.horizontal, 28)
            Button {
                Task { await store.pickFolder() }
            } label: {
                Text("Pick folder")
                    .font(EstormiFont.display(14, bold: true))
                    .tracking(2)
                    .padding(.horizontal, 22)
                    .padding(.vertical, 12)
            }
            .buttonStyle(.glass)
            .tint(EstormiColor.orAncien)
            SampleButton { store.enterSampleMode() }
            if store.folderStatus == .stale {
                Text("The previously chosen folder is no longer accessible — please re-pick.")
                    .font(EstormiTypeScale.bodySmall)
                    .foregroundStyle(EstormiColor.pourpreClair)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, 28)
            }
            if let err = store.lastError {
                Text(err)
                    .font(EstormiTypeScale.bodySmall)
                    .foregroundStyle(EstormiColor.pourpre)
                    .padding(.horizontal, 28)
            }
        }
        .padding(.vertical, 40)
        // Stable id the XCUITest asserts on for the onboarding / reviewer path.
        // `.contain` exposes the container so the identifier is queryable (see
        // the note on briefings-content) without flattening its children.
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("vault-empty-state")
    }
}

// "Explore a sample" affordance on the empty states — loads the bundled sample
// vault (VaultStore.enterSampleMode) so the app is fully usable, and App
// Store-reviewable, with no paired Mac and no chosen iCloud folder.
private struct SampleButton: View {
    var action: () -> Void

    var body: some View {
        Button(action: action) {
            Text("Explore a sample")
                .font(EstormiFont.display(13, bold: true))
                .tracking(1.6)
                .padding(.horizontal, 18)
                .padding(.vertical, 10)
        }
        .buttonStyle(.glass)
        .tint(EstormiColor.orSombre)
        .accessibilityHint("Loads sample demo data so you can try the app without a Mac.")
        // Stable id so the XCUITest can drive the in-app "Explore a sample" path.
        .accessibilityIdentifier("explore-sample-button")
    }
}

// Small gilt pill marking that the visible content is the bundled sample, not
// the user's real vault. The exit / pick-folder controls live in Settings.
struct SampleBadge: View {
    var body: some View {
        Text("SAMPLE · DEMO DATA")
            .font(EstormiFont.display(9, bold: true))
            .tracking(2.5)
            .foregroundStyle(EstormiColor.orClair)
            .padding(.horizontal, 10)
            .padding(.vertical, 4)
            .background(
                Capsule()
                    .fill(EstormiColor.orAncien.opacity(0.14))
                    .overlay(
                        Capsule().stroke(EstormiColor.orSombre.opacity(0.4), lineWidth: 0.5))
            )
            .accessibilityLabel("Viewing sample demo data")
            .accessibilityIdentifier("sample-badge")
    }
}

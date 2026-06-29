import Combine
import SwiftUI

// Top-level surface: two pages behind the stock iOS 26 tab bar. On iOS 26
// the system `TabView` is automatically rendered as a floating Liquid Glass
// bar (the same treatment Fitness / Health / Photos get) — so we let the
// platform own it rather than hand-rolling a glass menu.

enum EstormiPage: Int, CaseIterable, Hashable {
    case briefings = 0
    case metrics = 1

    var label: String {
        switch self {
        case .briefings: return "Briefings"
        case .metrics: return "Metrics"
        }
    }

    var systemImage: String {
        switch self {
        // Manuscript scroll (read the day's codex) + the memory-palace columns
        // (the loci of the art of memory) — thematic over generic glyphs.
        case .briefings: return "scroll.fill"
        case .metrics: return "building.columns.fill"
        }
    }

    fileprivate static func from(launchArg raw: String?) -> EstormiPage {
        switch raw?.lowercased() {
        case "metrics": return .metrics
        default: return .briefings
        }
    }
}

struct RootView: View {
    @EnvironmentObject private var store: VaultStore
    @Environment(\.scenePhase) private var scenePhase
    @State private var page: EstormiPage
    // Bumped each time a tab is tapped, so the tapped page scrolls back to the
    // top (re-tapping the active tab, or switching into a page).
    @State private var briefingsTop = 0
    @State private var metricsTop = 0
    // Foreground poll cadence for the live-refresh-while-open behaviour.
    private let refreshTick = Timer.publish(every: 60, on: .main, in: .common).autoconnect()

    init() {
        // `xcrun simctl launch booted app.estormi.ios -EstormiStartTab metrics`
        // lets us screenshot any page in DEBUG without driving touch input.
        #if DEBUG
        let raw = UserDefaults.standard.string(forKey: "EstormiStartTab")
        _page = State(initialValue: EstormiPage.from(launchArg: raw))
        #else
        _page = State(initialValue: .briefings)
        #endif
    }

    var body: some View {
        #if DEBUG
        // `-EstormiGallery YES` launch arg opens the illuminated-cap gallery so
        // we can screenshot the candidates. DEBUG-only scaffolding.
        if UserDefaults.standard.bool(forKey: "EstormiGallery") {
            return AnyView(BrandPreview().preferredColorScheme(.dark))
        }
        #endif
        return AnyView(tabs)
    }

    // Custom selection binding: SwiftUI calls the setter on every tab tap —
    // including re-tapping the already-active tab — so we bump that page's
    // scroll-to-top token there.
    private var tabSelection: Binding<EstormiPage> {
        Binding(
            get: { page },
            set: { newValue in
                switch newValue {
                case .briefings: briefingsTop &+= 1
                case .metrics: metricsTop &+= 1
                }
                page = newValue
            }
        )
    }

    private var tabs: some View {
        TabView(selection: tabSelection) {
            Tab(
                EstormiPage.briefings.label,
                systemImage: EstormiPage.briefings.systemImage,
                value: .briefings
            ) {
                BriefingsView(scrollToTop: briefingsTop)
            }
            // Stable id for the XCUITest tab-bar navigation (see UITests/).
            .accessibilityIdentifier("tab-briefings")
            Tab(
                EstormiPage.metrics.label,
                systemImage: EstormiPage.metrics.systemImage,
                value: .metrics
            ) {
                MetricsView(scrollToTop: metricsTop)
            }
            .accessibilityIdentifier("tab-metrics")
        }
        .tint(EstormiColor.orClair)
        .background(EstormiColor.charbon.ignoresSafeArea())
        .preferredColorScheme(.dark)
        .task {
            #if DEBUG
            IconExporter.exportIfRequested()
            #endif
            await store.bootstrap()
        }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active {
                // Coming to the foreground is a strong "show me the latest" —
                // force a fresh iCloud fetch of the open briefing so a Mac-side
                // edit made while the app was away appears immediately.
                Task { await store.refresh(forceBriefing: true) }
            }
        }
        // Live-ish refresh while the app is open: poll the vault on a cadence so
        // a briefing/metrics the Mac drops mid-session appears on its own.
        // (NSMetadataQuery would need the iCloud entitlement we intentionally
        // omit, so we poll the security-scoped folder instead.)
        .onReceive(refreshTick) { _ in
            guard scenePhase == .active else { return }
            Task { await store.refresh() }
        }
    }
}

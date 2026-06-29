import SwiftUI

// Page 2 — read-only metrics view. Mirrors information the macOS app
// displays in its status bar, engine ribbon and config panel, fed by the
// JSON files in the iCloud Drive vault. No mutation: everything that
// changes data lives on the Mac.

struct MetricsView: View {
    @EnvironmentObject private var store: VaultStore
    var scrollToTop: Int = 0

    var body: some View {
        ZStack {
            EstormiColor.charbon.ignoresSafeArea()
            if store.folderStatus != .ready {
                VaultEmptyState()
            } else {
                ScrollViewReader { proxy in
                    ScrollView {
                        VStack(spacing: 18) {
                            Color.clear.frame(height: 0).id(Self.topID)
                            header
                            VaultStatusCard()
                            TimeseriesCard(
                                eyebrow: "Memoria",
                                timeseries: store.metrics?.memory,
                                tone: .gold)
                            IngestionCard()
                            SourcesCatalogCard(sources: store.metrics?.sources ?? [])
                            EngineHistorySection(history: store.enginesHistory)
                            SettingsSection()
                            if let err = store.lastError {
                                Text(err)
                                    .font(EstormiTypeScale.bodySmall)
                                    .foregroundStyle(EstormiColor.pourpre)
                                    .padding(.horizontal, 8)
                            }
                        }
                        .padding(.horizontal, 18)
                        .padding(.top, 16)
                        .padding(.bottom, 120)
                        // Stable id the XCUITest asserts on to confirm the
                        // Metrics page rendered its cards (vs the empty state).
                        // `.contain` exposes the VStack as a queryable container
                        // element (a bare SwiftUI layout container isn't itself
                        // an accessibility element) while leaving its children
                        // individually accessible.
                        .accessibilityElement(children: .contain)
                        .accessibilityIdentifier("metrics-content")
                    }
                    .refreshable {
                        await store.refresh()
                    }
                    .onChange(of: scrollToTop) { _, _ in
                        withAnimation(.easeInOut(duration: EstormiMetric.Motion.medium)) {
                            proxy.scrollTo(Self.topID, anchor: .top)
                        }
                    }
                }
            }
        }
    }

    private static let topID = "metrics-top"

    // A quiet sibling of the Briefings masthead, not a second hero: the page
    // name in Cinzel, then one garlanded tagline in the same grammar as the
    // masthead's "Ars Memoriae" — speculum, the mirror, which is just what this
    // page is — over the shared illuminated rule. Smaller and markless on
    // purpose, so the hierarchy itself reads as secondary.
    private var header: some View {
        VStack(spacing: 8) {
            Text("Metrics")
                .font(EstormiFont.display(24, bold: true))
                .tracking(1)
                .foregroundStyle(EstormiColor.parcheminOs)
            HStack(spacing: 10) {
                Fleuron(size: 9, color: EstormiColor.orSombre)
                Text("SPECULUM")
                    .font(EstormiFont.display(10, bold: true))
                    .tracking(5)
                    .foregroundStyle(EstormiColor.orClair)
                Fleuron(size: 9, color: EstormiColor.orSombre)
            }
            IlluminatedRule()
                .padding(.horizontal, 56)
                .padding(.top, 4)
        }
        .frame(maxWidth: .infinity)
        .padding(.top, 8)
    }
}

import XCTest

// First UI / e2e coverage for the native iOS companion. These drive the two
// paths a user — or an App Store reviewer — hits with NO paired Mac and no
// chosen iCloud vault folder:
//
//   1. The empty / onboarding state (no folder selected).
//   2. The bundled "Explore a sample" demo (Briefings + Metrics fully render).
//
// Determinism without a backend: the app reads a TEST-ONLY `-UITestMode` launch
// argument (see `UITestMode` in Sources/Vault/VaultStore.swift) to pin its
// starting state, so the tests never depend on a real vault, iCloud sync, or
// the DEBUG `DebugVault` fallback being present in the simulator. Every wait
// uses `waitForExistence(timeout:)` — never `sleep` — so the suite stays
// non-flaky on a cold-launched simulator (the first launch is slow).
final class EstormiUITests: XCTestCase {
    /// Generous so the first cold launch on a freshly booted simulator (the app
    /// install + process spawn) never trips the assertion before the UI is up.
    private let launchTimeout: TimeInterval = 30
    /// Element appearance after the app is already on screen — short, since the
    /// view is rendered synchronously from in-memory fixtures (no disk I/O).
    private let uiTimeout: TimeInterval = 10

    override func setUp() {
        super.setUp()
        // A failed assertion should stop the test immediately rather than pile
        // up cascading failures against a screen that never appeared.
        continueAfterFailure = false
    }

    private func launch(mode: String) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments += ["-UITestMode", mode]
        app.launch()
        return app
    }

    /// Query an element by accessibility identifier across *any* element type.
    /// SwiftUI surfaces a `Text` as a `staticText` and an
    /// `.accessibilityElement(children:.contain)` container as an `otherElement`,
    /// so matching on `.any` keeps the assertions independent of which concrete
    /// XCUIElement type a given view happens to map to.
    private func element(_ identifier: String, in app: XCUIApplication) -> XCUIElement {
        app.descendants(matching: .any)[identifier]
    }

    /// Resolve a tab-bar item, preferring our accessibility identifier and
    /// falling back to the visible tab label. The iOS 26 `Tab` builder always
    /// exposes its label as the button title; the identifier may attach to the
    /// tab content rather than the bar item depending on the SwiftUI version, so
    /// the label fallback keeps the navigation robust either way.
    private func tabButton(id: String, label: String, in app: XCUIApplication) -> XCUIElement {
        let byID = app.tabBars.buttons[id]
        if byID.exists { return byID }
        let byLabelInBar = app.tabBars.buttons[label]
        if byLabelInBar.exists { return byLabelInBar }
        let byID2 = app.buttons[id]
        return byID2.exists ? byID2 : app.buttons[label]
    }

    // MARK: Empty-state path (reviewer / first-run)

    /// Launch with no folder → the onboarding "Choose your vault" empty state
    /// must appear, offering the folder picker and the sample affordance. This
    /// is exactly what an App Store reviewer sees before doing anything.
    func testEmptyStateAppearsWithNoVault() {
        let app = launch(mode: "empty")

        let emptyState = element("vault-empty-state", in: app)
        XCTAssertTrue(
            emptyState.waitForExistence(timeout: launchTimeout),
            "The onboarding empty state should appear when no vault folder is selected.")

        // The two onboarding affordances live inside the empty state.
        XCTAssertTrue(
            app.buttons["explore-sample-button"].waitForExistence(timeout: uiTimeout),
            "The 'Explore a sample' button should be offered on the empty state.")

        // No real content should be on screen in the empty state.
        XCTAssertFalse(
            element("briefings-content", in: app).exists,
            "Briefing content must not render before a vault or the sample is loaded.")
    }

    /// Tapping "Explore a sample" from the empty state loads the bundled demo
    /// vault in-app — the real release affordance, exercised end to end.
    func testExploreSampleFromEmptyStateLoadsContent() {
        let app = launch(mode: "empty")

        let sampleButton = app.buttons["explore-sample-button"]
        XCTAssertTrue(
            sampleButton.waitForExistence(timeout: launchTimeout),
            "The 'Explore a sample' button should be reachable from the empty state.")
        sampleButton.tap()

        XCTAssertTrue(
            element("briefings-content", in: app).waitForExistence(timeout: uiTimeout),
            "Tapping 'Explore a sample' should render the sample briefing content.")
        XCTAssertTrue(
            element("sample-badge", in: app).waitForExistence(timeout: uiTimeout),
            "The SAMPLE · DEMO DATA badge should mark the bundled sample vault.")
    }

    // MARK: Sample / demo path (Briefings + Metrics)

    /// Launch straight into the sample vault → the Briefings page renders its
    /// content and the sample badge, then navigating the tab bar to Metrics
    /// renders the metrics cards. Covers both pages a reviewer would tour.
    func testSampleModeRendersBriefingsThenMetrics() {
        let app = launch(mode: "sample")

        // Briefings (the default landing tab) renders the sample briefing.
        let briefings = element("briefings-content", in: app)
        XCTAssertTrue(
            briefings.waitForExistence(timeout: launchTimeout),
            "Sample mode should land on a populated Briefings page.")
        XCTAssertTrue(
            element("sample-badge", in: app).waitForExistence(timeout: uiTimeout),
            "The sample badge should be visible while viewing the bundled sample.")

        // Navigate to Metrics via the floating Liquid Glass tab bar.
        let metricsTab = tabButton(id: "tab-metrics", label: "Metrics", in: app)
        XCTAssertTrue(
            metricsTab.waitForExistence(timeout: uiTimeout),
            "The Metrics tab item should be present in the tab bar.")
        metricsTab.tap()

        XCTAssertTrue(
            element("metrics-content", in: app).waitForExistence(timeout: uiTimeout),
            "Tapping the Metrics tab should render the metrics cards.")

        // The VAULT card surfaces the sample folder name — a concrete bit of the
        // sample metrics making it onto the screen, not just an empty scaffold.
        XCTAssertTrue(
            app.staticTexts["Sample vault"].waitForExistence(timeout: uiTimeout),
            "The Metrics page should show the sample vault status card.")

        // Back to Briefings to confirm the tab bar round-trips.
        let briefingsTab = tabButton(id: "tab-briefings", label: "Briefings", in: app)
        XCTAssertTrue(briefingsTab.waitForExistence(timeout: uiTimeout))
        briefingsTab.tap()
        XCTAssertTrue(
            briefings.waitForExistence(timeout: uiTimeout),
            "Tapping the Briefings tab should return to the briefing reader.")
    }
}

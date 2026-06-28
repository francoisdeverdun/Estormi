import Foundation
import SwiftUI

// SwiftUI-facing observable wrapper over VaultReader. Owns the cached
// manifest, briefing index, engine history and summary counters; refreshes
// on demand or on app foreground.

@MainActor
final class VaultStore: ObservableObject {
    @Published var folderStatus: VaultFolderStatus = .noFolder
    @Published var folderName: String?
    @Published var manifest: VaultManifest?
    @Published var briefingIndex: [VaultBriefingIndexEntry] = []
    @Published var enginesHistory: VaultEnginesHistory?
    @Published var metrics: VaultMetrics?
    @Published var lastError: String?
    @Published var isRefreshing = false
    // True while showing the bundled sample vault (see enterSampleMode). Lets a
    // user — or an App Store reviewer — explore a fully populated app with no
    // paired Mac and no chosen iCloud folder. Every disk-touching path
    // (refresh, briefing, audio) short-circuits on this.
    @Published private(set) var isSampleMode = false
    // Bumped after every refresh() so views can re-read content they hold
    // locally (e.g. the open briefing body) when the vault changed underneath
    // them. Without it, an edit to the briefing already on screen never
    // reloads: its load is keyed only on the selected date, which doesn't
    // change when the Mac edits that same day's briefing.
    @Published var isDownloadingFromCloud = false
    @Published private(set) var revision = 0
    // Whether the refresh that produced the current `revision` asked for a
    // forced re-fetch of the open briefing (foreground / pull-to-refresh) vs a
    // passive poll. Read in the view's revision handler. Not @Published — only
    // `revision` drives the reload; this just qualifies how.
    private(set) var lastRefreshForcedBriefing = false

    // Convenience views derived from the latest engine runs. Ingestion's
    // chunksAdded/bySource are this-run deltas (chunks added over the run's
    // window), while briefing's briefingsTotal is a running total — each taken
    // from the newest run of its engine. The Mac appends each run to the end of
    // `runs`, so the newest run is `.last`, not `.first`.
    var latestIngestion: IngestionCounters? {
        enginesHistory?.runs.last(where: { $0.engine == "ingestion" })?
            .ingestionCounters
    }
    var latestBriefingCounters: BriefingCounters? {
        enginesHistory?.runs.last(where: { $0.engine == "briefing" })?
            .briefingCounters
    }

    var latestBriefingDate: String? { briefingIndex.first?.date }

    func bootstrap() async {
        // Test-only hook: the XCUITest target launches the app with
        // `-UITestMode empty|sample` to pin a deterministic starting state, so
        // the UI tests never depend on a real iCloud vault or the DEBUG
        // `DebugVault` fallback being present in the simulator. No-op in normal
        // use (the argument is never passed). See apps/estormi-ios/UITests/.
        switch UITestMode.current {
        case .sample:
            enterSampleMode()
            return
        case .empty:
            // Force the onboarding / "Choose your vault" empty state regardless
            // of any saved bookmark or debug fallback the sim might carry.
            folderStatus = .noFolder
            folderName = nil
            return
        case .none:
            break
        }
        if MockVault.isEnabled { enterSampleMode(); return }
        refreshFolderState()
        guard folderStatus == .ready else { return }
        await refresh()
    }

    // Load the bundled sample vault — a self-contained demo briefing plus
    // metrics and engine history — so the app is fully explorable (and App
    // Store-reviewable) without a paired Mac or a chosen iCloud folder. Reached
    // from the empty states' "Explore a sample" button and, in DEBUG, from the
    // `-EstormiMockVault` launch arg. Nothing is read from or written to disk in
    // this mode; the live-refresh poll and the vault readers all short-circuit
    // on `isSampleMode`.
    func enterSampleMode() {
        isSampleMode = true
        folderStatus = .ready
        folderName = "Sample vault"
        manifest = MockVault.manifest
        briefingIndex = [MockVault.indexEntry]
        metrics = MockVault.metrics
        enginesHistory = MockVault.enginesHistory
        lastError = nil
    }

    // Leave the sample and return to the real vault state — the folder picker,
    // or a previously chosen live vault.
    func exitSampleMode() {
        guard isSampleMode else { return }
        isSampleMode = false
        manifest = nil
        briefingIndex = []
        enginesHistory = nil
        metrics = nil
        refreshFolderState()
    }

    func refreshFolderState() {
        folderStatus = VaultFolder.shared.status()
        folderName = VaultFolder.shared.displayName()
    }

    func pickFolder() async {
        isSampleMode = false
        do {
            try await VaultFolder.shared.pick()
            refreshFolderState()
            await refresh()
        } catch {
            lastError = error.localizedDescription
        }
    }

    func clearFolder() {
        isSampleMode = false
        VaultFolder.shared.clear()
        manifest = nil
        briefingIndex = []
        enginesHistory = nil
        metrics = nil
        refreshFolderState()
    }

    func refresh(forceBriefing: Bool = false) async {
        if isSampleMode { return }
        guard folderStatus == .ready else { return }
        isRefreshing = true
        defer { isRefreshing = false }
        lastError = nil

        // Decode each file in isolation. iCloud Drive propagates files
        // independently and a present-yet-undecodable file (future schema bump,
        // truncated body) must degrade only its own section — a bad metrics.json
        // should not also blank the briefing index. Errors are accumulated and
        // surfaced together rather than short-circuiting the whole refresh.
        async let manifestTask = VaultReader.readManifest()
        async let indexTask = VaultReader.readBriefingIndex(limit: 60)
        async let historyTask = VaultReader.readEnginesHistory()
        async let metricsTask = VaultReader.readMetrics()

        var errors: [String] = []
        do { manifest = try await manifestTask } catch { errors.append(error.localizedDescription) }
        do { briefingIndex = try await indexTask } catch { errors.append(error.localizedDescription) }
        do { enginesHistory = try await historyTask } catch { errors.append(error.localizedDescription) }
        do { metrics = try await metricsTask } catch { errors.append(error.localizedDescription) }

        lastError = errors.isEmpty ? nil : errors.joined(separator: "\n")
        lastRefreshForcedBriefing = forceBriefing
        revision &+= 1
    }

    func briefing(for date: String, forceFresh: Bool = false) async -> VaultBriefing? {
        if isSampleMode { return MockVault.briefing }
        isDownloadingFromCloud = true
        defer { isDownloadingFromCloud = false }
        do {
            return try await VaultReader.readBriefing(date: date, forceFresh: forceFresh)
        } catch {
            lastError = error.localizedDescription
            return nil
        }
    }

    // Resolve a briefing's narration audio to a local file URL the player can
    // play, or nil when this briefing has none. Drives the audio bar's
    // visibility — it appears only when this returns a URL.
    func briefingAudioURL(for date: String) async -> URL? {
        if isSampleMode { return nil }
        do {
            return try await VaultReader.prepareBriefingAudio(date: date)
        } catch {
            lastError = error.localizedDescription
            return nil
        }
    }

    // Full captured log for one engine run, fetched on demand by the log modal.
    func engineLog(id: String) async -> String? {
        do {
            return try await VaultReader.readEngineLog(id: id)
        } catch {
            lastError = error.localizedDescription
            return nil
        }
    }
}

// TEST-ONLY launch hook. The XCUITest target (apps/estormi-ios/UITests/) drives
// the app headless in the simulator with no Mac backend, so it pins a
// deterministic starting state via the `-UITestMode <mode>` launch argument:
//   • `empty`  → the onboarding "Choose your vault" empty state (reviewer path)
//   • `sample` → the bundled sample vault (the "Explore a sample" demo path)
// Read once in VaultStore.bootstrap(). When the argument is absent (every normal
// launch) `current` is `.none` and bootstrap behaves exactly as before.
enum UITestMode: String {
    case empty
    case sample

    static var current: UITestMode? {
        let args = ProcessInfo.processInfo.arguments
        guard let i = args.firstIndex(of: "-UITestMode"), i + 1 < args.count else { return nil }
        return UITestMode(rawValue: args[i + 1])
    }
}

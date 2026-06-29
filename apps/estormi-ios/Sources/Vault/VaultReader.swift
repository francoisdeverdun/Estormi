import Foundation

// Read the iCloud Drive vault files written by the Mac. The Mac uses atomic
// temp-file + rename; we use NSFileCoordinator on every read so we never see a
// half-written file. Evicted iCloud files are pulled down on demand.

enum VaultReader {
    private static let downloadTimeout: TimeInterval = 30
    private static let decoder = JSONDecoder()

    // Read one top-level vault file as raw UTF-8 text. nil when absent.
    //
    // `forceFresh` pulls the iCloud server's current version even when the
    // device still believes its cached copy is `.current`. iCloud Drive does
    // not reliably push *modifications* to an existing file to other devices
    // (it does for new files) — so after the Mac edits a briefing, the iPhone
    // can keep reporting its stale local copy as current and never re-download.
    // Evicting the local copy forces the next download to round-trip to the
    // server, which holds the edit. Best-effort: an offline evict+download just
    // fails and the caller keeps showing what it had.
    static func readFile(_ name: String, forceFresh: Bool = false) async throws -> String? {
        try await withVaultFolder { folder in
            let url = folder.appendingPathComponent(name)
            if forceFresh {
                try? FileManager.default.evictUbiquitousItem(at: url)
            }
            let result = await ensureDownloaded(url)
            guard FileManager.default.fileExists(atPath: url.path) else {
                if result == .timedOut {
                    throw VaultError(message: "iCloud download timed out for \(name).")
                }
                return nil
            }
            return try coordinatedRead(url)
        }
    }

    static func readManifest() async throws -> VaultManifest? {
        try await readJSON("manifest.json", as: VaultManifest.self)
    }

    static func readBriefing(date: String, forceFresh: Bool = false) async throws -> VaultBriefing? {
        guard date.range(of: #"^\d{4}-\d{2}-\d{2}$"#, options: .regularExpression) != nil
        else {
            throw VaultError(message: "Invalid briefing date.")
        }
        return try await readJSON(
            "briefings/\(date).json", as: VaultBriefing.self, forceFresh: forceFresh)
    }

    // Resolve a briefing's narration audio (`briefings/<date>.m4a`) to a
    // playable local file URL, or nil when the briefing has no audio. The .m4a
    // lives in the security-scoped iCloud folder, but AVAudioPlayer needs
    // stable access for the whole playback — so we pull the file down (if
    // evicted) and copy it into the app's temp dir, then play from there,
    // outside the security scope. The date is constrained to the slug the Mac
    // writes so it can't escape the briefings directory.
    static func prepareBriefingAudio(date: String) async throws -> URL? {
        guard date.range(of: #"^\d{4}-\d{2}-\d{2}$"#, options: .regularExpression) != nil
        else {
            throw VaultError(message: "Invalid briefing date.")
        }
        return try await withVaultFolder { folder in
            let src = folder.appendingPathComponent("briefings/\(date).m4a")
            let result = await ensureDownloaded(src)
            guard FileManager.default.fileExists(atPath: src.path) else {
                if result == .timedOut {
                    throw VaultError(message: "iCloud download timed out for narration audio.")
                }
                return nil
            }

            // Reap stale narration copies from previous opens so they don't
            // accumulate unbounded (iOS only purges temp under storage
            // pressure). Only sweep files older than an hour — far longer than
            // any single narration — so a copy backing an in-flight playback is
            // never deleted out from under AVAudioPlayer.
            sweepStaleBriefingAudio(olderThan: 3600)

            // Unique per call: two overlapping loads (e.g. a foreground refresh
            // racing the user opening a briefing) must not removeItem/copyItem
            // the same fixed path and clobber each other mid-copy.
            let dest = FileManager.default.temporaryDirectory
                .appendingPathComponent("briefing-\(date)-\(UUID().uuidString).m4a")
            var coordinationError: NSError?
            var copyError: Error?
            NSFileCoordinator().coordinate(
                readingItemAt: src, options: [.withoutChanges], error: &coordinationError
            ) { readURL in
                do {
                    try FileManager.default.copyItem(at: readURL, to: dest)
                    // copyItem preserves the source's (often days-old) mtime, so
                    // stamp the copy with "now" — otherwise the sweep's age guard
                    // below could reap a copy the instant it's created.
                    try? FileManager.default.setAttributes(
                        [.modificationDate: Date()], ofItemAtPath: dest.path)
                } catch {
                    copyError = error
                }
            }
            if let error = coordinationError { throw error }
            if let error = copyError { throw error }
            return dest
        }
    }

    // Best-effort removal of leftover `briefing-*.m4a` narration copies in the
    // app temp dir older than `olderThan` seconds. Bounds the within-session
    // footprint left by prepareBriefingAudio's unique-per-call copies.
    private static func sweepStaleBriefingAudio(olderThan: TimeInterval) {
        let tmp = FileManager.default.temporaryDirectory
        guard
            let entries = try? FileManager.default.contentsOfDirectory(
                at: tmp, includingPropertiesForKeys: [.contentModificationDateKey])
        else { return }
        let cutoff = Date().addingTimeInterval(-olderThan)
        for url in entries
        where url.lastPathComponent.hasPrefix("briefing-") && url.pathExtension == "m4a" {
            let modified = (try? url.resourceValues(forKeys: [.contentModificationDateKey]))?
                .contentModificationDate
            if let modified, modified < cutoff {
                try? FileManager.default.removeItem(at: url)
            }
        }
    }

    static func readEnginesHistory() async throws -> VaultEnginesHistory? {
        try await readJSON("engines_history.json", as: VaultEnginesHistory.self)
    }

    static func readMetrics() async throws -> VaultMetrics? {
        try await readJSON("metrics.json", as: VaultMetrics.self)
    }

    // One run's captured log (`engine-logs/<id>.log`), fetched on demand when
    // the user opens a run in the log modal. nil when the file is absent (the
    // Mac keeps only the most recent runs' logs). The id is constrained to the
    // slug the Mac writes so it can't escape the engine-logs directory.
    static func readEngineLog(id: String) async throws -> String? {
        guard id.range(of: #"^[A-Za-z0-9._-]+$"#, options: .regularExpression) != nil
        else {
            throw VaultError(message: "Invalid log id.")
        }
        return try await readFile("engine-logs/\(id).log")
    }

    // Briefing list — newest-first dates only. Building the list never opens a
    // briefing file: the date strip and counters read only `.date`, and the
    // detail view calls `readBriefing` to fetch a full body when the user opens
    // one. (Earlier this downloaded + parsed every briefing JSON — up to 60 per
    // refresh — to populate title/excerpt fields no view rendered.)
    static func readBriefingIndex(limit: Int) async throws -> [VaultBriefingIndexEntry] {
        try await withVaultFolder { folder in
            await briefingDates(in: folder)
                .prefix(max(0, limit))
                .map { VaultBriefingIndexEntry(date: $0) }
        }
    }

    // MARK: - Internals

    // Resolve the vault folder, bracket security-scoped access for the duration
    // of `body`, and tear it down on exit. The app's own Documents fallback
    // (DEBUG: VaultFolder.debugFallbackURL builds Documents/DebugVault) isn't
    // security-scoped, so we skip the begin/end calls there to avoid churning
    // the access counter. Every reader entry point goes through here so the
    // scoping rule lives in one place.
    private static func withVaultFolder<T>(
        _ body: (URL) async throws -> T
    ) async throws -> T {
        let folder = try await MainActor.run { try VaultFolder.shared.resolveURL() }
        let needsScope = !folder.path.contains("/Documents/DebugVault")
        let scoped = needsScope && folder.startAccessingSecurityScopedResource()
        defer { if scoped { folder.stopAccessingSecurityScopedResource() } }
        return try await body(folder)
    }

    private static func readJSON<T: Decodable>(
        _ name: String, as: T.Type, forceFresh: Bool = false
    ) async throws -> T? {
        guard let text = try await readFile(name, forceFresh: forceFresh),
            let data = text.data(using: .utf8)
        else { return nil }
        return try decoder.decode(T.self, from: data)
    }

    // Briefing dates from the manifest when present, otherwise from a scan
    // of briefings/. Newest-first.
    private static func briefingDates(in folder: URL) async -> [String] {
        let manifest = folder.appendingPathComponent("manifest.json")
        _ = await ensureDownloaded(manifest)
        if FileManager.default.fileExists(atPath: manifest.path),
            let text = try? coordinatedRead(manifest),
            let data = text.data(using: .utf8),
            let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            let listed = object["briefings"] as? [String]
        {
            return listed
        }
        let dir = folder.appendingPathComponent("briefings", isDirectory: true)
        let files =
            (try? FileManager.default.contentsOfDirectory(
                at: dir, includingPropertiesForKeys: nil)) ?? []
        return
            files
            .compactMap { url -> String? in
                // An iCloud-evicted briefing appears as a `.<date>.json.icloud`
                // placeholder; strip that wrapper so the fallback doesn't omit
                // not-yet-downloaded dates.
                var name = url.lastPathComponent
                if name.hasSuffix(".icloud") {
                    name = String(name.dropLast(".icloud".count))
                    if name.hasPrefix(".") { name.removeFirst() }
                }
                guard name.hasSuffix(".json") else { return nil }
                return String(name.dropLast(".json".count))
            }
            .sorted(by: >)
    }

    enum DownloadResult: Equatable {
        case ready
        case notUbiquitous
        case timedOut
    }

    private static func ensureDownloaded(_ url: URL) async -> DownloadResult {
        let keys: Set<URLResourceKey> = [
            .isUbiquitousItemKey, .ubiquitousItemDownloadingStatusKey,
        ]
        guard let values = try? url.resourceValues(forKeys: keys),
            values.isUbiquitousItem == true
        else { return .notUbiquitous }
        if values.ubiquitousItemDownloadingStatus == .current { return .ready }

        try? FileManager.default.startDownloadingUbiquitousItem(at: url)
        var attempts = 0
        let maxAttempts = Int(downloadTimeout / 0.25)
        while attempts < maxAttempts {
            if let status = try? url.resourceValues(forKeys: [
                .ubiquitousItemDownloadingStatusKey
            ])
            .ubiquitousItemDownloadingStatus, status == .current {
                return .ready
            }
            do {
                try await Task.sleep(nanoseconds: 250_000_000)
            } catch {
                return .timedOut
            }
            attempts += 1
        }
        return .timedOut
    }

    // Coordinated read — safe against the Mac's in-progress atomic writes.
    private static func coordinatedRead(_ url: URL) throws -> String {
        var coordinationError: NSError?
        var data: Data?
        var readError: Error?
        NSFileCoordinator().coordinate(
            readingItemAt: url, options: [.withoutChanges], error: &coordinationError
        ) { readURL in
            do { data = try Data(contentsOf: readURL) } catch { readError = error }
        }
        if let error = coordinationError { throw error }
        if let error = readError { throw error }
        guard let data else {
            throw VaultError(message: "Could not read \(url.lastPathComponent).")
        }
        return String(decoding: data, as: UTF8.self)
    }
}

import Foundation
import Testing

@testable import Estormi

// Decoding tests for the Codable model layer that reads the iCloud Drive vault
// the Mac writes (see `estormi_ingestion/shared/delivery/vault_sync.py` +
// `docs/specs/vault-schema.md`). Fixtures below mirror the real writer's
// output byte-for-byte in shape: `briefings/<date>.json` from
// `run_knowledge.py`, `manifest.json` + `engines_history.json` from
// `vault_sync.py`, and the `metrics.json` snapshot. We decode through the same
// configuration `VaultReader` uses — a bare `JSONDecoder()`, no snake_case
// conversion — so a key mismatch here is a real reader bug.

private let decoder = JSONDecoder()

private func decode<T: Decodable>(_ type: T.Type, _ json: String) throws -> T {
    try decoder.decode(T.self, from: Data(json.utf8))
}

// MARK: - Briefing payload (briefings/<date>.json)

@Suite("Briefing payload")
struct BriefingDecodingTests {
    // Exactly the dict `run_knowledge.py::_vault_push_briefing` writes: note
    // `id` is `briefing-<date>`, distinct from `date`.
    static let json = """
        {
          "id": "briefing-2026-06-02",
          "date": "2026-06-02",
          "title": "Briefing — 2026-06-02",
          "htmlBody": "<article><h1>Briefing</h1><p>Three threads converge.</p></article>",
          "sourceCount": 5,
          "videoCount": 2,
          "articleCount": 3,
          "generatedAt": "2026-06-02T07:00:00Z"
        }
        """

    @Test func decodesAllFields() throws {
        let b = try decode(VaultBriefing.self, Self.json)
        #expect(b.id == "briefing-2026-06-02")
        #expect(b.date == "2026-06-02")
        #expect(b.title == "Briefing — 2026-06-02")
        #expect(b.htmlBody == "<article><h1>Briefing</h1><p>Three threads converge.</p></article>")
        #expect(b.sourceCount == 5)
        #expect(b.videoCount == 2)
        #expect(b.articleCount == 3)
        #expect(b.generatedAt == "2026-06-02T07:00:00Z")
    }

    // Only `id`, `date`, and `htmlBody` are non-optional; the detail view reads
    // those lazily. A minimal payload must still decode.
    @Test func decodesWithOptionalFieldsAbsent() throws {
        let json = """
            {"id": "briefing-2026-06-02", "date": "2026-06-02", "htmlBody": "<p>x</p>"}
            """
        let b = try decode(VaultBriefing.self, json)
        #expect(b.date == "2026-06-02")
        #expect(b.title == nil)
        #expect(b.sourceCount == nil)
    }

    // The reader ignores unknown keys — the schema doc requires this so the Mac
    // can add fields without breaking older companions.
    @Test func toleratesUnknownKeys() throws {
        let json = """
            {"id": "x", "date": "2026-06-02", "htmlBody": "<p/>", "futureField": 7}
            """
        let b = try decode(VaultBriefing.self, json)
        #expect(b.date == "2026-06-02")
    }
}

// MARK: - Manifest / briefing index

@Suite("Manifest + briefing index")
struct ManifestDecodingTests {
    // `_rebuild_manifest` in vault_sync.py writes exactly these four keys.
    static let json = """
        {
          "generatedAt": "2026-06-02T18:41:02Z",
          "briefings": ["2026-06-02", "2026-06-01", "2026-05-31"],
          "hasEnginesHistory": true,
          "hasMetrics": true
        }
        """

    @Test func decodesManifest() throws {
        let m = try decode(VaultManifest.self, Self.json)
        #expect(m.generatedAt == "2026-06-02T18:41:02Z")
        #expect(m.briefings == ["2026-06-02", "2026-06-01", "2026-05-31"])
        #expect(m.hasEnginesHistory == true)
        #expect(m.hasMetrics == true)
    }

    // The list-view path: VaultReader builds the index from manifest.briefings,
    // newest-first, mapping each date to an index entry whose id == date.
    @Test func buildsBriefingIndexFromManifestDates() throws {
        let m = try decode(VaultManifest.self, Self.json)
        let dates = try #require(m.briefings)
        let index = dates.map { VaultBriefingIndexEntry(date: $0) }
        #expect(index.map(\.date) == ["2026-06-02", "2026-06-01", "2026-05-31"])
        #expect(index.first?.id == "2026-06-02")  // id is the date
    }

    @Test func emptyBriefingsListDecodes() throws {
        let json = """
            {"generatedAt": "2026-06-02T18:41:02Z", "briefings": [],
             "hasEnginesHistory": false, "hasMetrics": false}
            """
        let m = try decode(VaultManifest.self, json)
        #expect(m.briefings == [])
        #expect(m.hasMetrics == false)
    }
}

// MARK: - Engines history

@Suite("Engines history")
struct EnginesHistoryDecodingTests {
    // Mirrors vault_sync.py::push_engine_run output: version 1, generatedAt,
    // and runs[] with engine-specific counters. Includes one ingestion run
    // (chunks_added + by_source), one briefing run (briefings_total +
    // last_date), and a failed run carrying vaultSyncFailed.
    static let json = """
        {
          "version": 1,
          "generatedAt": "2026-06-02T18:41:02Z",
          "runs": [
            {
              "engine": "ingestion",
              "startedAt": "2026-06-02T02:00:00Z",
              "endedAt": "2026-06-02T02:18:31Z",
              "durationMs": 1111000,
              "status": "ok",
              "counters": {"chunks_added": 814, "by_source": {"notes": 312, "mail": 502}},
              "logId": "ingestion-20260602T020000Z"
            },
            {
              "engine": "briefing",
              "startedAt": "2026-06-02T07:00:00Z",
              "endedAt": "2026-06-02T07:00:42Z",
              "durationMs": 42000,
              "status": "ok",
              "counters": {"briefings_total": 73, "last_date": "2026-06-02"},
              "logId": "briefing-20260602T070000Z"
            },
            {
              "engine": "ingestion",
              "startedAt": "2026-06-01T02:00:00Z",
              "endedAt": "2026-06-01T02:01:00Z",
              "durationMs": 60000,
              "status": "failed",
              "counters": {"chunks_added": 0, "by_source": {}},
              "vaultSyncFailed": true
            }
          ]
        }
        """

    @Test func decodesRuns() throws {
        let h = try decode(VaultEnginesHistory.self, Self.json)
        #expect(h.version == 1)
        #expect(h.runs.count == 3)

        let first = h.runs[0]
        #expect(first.engine == "ingestion")
        #expect(first.status == "ok")
        #expect(first.startedAt == "2026-06-02T02:00:00Z")
        #expect(first.durationMs == 1111000)
        #expect(first.logId == "ingestion-20260602T020000Z")
        #expect(first.vaultSyncFailed == nil)

        let failed = h.runs[2]
        #expect(failed.status == "failed")
        #expect(failed.vaultSyncFailed == true)
        #expect(failed.logId == nil)
    }

    // The typed counter projections used by the Metrics page.
    @Test func projectsTypedCounters() throws {
        let h = try decode(VaultEnginesHistory.self, Self.json)

        let ingestion = try #require(h.runs[0].ingestionCounters)
        #expect(ingestion.chunksAdded == 814)
        #expect(ingestion.bySource == ["notes": 312, "mail": 502])

        let briefing = try #require(h.runs[1].briefingCounters)
        #expect(briefing.briefingsTotal == 73)
        #expect(briefing.lastDate == "2026-06-02")

        // Cross-engine projection returns nil (a briefing run has no ingestion
        // counters and vice-versa).
        #expect(h.runs[1].ingestionCounters == nil)
        #expect(h.runs[0].briefingCounters == nil)
    }

    // A counter number too large for `Int` must NOT crash the tolerant decode.
    // `AnyJSON.asInt` once used a trapping `Int(Double)` conversion, so an
    // out-of-range `chunks_added` (here 1e30) would abort the app; it must now
    // fall back to the `?? 0` default instead.
    @Test func outOfRangeCounterDoesNotCrash() throws {
        let json = """
            {
              "version": 1,
              "generatedAt": "x",
              "runs": [
                {
                  "engine": "ingestion",
                  "startedAt": "2026-06-02T02:00:00Z",
                  "endedAt": "2026-06-02T02:18:31Z",
                  "durationMs": 1000,
                  "status": "ok",
                  "counters": {"chunks_added": 1e30, "by_source": {}}
                }
              ]
            }
            """
        let h = try decode(VaultEnginesHistory.self, json)
        let ingestion = try #require(h.runs[0].ingestionCounters)
        #expect(ingestion.chunksAdded == 0)  // out-of-range → fallback, not a trap
    }

    // VaultStore surfaces the newest run per engine via `.last(where:)`, since
    // the Mac appends. Verify that selection picks the right rows.
    @Test func newestRunPerEngineIsLast() throws {
        let h = try decode(VaultEnginesHistory.self, Self.json)
        // Newest ingestion is runs[0] (ok) here only because runs[2] is older
        // but appears later; `.last(where:)` would pick runs[2]. Mirror the
        // real store semantics: the Mac appends, so the file's tail is newest.
        let newestIngestion = h.runs.last(where: { $0.engine == "ingestion" })
        #expect(newestIngestion?.status == "failed")
        let newestBriefing = h.runs.last(where: { $0.engine == "briefing" })
        #expect(newestBriefing?.briefingCounters?.briefingsTotal == 73)
    }
}

// MARK: - Metrics snapshot

@Suite("Metrics snapshot")
struct MetricsDecodingTests {
    // Mirrors metrics.json from the vault-schema doc / _build_vault_metrics:
    // totals, corpus split, the `memory` cumulative timeseries (note the
    // snake_case `by_source` inside series, which the model maps via
    // CodingKeys), and the read-only source catalogue.
    static let json = """
        {
          "version": 1,
          "generatedAt": "2026-06-02T02:18:31Z",
          "totalChunks": 48210,
          "corpus": {"personal": 39500, "world": 8710},
          "bySource": {"whatsapp": 18400, "mail": 11900, "notes": 7200},
          "ingestion": {
            "days": ["2026-06-01", "2026-06-02"],
            "sources": ["mail", "notes"],
            "series": [
              {"day": "2026-06-01", "total": 120, "by_source": {"mail": 80, "notes": 40}}
            ]
          },
          "memory": {
            "days": ["2026-06-01", "2026-06-02"],
            "sources": ["whatsapp", "mail", "notes"],
            "series": [
              {"day": "2026-06-01", "total": 47900, "by_source": {"whatsapp": 18400}},
              {"day": "2026-06-02", "total": 48210, "by_source": {"whatsapp": 18400}}
            ]
          },
          "sources": [
            {
              "name": "notes",
              "title": "Apple Notes",
              "description": "Your Apple Notes.",
              "chunks": 7200,
              "enabled": true,
              "lastFetchedAt": "2026-06-01T02:00:00Z",
              "historicDepth": "1y",
              "depthWindowEnv": "NOTES_DAYS_WINDOW",
              "root": null,
              "permissions": ["AppleEvents:Notes"],
              "usesWatermark": true,
              "requiresRoot": false,
              "dagStage": true,
              "dagOrder": 10
            }
          ]
        }
        """

    @Test func decodesTopLevel() throws {
        let m = try decode(VaultMetrics.self, Self.json)
        #expect(m.version == 1)
        #expect(m.totalChunks == 48210)
        #expect(m.corpus == ["personal": 39500, "world": 8710])
        #expect(m.bySource?["whatsapp"] == 18400)
    }

    // The `memory` timeseries backs the cumulative-memory chart; verify the
    // snake_case `by_source` key inside each point maps through CodingKeys.
    @Test func decodesMemoryTimeseries() throws {
        let m = try decode(VaultMetrics.self, Self.json)
        let memory = try #require(m.memory)
        #expect(memory.days == ["2026-06-01", "2026-06-02"])
        #expect(memory.series.count == 2)
        #expect(memory.series[1].day == "2026-06-02")
        #expect(memory.series[1].total == 48210)
        #expect(memory.series[0].bySource["whatsapp"] == 18400)
        #expect(memory.series[1].id == "2026-06-02")  // id == day
    }

    @Test func decodesSourceCatalogue() throws {
        let m = try decode(VaultMetrics.self, Self.json)
        let source = try #require(m.sources?.first)
        #expect(source.id == "notes")  // id == name
        #expect(source.title == "Apple Notes")
        #expect(source.chunks == 7200)
        #expect(source.enabled == true)
        #expect(source.root == nil)
        #expect(source.permissions == ["AppleEvents:Notes"])
        #expect(source.usesWatermark == true)
        #expect(source.dagOrder == 10)
    }
}

// MARK: - Robustness

@Suite("Decoding robustness")
struct RobustnessDecodingTests {
    // VaultReader.readJSON throws when the bytes don't decode; the caller
    // (VaultStore.refresh) catches and surfaces lastError rather than crashing.
    // These tests assert decode *throws* (does not crash) for bad input.

    @Test func emptyDataThrows() {
        #expect(throws: (any Error).self) {
            try decode(VaultBriefing.self, "")
        }
    }

    @Test func malformedJSONThrows() {
        #expect(throws: (any Error).self) {
            try decode(VaultManifest.self, "{not valid json")
        }
    }

    @Test func wrongTypeThrows() {
        // briefings should be an array of strings; a number is a type error.
        let json = """
            {"generatedAt": "x", "briefings": 7, "hasEnginesHistory": true, "hasMetrics": true}
            """
        #expect(throws: (any Error).self) {
            try decode(VaultManifest.self, json)
        }
    }

    @Test func missingRequiredFieldThrows() {
        // VaultBriefing.htmlBody is non-optional; omitting it must throw, not
        // silently produce an empty body.
        let json = #"{"id": "x", "date": "2026-06-02"}"#
        #expect(throws: (any Error).self) {
            try decode(VaultBriefing.self, json)
        }
    }

    @Test func emptyRunsArrayDecodes() throws {
        // An empty (but well-formed) history is valid — a fresh vault.
        let json = #"{"version": 1, "generatedAt": "x", "runs": []}"#
        let h = try decode(VaultEnginesHistory.self, json)
        #expect(h.runs.isEmpty)
    }
}

import Foundation

// On-disk shape of the iCloud Drive vault written by the Mac pipeline
// (`estormi_ingestion/shared/delivery/vault_sync.py`). The Mac is the only writer; this app is
// read-only. Field names mirror the JSON keys verbatim — keep them in lockstep
// with `vault_sync.py` when the schema evolves.

// MARK: - Manifest + Briefing

struct VaultManifest: Codable, Equatable {
    let generatedAt: String?
    let briefings: [String]?
    let hasEnginesHistory: Bool?
    let hasMetrics: Bool?
}

struct VaultBriefing: Codable, Equatable, Identifiable {
    let id: String
    let date: String
    let title: String?
    let htmlBody: String
    // Decoded for forward-compatibility / schema parity with the Mac's payload;
    // no view renders these counts today.
    let sourceCount: Int?
    let videoCount: Int?
    let articleCount: Int?
    let generatedAt: String?
    /// Vault-relative path of the narration audio (`briefings/<date>.m4a`),
    /// present only when the Mac synthesized speech for this briefing (Voxtral
    /// TTS — see `tts_local.py`). The reader resolves it to a file URL; the
    /// player only appears when audio exists.
    let audioPath: String?
}

struct VaultBriefingIndexEntry: Codable, Equatable, Identifiable {
    let date: String
    var id: String { date }
}

// MARK: - Engines history

struct VaultEngineRun: Codable, Equatable, Hashable {
    let engine: String
    let startedAt: String?
    let endedAt: String?
    let durationMs: Double?
    let status: String?
    let counters: [String: AnyJSON]?
    let vaultSyncFailed: Bool?
    /// Filename stem of this run's captured log under `engine-logs/<logId>.log`.
    /// Present only on recent runs (the Mac keeps a bounded number of files);
    /// the companion fetches the file on demand when the row is tapped.
    let logId: String?
}

struct VaultEnginesHistory: Codable, Equatable {
    let version: Int?
    let generatedAt: String?
    let runs: [VaultEngineRun]
}

// Strongly-typed view over `counters` for the engines we know about. Pulled
// from `estormi_server/server/jobs.py` (see the per-engine counter shapes).
extension VaultEngineRun {
    var ingestionCounters: IngestionCounters? {
        guard engine == "ingestion", let c = counters else { return nil }
        return IngestionCounters(
            chunksAdded: c["chunks_added"]?.asInt ?? 0,
            bySource: (c["by_source"]?.asObject ?? [:])
                .compactMapValues { $0.asInt }
        )
    }
    var briefingCounters: BriefingCounters? {
        guard engine == "briefing", let c = counters else { return nil }
        return BriefingCounters(
            briefingsTotal: c["briefings_total"]?.asInt ?? 0,
            lastDate: c["last_date"]?.asString
        )
    }
}

struct IngestionCounters: Equatable {
    let chunksAdded: Int
    let bySource: [String: Int]
}
struct BriefingCounters: Equatable {
    let briefingsTotal: Int
    let lastDate: String?
}

// MARK: - Metrics snapshot

// `metrics.json` — a point-in-time mirror of the whole store the Mac
// overwrites each engine run. Backs the Metrics page's total-chunk count, the
// cumulative-memory stacked-area chart, and the read-only source catalogue.
// Built by `server/jobs.py::_build_vault_metrics`. (The Mac also writes an
// `ingestion` timeseries for compatibility; the app no longer charts it, so it
// is not decoded here — unknown keys are ignored.)

struct VaultMetrics: Codable, Equatable {
    let version: Int?
    let generatedAt: String?
    let totalChunks: Int?
    let corpus: [String: Int]?
    let bySource: [String: Int]?
    let memory: VaultTimeseries?
    let sources: [VaultSourceInfo]?
}

// Tolerant of a partial optional `memory` subtree: missing arrays default to
// empty so a malformed/absent block degrades only the chart, consistent with
// the schema doc's "tolerate unknown keys / add fields without breaking older
// companions" contract and TimeseriesCard's existing empty-state handling.
struct VaultTimeseries: Codable, Equatable {
    let days: [String]
    let sources: [String]
    let series: [VaultTimeseriesPoint]

    enum CodingKeys: String, CodingKey {
        case days, sources, series
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        days = try c.decodeIfPresent([String].self, forKey: .days) ?? []
        sources = try c.decodeIfPresent([String].self, forKey: .sources) ?? []
        series = try c.decodeIfPresent([VaultTimeseriesPoint].self, forKey: .series) ?? []
    }
}

struct VaultTimeseriesPoint: Codable, Equatable, Identifiable {
    var id: String { day }
    let day: String
    let total: Int
    let bySource: [String: Int]

    enum CodingKeys: String, CodingKey {
        case day, total
        case bySource = "by_source"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        day = try c.decode(String.self, forKey: .day)
        total = try c.decodeIfPresent(Int.self, forKey: .total) ?? 0
        bySource = try c.decodeIfPresent([String: Int].self, forKey: .bySource) ?? [:]
    }
}

// One registered connector, with its spec metadata joined to live config. All
// read-only — the phone never mutates source settings.
struct VaultSourceInfo: Codable, Equatable, Identifiable {
    var id: String { name }
    let name: String
    let title: String?
    let description: String?
    let chunks: Int?
    let enabled: Bool?
    let lastFetchedAt: String?
    let historicDepth: String?
    let depthWindowEnv: String?
    let root: String?
    let permissions: [String]?
    let usesWatermark: Bool?
    let requiresRoot: Bool?
    let dagStage: Bool?
    let dagOrder: Int?
}

// MARK: - AnyJSON

// `counters` and similar dicts are heterogeneous — strings, ints, doubles,
// nested objects, arrays. We hold them as type-erased JSON and unwrap on
// demand at the call site.
enum AnyJSON: Codable, Equatable, Hashable {
    case string(String)
    case number(Double)
    case bool(Bool)
    case null
    case array([AnyJSON])
    case object([String: AnyJSON])

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null; return }
        if let b = try? c.decode(Bool.self) { self = .bool(b); return }
        if let n = try? c.decode(Double.self) { self = .number(n); return }
        if let s = try? c.decode(String.self) { self = .string(s); return }
        if let a = try? c.decode([AnyJSON].self) { self = .array(a); return }
        if let o = try? c.decode([String: AnyJSON].self) { self = .object(o); return }
        throw DecodingError.dataCorruptedError(
            in: c, debugDescription: "Unknown JSON value")
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .null: try c.encodeNil()
        case .bool(let b): try c.encode(b)
        case .number(let n): try c.encode(n)
        case .string(let s): try c.encode(s)
        case .array(let a): try c.encode(a)
        case .object(let o): try c.encode(o)
        }
    }

    var asString: String? { if case .string(let s) = self { return s } else { return nil } }
    var asInt: Int? {
        // `Int(Double)` traps on NaN/infinity and out-of-range magnitudes, so a
        // hostile or malformed number in the vault would crash the app —
        // contrary to the tolerant-decode contract. `Int(exactly:)` returns nil
        // instead; we floor the fractional part first so a plain `7.0` (and
        // `7.9`) still yield 7 rather than dropping to nil on the exactness check.
        if case .number(let n) = self {
            guard n.isFinite else { return nil }
            return Int(exactly: n.rounded(.towardZero))
        }
        if case .string(let s) = self { return Int(s) }
        return nil
    }
    var asObject: [String: AnyJSON]? {
        if case .object(let o) = self { return o }
        return nil
    }
}

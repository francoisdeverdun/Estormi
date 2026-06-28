import Foundation

// Bundled sample vault — a self-contained demo briefing plus metrics and engine
// history (no narration audio). The data is compiled into every build and
// loaded by VaultStore.enterSampleMode(). Two entry points:
//   • Release: the empty states' "Explore a sample" button, so the app is fully
//     explorable — and App Store-reviewable — without a paired Mac or a chosen
//     iCloud folder.
//   • DEBUG: `-EstormiMockVault YES` auto-loads it on launch (the simulator has
//     no vault folder), mirroring the launch-arg scaffolding in RootView
//     (EstormiStartTab / EstormiGallery).
//
//   xcrun simctl launch booted app.estormi.ios -EstormiMockVault YES
enum MockVault {
    // Auto-load the sample on launch — DEBUG-only (the `-EstormiMockVault`
    // launch arg). In release the sample is reached explicitly via the
    // "Explore a sample" button, never automatically.
    static var isEnabled: Bool {
        #if DEBUG
        return UserDefaults.standard.bool(forKey: "EstormiMockVault")
        #else
        return false
        #endif
    }

    static let date = "2026-06-02"

    static var manifest: VaultManifest {
        VaultManifest(
            generatedAt: "\(date)T07:00:00Z", briefings: [date], hasEnginesHistory: true,
            hasMetrics: true)
    }

    static var indexEntry: VaultBriefingIndexEntry {
        VaultBriefingIndexEntry(date: date)
    }

    // A sample briefing for the mock vault. No narration audio — the
    // mock has no .m4a, so the audio bar stays hidden (audioPath: nil).
    static var briefing: VaultBriefing {
        VaultBriefing(
            id: "briefing-\(date)",
            date: date,
            title: "Briefing du 2 juin",
            htmlBody: """
                <h1>Briefing du 2 juin</h1>
                <p>Bonjour. Voici le fil de ta journée, tissé à partir de tes \
                sources. Trois fils convergent ce matin autour d'un même sujet.</p>
                <p>Le premier vient de tes messages : la réunion de jeudi a été \
                déplacée à mardi prochain, quatorze heures. Le deuxième, dans ton \
                courrier, confirme le même créneau pour la signature du contrat.</p>
                <p>Enfin, côté actualité, une brève : <em>les marchés ont ouvert \
                en hausse aujourd'hui.</em> Rien qui touche directement tes \
                projets en cours.</p>
                <p>Belle journée à toi, et que la mémoire te serve fidèlement.</p>
                """,
            sourceCount: 3,
            videoCount: 0,
            articleCount: 1,
            generatedAt: "\(date)T07:00:00Z",
            audioPath: nil)
    }

    // Sample whole-store snapshot so the Metrics page renders fully under
    // `-EstormiMockVault` (total chunks, corpus split, the cumulative-memory
    // chart, and the source catalogue). Decoded from a JSON literal that
    // mirrors the on-disk `metrics.json` shape (see docs/specs/vault-schema.md).
    static var metrics: VaultMetrics? { decode(metricsJSON) }

    // Sample engines-history log so the Ingestion and Briefing engine cards
    // have a most-recent run to project (counters, status, duration, logId).
    static var enginesHistory: VaultEnginesHistory? { decode(enginesHistoryJSON) }

    private static func decode<T: Decodable>(_ json: String) -> T? {
        guard let data = json.data(using: .utf8) else { return nil }
        return try? JSONDecoder().decode(T.self, from: data)
    }

    private static let metricsJSON = """
        {
          "version": 1,
          "generatedAt": "\(date)T07:00:00Z",
          "totalChunks": 48210,
          "corpus": {"personal": 39500, "world": 8710},
          "bySource": {"whatsapp": 18400, "mail": 11900, "notes": 7200},
          "memory": {
            "days": ["2026-05-31", "2026-06-01", "2026-06-02"],
            "sources": ["whatsapp", "mail", "notes"],
            "series": [
              {"day": "2026-05-31", "total": 47600, "by_source": {"whatsapp": 18100, "mail": 11800, "notes": 7100}},
              {"day": "2026-06-01", "total": 47950, "by_source": {"whatsapp": 18250, "mail": 11850, "notes": 7150}},
              {"day": "2026-06-02", "total": 48210, "by_source": {"whatsapp": 18400, "mail": 11900, "notes": 7200}}
            ]
          },
          "sources": [
            {
              "name": "whatsapp", "title": "WhatsApp", "description": "Conversations",
              "chunks": 18400, "enabled": true, "lastFetchedAt": "\(date)T02:00:00Z",
              "historicDepth": "1y", "depthWindowEnv": "WHATSAPP_DAYS_WINDOW", "root": null,
              "permissions": ["FullDiskAccess"],
              "usesWatermark": true, "requiresRoot": false, "dagStage": true, "dagOrder": 1
            },
            {
              "name": "mail", "title": "Apple Mail", "description": "Messages",
              "chunks": 11900, "enabled": true, "lastFetchedAt": "\(date)T02:00:00Z",
              "historicDepth": "1y", "depthWindowEnv": "MAIL_DAYS_WINDOW", "root": null,
              "permissions": ["AppleEvents:Mail"],
              "usesWatermark": true, "requiresRoot": false, "dagStage": true, "dagOrder": 2
            },
            {
              "name": "notes", "title": "Apple Notes", "description": "Notes",
              "chunks": 7200, "enabled": true, "lastFetchedAt": "\(date)T02:00:00Z",
              "historicDepth": "1y", "depthWindowEnv": "NOTES_DAYS_WINDOW", "root": null,
              "permissions": ["AppleEvents:Notes"],
              "usesWatermark": true, "requiresRoot": false, "dagStage": true, "dagOrder": 3
            }
          ]
        }
        """

    private static let enginesHistoryJSON = """
        {
          "version": 1,
          "generatedAt": "\(date)T07:00:00Z",
          "runs": [
            {
              "engine": "ingestion",
              "startedAt": "\(date)T02:00:00Z",
              "endedAt": "\(date)T02:18:31Z",
              "durationMs": 1111000,
              "status": "ok",
              "counters": {"chunks_added": 814, "by_source": {"whatsapp": 300, "mail": 412, "notes": 102}},
              "logId": "ingestion-20260602T020000Z"
            },
            {
              "engine": "briefing",
              "startedAt": "\(date)T07:00:00Z",
              "endedAt": "\(date)T07:00:42Z",
              "durationMs": 42000,
              "status": "ok",
              "counters": {"briefings_total": 128, "last_date": "\(date)"},
              "logId": "briefing-20260602T070000Z"
            }
          ]
        }
        """
}

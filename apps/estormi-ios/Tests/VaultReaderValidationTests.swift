import Foundation
import Testing

@testable import Estormi

// Path-slug guards in `VaultReader`. The vault folder is user-picked and
// security-scoped, but the file *name* is built from caller-supplied strings
// (`briefings/<date>.json`, `engine-logs/<id>.log`). Both are validated against
// a strict pattern BEFORE any I/O so a crafted date/id can't escape its
// directory (path traversal). These guards had no direct test; pin them with
// positive controls and traversal-attempt cases.
@Suite("VaultReader path-slug guards")
struct VaultReaderValidationTests {
    @Test("readBriefing rejects non-date / traversal strings")
    func rejectsBadDates() async {
        let bad = [
            "../../etc/passwd",
            "2026-01-01/../../secret",
            "2026-6-1",  // not zero-padded → not the writer's slug
            "2026-01-01.json",
            "latest",
            "",
        ]
        for date in bad {
            await #expect(throws: VaultError.self) {
                _ = try await VaultReader.readBriefing(date: date)
            }
        }
    }

    @Test("readBriefing accepts a well-formed date (past the slug guard)")
    func acceptsGoodDate() async {
        // A valid slug clears the date guard; with no vault folder selected it
        // then fails at folder resolution — which is fine here. The one thing it
        // must NOT do is reject the date as invalid.
        do {
            _ = try await VaultReader.readBriefing(date: "2026-06-21")
        } catch let error as VaultError {
            #expect(error.message != "Invalid briefing date.")
        } catch {
            // Any non-VaultError (e.g. folder resolution) is acceptable.
        }
    }

    @Test("readEngineLog rejects ids with separators or unsafe chars")
    func rejectsBadLogIds() async {
        let bad = ["../secret", "a/b", "foo bar", "id;rm -rf", "tab\tid", ""]
        for id in bad {
            await #expect(throws: VaultError.self) {
                _ = try await VaultReader.readEngineLog(id: id)
            }
        }
    }
}

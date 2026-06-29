import Foundation
import Testing

@testable import Estormi

// Parser tests for `EngineLogFormat.parse` — the iOS port of the web-ui shared
// log tokeniser (`packages/web-ui/src/lib/logFormat.ts`). The two surfaces must
// render a run's log identically; cross-surface parity is otherwise only
// contract-tested by tests/contract/test_log_format_parity.py, so these tests
// reuse the SAME fixture inputs as the web-ui suite
// (`packages/web-ui/src/__tests__/logFormat.test.ts`) to keep the ports pinned.
//
// The Swift port covers tokenising + level-inference + run-break + marker
// highlighting; the run-scoping / dropRe / httpx-noise opts are a web-ui-only
// concern (the iOS callers scope before parsing), so they have no Swift mirror.

private func parse(_ raw: String, marker: NSRegularExpression? = nil) -> [LogLine] {
    EngineLogFormat.parse(raw, markerRe: marker)
}

@Suite("EngineLogFormat — tokenising")
struct EngineLogFormatTokenisingTests {
    @Test func parsesAnIngestionTagLine() throws {
        let l = try #require(parse("[21:50:16] [connectors] notes: starting").first)
        #expect(l.time == "21:50:16")
        #expect(l.tag == "connectors")
        #expect(l.message == "notes: starting")
        #expect(l.isRunBreak == false)
    }

    @Test func parsesAnIngestionLineWithNoTag() throws {
        let l = try #require(parse("[22:57:49] 0 notes exported to /tmp/notes/").first)
        #expect(l.time == "22:57:49")
        #expect(l.tag == "")
        #expect(l.message == "0 notes exported to /tmp/notes/")
    }

    @Test func parsesABriefingLineKeepingTheLevelTokenAsTheTag() throws {
        let l = try #require(
            parse("[briefing] 22:37:43 INFO correlation graph: 4 cross-source thread(s)").first)
        #expect(l.time == "22:37:43")
        #expect(l.tag == "INFO")
        #expect(l.level == .info)
        #expect(l.message.contains("correlation graph"))
    }

    @Test func parsesADistillLineTheSameWayAsABriefingLine() throws {
        let l = try #require(
            parse("[distill] 20:47:24 INFO dataset: {'train': 95, 'valid': 17}").first)
        #expect(l.time == "20:47:24")
        #expect(l.tag == "INFO")
        #expect(l.level == .info)
        #expect(l.message.contains("dataset:"))
    }

    @Test func rendersARunBreakLineAsASeparator() throws {
        let l = try #require(parse("── run 20260603-215016 ──").first)
        #expect(l.isRunBreak == true)
        #expect(l.message == "run 20260603-215016")
    }

    @Test func dropsEmptyLines() {
        // A blank line between two real lines is omitted, not tokenised.
        let lines = parse("[10:00:00] [briefing] keep me\n\n[10:00:01] [briefing] and me")
        #expect(lines.count == 2)
        #expect(lines[0].message == "keep me")
        #expect(lines[1].message == "and me")
    }

    @Test func dropsUnrecognisedLineShapes() {
        // A line matching none of the three shapes is dropped (returns nil from
        // tokenize), mirroring the web-ui parser's `return null`.
        #expect(parse("a bare line with no timestamp").isEmpty)
    }
}

@Suite("EngineLogFormat — level inference")
struct EngineLogFormatLevelTests {
    @Test func infersOkFromContent() throws {
        let l = try #require(parse("[22:57:54] [connectors] notes: ok in 5.2s").first)
        #expect(l.level == .ok)
    }

    @Test func infersErrorFromAFailureVerb() throws {
        let l = try #require(parse("[22:57:54] [mail] mail: failed — boom").first)
        #expect(l.level == .error)
    }

    @Test func infersErrorFromANonZeroFailedCount() throws {
        let l = try #require(parse("[22:57:54] [notes] 3 failed to post").first)
        #expect(l.level == .error)
    }

    // The headline parity case: a "(0 failed)" summary is success, so it must
    // NOT trip the error colour (regression the inferLevel comment guards).
    @Test func doesNotFlagAZeroFailureSuccessSummaryAsAnError() throws {
        let l = try #require(
            parse("[22:57:54] [notes] Done — 0 notes processed, 0 chunks indexed (0 failed).")
                .first)
        #expect(l.level == .ok)
    }
}

@Suite("EngineLogFormat — markers")
struct EngineLogFormatMarkerTests {
    // Mirrors the web-ui `flags marker lines` case: a `=== … ===` stage header
    // in a per-source log is highlighted when SOURCE_MARKER is supplied (here
    // the Swift `sourceMarker`, the same regex).
    @Test func flagsMarkerLines() throws {
        let l = try #require(
            parse("[22:27:55] [dag] === knowledge ok (85s) ===", marker: EngineLogFormat.sourceMarker)
                .first)
        #expect(l.isMarker == true)
    }

    @Test func leavesNonMarkerLinesUnflagged() throws {
        let l = try #require(
            parse("[22:27:55] [dag] just a normal line", marker: EngineLogFormat.sourceMarker)
                .first)
        #expect(l.isMarker == false)
    }

    // A run-break is always a marker, regardless of the marker regex (mirrors
    // `isMarker = tok.isRunBreak || …`).
    @Test func runBreakIsAlwaysAMarker() throws {
        let l = try #require(parse("── run 20260603-215016 ──", marker: nil).first)
        #expect(l.isRunBreak == true)
        #expect(l.isMarker == true)
    }

    // The briefing-phase markers track the day-vision DAG regexes; a
    // `correlation graph:` line is highlighted under briefingMarker.
    @Test func flagsBriefingPhaseMarkers() throws {
        let l = try #require(
            parse(
                "[briefing] 22:37:43 INFO correlation graph: 4 cross-source thread(s)",
                marker: EngineLogFormat.briefingMarker
            ).first)
        #expect(l.isMarker == true)
    }
}

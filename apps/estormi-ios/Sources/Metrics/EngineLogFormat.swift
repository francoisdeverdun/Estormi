import SwiftUI

// Mirrors packages/web-ui/src/lib/logFormat.ts + components/log/LogStream.tsx —
// the macOS "Atelier" formatted-log rendering — so a run's log reads identically
// on both surfaces: a time gutter · a colour-coded tag · the message, with
// run-break separators and highlighted stage markers.
//
// Recognised line shapes (kept in lockstep with logFormat.ts):
//   engine:    [briefing]/[distill] HH:MM:SS LEVEL message
//   ingestion: [HH:MM:SS] [tag] message  /  [HH:MM:SS] message
//   run break: ── run 20260603-215016 ──
//
// sourceMarker mirrors logFormat.ts SOURCE_MARKER; briefingMarker tracks the
// phase `marker` regexes in packages/web-ui/src/lib/briefingPhases.ts.

enum LogLevel {
    case info, warn, error, ok, other
}

struct LogLine {
    let time: String
    /// Gutter channel — a level (INFO) or a source tag (notes, dag, connectors).
    let tag: String
    let level: LogLevel
    let message: String
    /// A run-boundary line — rendered as a full-width separator.
    let isRunBreak: Bool
    /// A noteworthy line (phase/stage transition) — highlighted.
    let isMarker: Bool
}

enum EngineLogFormat {
    private static let engineRe = try! NSRegularExpression(
        pattern: #"^\[(?:briefing|distill)\]\s+(\d{1,2}:\d{2}:\d{2})\s+([A-Z]+)\s+(.*)$"#)
    private static let tsTagRe = try! NSRegularExpression(
        pattern: #"^\[(\d{1,2}:\d{2}:\d{2})\]\s+(?:\[([^\]]+)\]\s+)?(.*)$"#)
    private static let runBreakRe = try! NSRegularExpression(pattern: #"^──\s*run\b"#)

    /// Highlight stage boundaries + step headers in an ingestion run log
    /// (logFormat.ts `SOURCE_MARKER`).
    static let sourceMarker = try! NSRegularExpression(
        pattern: #":\s*(starting|ok|fail)\b|Done\b|Step \d|==="#)
    /// Highlight phase transitions in a briefing run log (the day-vision DAG).
    static let briefingMarker = try! NSRegularExpression(
        pattern: #"world corpus:|news_synthesis:|extractor facts|enrichments:|event correlations:|correlation graph:|day_vision:|briefing critic:|vault write:|\bDone\b"#)

    /// Parse a raw run log into uniform, render-ready lines.
    static func parse(_ raw: String, markerRe: NSRegularExpression?) -> [LogLine] {
        var out: [LogLine] = []
        for rawLine in raw.split(separator: "\n", omittingEmptySubsequences: false) {
            let text = String(rawLine).trimmingTrailingWhitespace()
            if text.isEmpty { continue }
            guard let tok = tokenize(text) else { continue }
            let isMarker = tok.isRunBreak || (markerRe.map { matches($0, tok.message) } ?? false)
            out.append(
                LogLine(
                    time: tok.time, tag: tok.tag, level: tok.level, message: tok.message,
                    isRunBreak: tok.isRunBreak, isMarker: isMarker))
        }
        return out
    }

    // MARK: - Tokenising

    private static func tokenize(_ text: String) -> LogLine? {
        if matches(runBreakRe, text) {
            let msg = text.replacingOccurrences(of: "─", with: "")
                .trimmingCharacters(in: .whitespaces)
            return LogLine(
                time: "", tag: "run", level: .other, message: msg, isRunBreak: true,
                isMarker: false)
        }
        if let g = groups(engineRe, text) {
            return LogLine(
                time: g[1] ?? "", tag: g[2] ?? "", level: mapNamedLevel(g[2] ?? ""),
                message: g[3] ?? "", isRunBreak: false, isMarker: false)
        }
        if let g = groups(tsTagRe, text) {
            let message = g[3] ?? ""
            return LogLine(
                time: g[1] ?? "", tag: g[2] ?? "", level: inferLevel(message),
                message: message, isRunBreak: false, isMarker: false)
        }
        return nil
    }

    private static func mapNamedLevel(_ raw: String) -> LogLevel {
        switch raw {
        case "INFO": return .info
        case "WARNING", "WARN": return .warn
        case "ERROR", "CRITICAL": return .error
        default: return .other
        }
    }

    /// Infer a level for tag-style ingestion lines that carry no explicit level.
    /// A "(0 failed)" summary is success, so it must NOT trip the error colour.
    private static func inferLevel(_ message: String) -> LogLevel {
        let m = message.lowercased()
        let failed =
            regexHit(m, #"\b[1-9]\d* (failed|errors?)\b"#)
            || regexHit(m, #"✗|✘|traceback|exception|\berror:|\bfailed (to|—|-|:)"#)
        if failed { return .error }
        if regexHit(m, #"\bok\b|\bdone\b|✓|complete|completed|success"#) { return .ok }
        if regexHit(m, #"\bwarn"#) { return .warn }
        return .other
    }

    // MARK: - Regex helpers

    private static func matches(_ re: NSRegularExpression, _ s: String) -> Bool {
        re.firstMatch(in: s, range: NSRange(s.startIndex..<s.endIndex, in: s)) != nil
    }

    private static func regexHit(_ s: String, _ pattern: String) -> Bool {
        s.range(of: pattern, options: .regularExpression) != nil
    }

    /// Capture groups of the first match (index 0 is the whole match), or nil.
    private static func groups(_ re: NSRegularExpression, _ s: String) -> [String?]? {
        let ns = s as NSString
        guard let m = re.firstMatch(in: s, range: NSRange(location: 0, length: ns.length))
        else { return nil }
        return (0..<m.numberOfRanges).map { i in
            let r = m.range(at: i)
            return r.location == NSNotFound ? nil : ns.substring(with: r)
        }
    }
}

private extension String {
    func trimmingTrailingWhitespace() -> String {
        var s = self[...]
        while let last = s.last, last == " " || last == "\t" || last == "\r" {
            s = s.dropLast()
        }
        return String(s)
    }
}

// MARK: - Rendering (mirrors LogStream.tsx)

/// One formatted log line — the time gutter, the colour-coded tag, the message.
/// Run breaks render as a full-width gilt separator; markers get a gold rule and
/// a faint gold wash.
struct PrettyLogRow: View {
    let line: LogLine

    var body: some View {
        if line.isRunBreak { runBreak } else { logRow }
    }

    private var runBreak: some View {
        HStack(spacing: 8) {
            giltRule
            Text(line.message.uppercased())
                .font(EstormiFont.display(9, bold: false))
                .tracking(2)
                .foregroundStyle(EstormiColor.orAncien)
                .fixedSize()
            giltRule
        }
        .padding(.vertical, 6)
    }

    private var giltRule: some View {
        Rectangle()
            .fill(EstormiColor.orAncien.opacity(0.22))
            .frame(height: 1)
    }

    private var logRow: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(line.time)
                .foregroundStyle(EstormiColor.parchemin.opacity(0.62))
            Text(line.tag)
                .foregroundStyle(tagColor)
                .frame(width: 76, alignment: .leading)
                .lineLimit(1)
                .truncationMode(.tail)
            Text(line.message)
                .foregroundStyle(line.isMarker ? EstormiColor.orClair : EstormiColor.parchemin)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .font(.system(size: 11.5, design: .monospaced))
        .padding(.vertical, 1)
        .padding(.leading, line.isMarker ? 6 : 8)
        .padding(.trailing, 8)
        .background(line.isMarker ? EstormiColor.orAncien.opacity(0.06) : Color.clear)
        .overlay(alignment: .leading) {
            Rectangle()
                .fill(line.isMarker ? EstormiColor.orAncien : Color.clear)
                .frame(width: 2)
        }
    }

    private var tagColor: Color {
        switch line.level {
        case .ok: return EstormiColor.vertSauge
        case .warn: return EstormiColor.orClair
        case .error: return EstormiColor.rougeClair
        case .info, .other: return EstormiColor.parchemin.opacity(0.62)
        }
    }
}

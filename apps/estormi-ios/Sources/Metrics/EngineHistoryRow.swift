import SwiftUI

struct EngineHistorySection: View {
    let history: VaultEnginesHistory?

    // Wraps a run with its row index so `.sheet(item:)` has a stable identity
    // even when two runs share a start timestamp.
    private struct SelectedRun: Identifiable {
        let id: Int
        let run: VaultEngineRun
    }
    @State private var selected: SelectedRun?

    var body: some View {
        GildedPanel(tone: .gold) {
            VStack(alignment: .leading, spacing: 12) {
                Text("ENGINE RUNS")
                    .font(EstormiFont.display(11, bold: true))
                    .tracking(3.4)
                    .foregroundStyle(EstormiColor.orSombre)
                // The Mac appends runs oldest-first, so reverse before taking
                // the 10 most recent for the newest-first activity panel.
                if let runs = history?.runs.reversed().prefix(10), !runs.isEmpty {
                    ForEach(Array(runs.enumerated()), id: \.offset) { offset, run in
                        EngineHistoryRow(run: run) {
                            selected = SelectedRun(id: offset, run: run)
                        }
                    }
                } else {
                    Text("No engine runs recorded yet.")
                        .font(EstormiTypeScale.bodySmall)
                        .foregroundStyle(EstormiColor.parchemin.opacity(0.6))
                }
            }
        }
        .sheet(item: $selected) { sel in
            EngineRunLogSheet(run: sel.run)
        }
    }
}

struct EngineHistoryRow: View {
    let run: VaultEngineRun
    /// Tapping opens the run's log modal. Only wired up when `run` has a log.
    var onTap: (() -> Void)?

    private var hasLogs: Bool { !(run.logId ?? "").isEmpty }

    var body: some View {
        if hasLogs, let onTap {
            Button(action: onTap) { rowContent }
                .buttonStyle(.plain)
        } else {
            rowContent
        }
    }

    private var rowContent: some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            // WCAG 1.4.1: status must not be color-only. The SF Symbol's shape
            // differs by outcome (check / x / minus), so colorblind users can
            // still tell success from failure from skipped; the gilt token
            // keeps the original sighted colour cue as a redundant second
            // channel. `.accessibilityHidden` so VoiceOver speaks the status
            // once via the row's combined label, not twice.
            Image(systemName: statusSymbol)
                .font(.system(size: 9, weight: .bold))
                .foregroundStyle(statusColor)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text(run.engine.capitalized)
                    .font(EstormiTypeScale.bodyLarge)
                    .foregroundStyle(EstormiColor.parcheminOs)
                if let started = run.startedAt {
                    Text(EstormiDate.shortDateTime(started))
                        .font(EstormiFont.body(11))
                        .foregroundStyle(EstormiColor.parchemin.opacity(0.55))
                }
            }
            Spacer()
            if let ms = run.durationMs {
                Text(formatDuration(ms))
                    .font(EstormiFont.display(12, bold: true))
                    .tracking(1.2)
                    .foregroundStyle(EstormiColor.orClair)
            }
            if hasLogs {
                Image(systemName: "chevron.right")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(EstormiColor.orSombre)
            }
        }
        .padding(.vertical, 4)
        .contentShape(Rectangle())
        // Without this the row only exposes the child Texts (engine, time,
        // duration) and the outcome stays silent for VoiceOver. Combine the
        // children into one element and speak an explicit label that names the
        // status word, so the run reads as a single coherent announcement and
        // the chevron / status glyph don't get announced on their own.
        .accessibilityElement(children: .combine)
        .accessibilityLabel(accessibilityLabel)
    }

    /// VoiceOver label: engine name, spoken status, then the started time and
    /// duration when present — built from the same data the Texts render.
    private var accessibilityLabel: String {
        var parts = ["\(run.engine.capitalized), \(statusWord)"]
        if let started = run.startedAt {
            parts.append(EstormiDate.shortDateTime(started))
        }
        if let ms = run.durationMs {
            parts.append(formatDuration(ms))
        }
        return parts.joined(separator: ", ")
    }

    private var statusColor: Color {
        switch run.status?.lowercased() {
        case "ok", "success", "completed": return EstormiColor.vertSauge
        case "failed", "error": return EstormiColor.pourpre
        case "skipped", "noop": return EstormiColor.orSombre
        default: return EstormiColor.enluminureClair
        }
    }

    // WCAG 1.4.1 redundant cue: a per-outcome glyph shape parallels
    // `statusColor` so meaning survives without colour perception.
    private var statusSymbol: String {
        switch run.status?.lowercased() {
        case "ok", "success", "completed": return "checkmark.circle.fill"
        case "failed", "error": return "xmark.octagon.fill"
        case "skipped", "noop": return "minus.circle.fill"
        default: return "circle.fill"
        }
    }

    // Spoken outcome for the row's VoiceOver label, paralleling `statusColor`.
    private var statusWord: String {
        switch run.status?.lowercased() {
        case "ok", "success", "completed": return "Succeeded"
        case "failed", "error": return "Failed"
        case "skipped", "noop": return "Skipped"
        default: return run.status ?? "Unknown"
        }
    }

    private func formatDuration(_ ms: Double) -> String {
        if ms < 1000 { return String(format: "%dms", Int(ms)) }
        let s = ms / 1000
        if s < 60 { return String(format: "%.1fs", s) }
        let m = Int(s / 60)
        let rem = Int(s.truncatingRemainder(dividingBy: 60))
        return "\(m)m \(rem)s"
    }
}

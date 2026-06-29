import SwiftUI

// Modal that shows one engine run's full captured log. The log lives in its
// own `engine-logs/<logId>.log` file in the vault (only recent runs keep one),
// so we fetch it on demand when the sheet opens rather than carrying every
// log in the history index.

struct EngineRunLogSheet: View {
    let run: VaultEngineRun
    @EnvironmentObject private var store: VaultStore
    @Environment(\.dismiss) private var dismiss

    @State private var logText: String?
    @State private var isLoading = true

    var body: some View {
        ZStack {
            EstormiColor.charbon.ignoresSafeArea()
            VStack(spacing: 0) {
                header
                Divider().overlay(EstormiColor.orSombre.opacity(0.4))
                content
            }
        }
        .presentationDragIndicator(.visible)
        .task { await load() }
    }

    // MARK: - Header

    private var header: some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                Text(run.engine.capitalized)
                    .font(EstormiFont.display(20, bold: true))
                    .foregroundStyle(EstormiColor.parcheminOs)
                if let started = run.startedAt {
                    Text(EstormiDate.shortDateTime(started))
                        .font(EstormiFont.body(12))
                        .foregroundStyle(EstormiColor.parchemin.opacity(0.55))
                }
            }
            Spacer()
            Button { dismiss() } label: {
                Image(systemName: "xmark.circle.fill")
                    .font(.system(size: 24))
                    .foregroundStyle(EstormiColor.orSombre)
            }
            .accessibilityLabel("Close")
        }
        .padding(.horizontal, 18)
        .padding(.top, 20)
        .padding(.bottom, 14)
    }

    // MARK: - Body

    @ViewBuilder
    private var content: some View {
        if isLoading {
            Spacer()
            ProgressView()
                .tint(EstormiColor.orClair)
            Spacer()
        } else if let logText, !logText.isEmpty {
            formattedLog(logText)
        } else {
            Spacer()
            Text("This run's log is no longer available.")
                .font(EstormiTypeScale.bodySmall)
                .foregroundStyle(EstormiColor.parchemin.opacity(0.6))
                .multilineTextAlignment(.center)
                .padding(.horizontal, 32)
            Spacer()
        }
    }

    // Prettified log — same time · tag · message theme as the macOS Atelier.
    // Falls back to raw monospaced text if the format isn't recognised, so an
    // unexpected log shape is never swallowed.
    @ViewBuilder
    private func formattedLog(_ text: String) -> some View {
        let lines = EngineLogFormat.parse(text, markerRe: markerRe)
        ScrollView {
            if lines.isEmpty {
                Text(text)
                    .font(.system(size: 11.5, design: .monospaced))
                    .foregroundStyle(EstormiColor.parchemin.opacity(0.88))
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(16)
            } else {
                LazyVStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(lines.enumerated()), id: \.offset) { _, line in
                        PrettyLogRow(line: line)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.vertical, 12)
                .padding(.horizontal, 12)
            }
        }
        .textSelection(.enabled)
    }

    private var markerRe: NSRegularExpression {
        run.engine.lowercased() == "briefing"
            ? EngineLogFormat.briefingMarker : EngineLogFormat.sourceMarker
    }

    private func load() async {
        defer { isLoading = false }
        guard let id = run.logId, !id.isEmpty else { return }
        logText = await store.engineLog(id: id)
    }
}

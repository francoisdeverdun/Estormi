import SwiftUI

// Vault status card — folder + last-sync + the briefing count. Mirrors the
// macOS CardinalSection.

struct VaultStatusCard: View {
    @EnvironmentObject private var store: VaultStore

    var body: some View {
        GildedPanel(tone: .gold, cornerOrnaments: true) {
            VStack(alignment: .leading, spacing: 10) {
                Text("VAULT")
                    .font(EstormiFont.display(11, bold: true))
                    .tracking(3.4)
                    .foregroundStyle(EstormiColor.orSombre)
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    StatusDot(status: store.folderStatus)
                    Text(store.folderName ?? "No folder selected")
                        .font(EstormiTypeScale.h3)
                        .foregroundStyle(EstormiColor.parcheminOs)
                        .lineLimit(1)
                }
                if let generatedAt = store.manifest?.generatedAt {
                    Text("Last sync · \(EstormiDate.relative(generatedAt))")
                        .font(EstormiTypeScale.bodySmall)
                        .foregroundStyle(EstormiColor.parchemin.opacity(0.7))
                }
                LazyVGrid(
                    columns: [
                        GridItem(.flexible(), alignment: .leading),
                        GridItem(.flexible(), alignment: .leading),
                    ],
                    alignment: .leading,
                    spacing: 10
                ) {
                    Stat(label: "Chunks", value: totalChunks)
                    Stat(label: "Briefings", value: briefingsCount)
                }
                .padding(.top, 6)
                if let corpus = corpusSplit {
                    Text(corpus)
                        .font(EstormiTypeScale.micro)
                        .foregroundStyle(EstormiColor.parchemin.opacity(0.65))
                }
            }
        }
    }

    private var totalChunks: Int {
        store.metrics?.totalChunks
            ?? store.metrics?.bySource?.values.reduce(0, +)
            ?? 0
    }

    private var briefingsCount: Int {
        store.latestBriefingCounters?.briefingsTotal ?? store.briefingIndex.count
    }

    // "Personal 39,500 · World 8,710" — omitted entirely when no corpus
    // breakdown is present in the snapshot.
    private var corpusSplit: String? {
        guard let corpus = store.metrics?.corpus, !corpus.isEmpty else { return nil }
        let personal = corpus["personal"] ?? 0
        let world = corpus["world"] ?? 0
        var parts: [String] = []
        if personal > 0 { parts.append("Personal \(personal.formatted())") }
        if world > 0 { parts.append("World \(world.formatted())") }
        return parts.isEmpty ? nil : parts.joined(separator: "  ·  ")
    }
}

private struct StatusDot: View {
    let status: VaultFolderStatus
    var body: some View {
        Circle()
            .fill(color)
            .frame(width: 9, height: 9)
            .overlay(Circle().stroke(color.opacity(0.4), lineWidth: 4))
    }
    private var color: Color {
        switch status {
        case .ready: return EstormiColor.vertSauge
        case .stale: return EstormiColor.pourpre
        case .noFolder: return EstormiColor.orSombre
        }
    }
}

private struct Stat: View {
    let label: String
    let value: Int
    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("\(value)")
                .font(EstormiFont.display(20, bold: true))
                .foregroundStyle(EstormiColor.orClair)
            Text(label.uppercased())
                .font(EstormiFont.display(9, bold: true))
                .tracking(2)
                .foregroundStyle(EstormiColor.orSombre)
        }
    }
}


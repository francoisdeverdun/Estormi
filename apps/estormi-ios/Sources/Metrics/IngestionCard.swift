import SwiftUI

// Latest ingestion run — total chunks added + per-source breakdown.
// Mirrors the macOS SourcesPanel header, simplified to read-only.

struct IngestionCard: View {
    @EnvironmentObject private var store: VaultStore

    var body: some View {
        GildedPanel(tone: .burgundy) {
            VStack(alignment: .leading, spacing: 12) {
                HStack(alignment: .firstTextBaseline) {
                    Text("LATEST INGESTION")
                        .font(EstormiFont.display(11, bold: true))
                        .tracking(3.4)
                        .foregroundStyle(EstormiColor.orSombre)
                    Spacer()
                    if let ingestion = store.latestIngestion {
                        Text("+\(ingestion.chunksAdded) chunks")
                            .font(EstormiFont.display(13, bold: true))
                            .foregroundStyle(EstormiColor.orClair)
                    }
                }
                if let bySource = store.latestIngestion?.bySource, !bySource.isEmpty {
                    let sorted = bySource.sorted { $0.value > $1.value }
                    let maxN = max(1, sorted.first?.value ?? 1)
                    ForEach(sorted, id: \.key) { source, count in
                        HStack(spacing: 10) {
                            Text(source)
                                .font(EstormiTypeScale.bodySmall)
                                .foregroundStyle(EstormiColor.parcheminOs)
                                .frame(width: 100, alignment: .leading)
                            GeometryReader { geo in
                                ZStack(alignment: .leading) {
                                    Capsule().fill(EstormiColor.charbon3).frame(height: 6)
                                    Capsule()
                                        .fill(EstormiColor.capGradient)
                                        .frame(
                                            width: geo.size.width * CGFloat(count) / CGFloat(maxN),
                                            height: 6)
                                }
                            }
                            .frame(height: 6)
                            Text("\(count)")
                                .font(EstormiFont.display(11, bold: true))
                                .foregroundStyle(EstormiColor.orClair)
                                .frame(width: 36, alignment: .trailing)
                        }
                    }
                } else {
                    Text("No ingestion runs recorded yet.")
                        .font(EstormiTypeScale.bodySmall)
                        .foregroundStyle(EstormiColor.parchemin.opacity(0.6))
                }
            }
        }
    }
}

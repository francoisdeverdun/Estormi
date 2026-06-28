import SwiftUI

// A titled gilded panel wrapping one StackedAreaChart — the "memory · by
// source" (cumulative store) strip on the Metrics page. Mirrors the macOS
// MemoriaPulse hero (packages/web-ui/src/components/MemoriaPulse.tsx), header
// included.

struct TimeseriesCard: View {
    let eyebrow: String
    let timeseries: VaultTimeseries?
    var tone: GildedTone = .gold

    var body: some View {
        GildedPanel(tone: tone) {
            VStack(alignment: .leading, spacing: 12) {
                Text(eyebrow.uppercased())
                    .font(EstormiFont.display(11, bold: true))
                    .tracking(3.4)
                    .foregroundStyle(EstormiColor.orSombre)
                if let ts = timeseries, !ts.series.isEmpty {
                    StackedAreaChart(timeseries: ts)
                } else {
                    Text("No data recorded yet.")
                        .font(EstormiTypeScale.bodySmall)
                        .foregroundStyle(EstormiColor.parchemin.opacity(0.6))
                        .frame(maxWidth: .infinity, minHeight: 120, alignment: .center)
                }
            }
        }
    }
}

import SwiftUI

// Stacked-area chart in the macOS MemoriaPulse idiom (an *illuminated
// manuscript* strip): one colour band per source, gilt rulings at the
// quarters, a scribed contour stroke on each band's top edge, and a brass
// marker rod on scrub. The macOS original lives at
// `packages/web-ui/src/components/MemoriaPulse.tsx`; this is the native
// SwiftUI port. The legend doubles as a per-source show/hide toggle.

// Source-band palette, copied verbatim from MemoriaPulse so the same source
// keeps the same colour across both surfaces. Index is stable: a source's
// colour comes from its position in the dataset's full `sources` list. The hex
// values are the source of truth (matched to MemoriaPulse); no per-swatch
// token names here, so they can't drift out of sync with the web palette.
enum ChartPalette {
    static let colours: [Color] = [
        Color(hex: 0xC8A467),
        Color(hex: 0xA88A4F),
        Color(hex: 0x6B8A5F),
        Color(hex: 0x7D8FB3),
        Color(hex: 0xB05A6E),
        Color(hex: 0xD9B978),
        Color(hex: 0x8AA0A7),
        Color(hex: 0x9C7B5C),
        Color(hex: 0x5F7C8A),
        Color(hex: 0xA8765F),
        Color(hex: 0x7A8B6F),
        Color(hex: 0x6E6A8A),
    ]

    static func colour(_ index: Int) -> Color {
        guard index >= 0 else { return colours[0] }
        return colours[index % colours.count]
    }
}

struct StackedAreaChart: View {
    let timeseries: VaultTimeseries
    var height: CGFloat = 150

    @State private var hidden: Set<String> = []
    @State private var selected: Int?

    private var series: [VaultTimeseriesPoint] { timeseries.series }
    private var allSources: [String] { timeseries.sources }
    private var visibleSources: [String] { allSources.filter { !hidden.contains($0) } }

    var body: some View {
        let hasData = series.contains { point in
            visibleSources.contains { (point.bySource[$0] ?? 0) > 0 }
        }
        VStack(alignment: .leading, spacing: 10) {
            GeometryReader { geo in
                let w = geo.size.width
                let h = geo.size.height
                let cumulative = stack(visible: visibleSources)
                let maxY = max(1.0, cumulative.map { $0.last ?? 0 }.max() ?? 1)

                ZStack(alignment: .topLeading) {
                    Canvas { ctx, size in
                        drawRulings(ctx, size: size)
                        guard hasData else { return }
                        drawBands(
                            ctx, size: size, cumulative: cumulative, maxY: maxY)
                    }
                    if hasData, let sel = selected, series.indices.contains(sel) {
                        marker(x: xFor(sel, width: w), height: h)
                    }
                }
                .contentShape(Rectangle())
                // Simultaneous so a vertical drag still scrolls the page while
                // a horizontal scrub moves the marker; the selection clears on
                // lift.
                .simultaneousGesture(
                    DragGesture(minimumDistance: 0)
                        .onChanged { value in
                            guard series.count > 0 else { return }
                            let rel = max(0, min(1, value.location.x / max(1, w)))
                            selected = Int(
                                (rel * CGFloat(series.count - 1)).rounded())
                        }
                        .onEnded { _ in selected = nil }
                )
            }
            .frame(height: height)
            // The scrub breakdown floats over the top of the chart — above the
            // finger, not under it — so the reading isn't hidden by the touch.
            // The legend stays pinned below as a permanent show/hide control.
            .overlay(alignment: .top) {
                if hasData, let sel = selected, series.indices.contains(sel) {
                    breakdown(for: series[sel])
                        .padding(10)
                        .background(
                            RoundedRectangle(cornerRadius: 6, style: .continuous)
                                .fill(EstormiColor.charbon.opacity(0.92))
                                .overlay(
                                    RoundedRectangle(cornerRadius: 6, style: .continuous)
                                        .stroke(EstormiColor.orAncien.opacity(0.35), lineWidth: 0.6)
                                )
                        )
                        .padding(.horizontal, 4)
                        .allowsHitTesting(false)
                }
            }

            legend
        }
    }

    // MARK: - Geometry

    // Per-day cumulative stack over the visible sources (inner index follows
    // `visibleSources` order).
    private func stack(visible: [String]) -> [[Double]] {
        series.map { point in
            var acc = 0.0
            return visible.map { src in
                acc += Double(point.bySource[src] ?? 0)
                return acc
            }
        }
    }

    private func xFor(_ day: Int, width: CGFloat) -> CGFloat {
        series.count <= 1 ? width / 2 : width * CGFloat(day) / CGFloat(series.count - 1)
    }

    private func yFor(_ value: Double, maxY: Double, height: CGFloat) -> CGFloat {
        let usable = height - 2
        return usable - CGFloat(value / maxY) * usable + 1
    }

    // MARK: - Canvas drawing

    private func drawRulings(_ ctx: GraphicsContext, size: CGSize) {
        for frac in [0.25, 0.5, 0.75] {
            let y = size.height * frac
            var line = Path()
            line.move(to: CGPoint(x: 0, y: y))
            line.addLine(to: CGPoint(x: size.width, y: y))
            ctx.stroke(
                line, with: .color(EstormiColor.orAncien.opacity(0.22)),
                lineWidth: 0.5)
        }
    }

    private func drawBands(
        _ ctx: GraphicsContext, size: CGSize, cumulative: [[Double]], maxY: Double
    ) {
        let h = size.height
        let w = size.width
        // Hachure first, then contours, so every scribed top edge reads above
        // the adjacent fill regardless of stack order. Each band is a faint
        // wash overlaid with hand-drawn diagonal hatching in the source colour
        // — an illuminated-manuscript pen idiom rather than a flat fill.
        for (si, src) in visibleSources.enumerated() {
            let colour = ChartPalette.colour(allSources.firstIndex(of: src) ?? si)
            let band = bandPath(si, cumulative: cumulative, maxY: maxY, size: size)
            ctx.fill(band, with: .color(colour.opacity(0.12)))
            hatch(ctx, size: size, clip: band, colour: colour)
        }
        for (si, src) in visibleSources.enumerated() {
            let colour = ChartPalette.colour(allSources.firstIndex(of: src) ?? si)
            var top = Path()
            for di in series.indices {
                let pt = CGPoint(
                    x: xFor(di, width: w),
                    y: yFor(cumulative[di][si], maxY: maxY, height: h))
                di == 0 ? top.move(to: pt) : top.addLine(to: pt)
            }
            ctx.stroke(
                top, with: .color(colour.opacity(0.95)),
                style: StrokeStyle(lineWidth: 1.1, lineCap: .round, lineJoin: .round))
        }
    }

    // Closed polygon for one stacked band: along its top edge, then back along
    // the band below it (or the baseline for the first band).
    private func bandPath(
        _ si: Int, cumulative: [[Double]], maxY: Double, size: CGSize
    ) -> Path {
        let h = size.height
        let w = size.width
        var band = Path()
        for di in series.indices {
            let pt = CGPoint(
                x: xFor(di, width: w),
                y: yFor(cumulative[di][si], maxY: maxY, height: h))
            di == 0 ? band.move(to: pt) : band.addLine(to: pt)
        }
        for di in series.indices.reversed() {
            let yb =
                si == 0
                ? yFor(0, maxY: maxY, height: h)
                : yFor(cumulative[di][si - 1], maxY: maxY, height: h)
            band.addLine(to: CGPoint(x: xFor(di, width: w), y: yb))
        }
        band.closeSubpath()
        return band
    }

    // Fill a band with 45° pen hatching clipped to its outline. The clip is
    // applied to a copy of the context so it stays local to this band.
    private func hatch(
        _ ctx: GraphicsContext, size: CGSize, clip: Path, colour: Color
    ) {
        var c = ctx
        c.clip(to: clip)
        let spacing: CGFloat = 6
        var hatching = Path()
        var x = -size.height
        while x < size.width {
            hatching.move(to: CGPoint(x: x, y: size.height))
            hatching.addLine(to: CGPoint(x: x + size.height, y: 0))
            x += spacing
        }
        c.stroke(hatching, with: .color(colour.opacity(0.55)), lineWidth: 0.8)
    }

    // Brass marker rod with a diamond cap — replaces the macOS crosshair.
    private func marker(x: CGFloat, height: CGFloat) -> some View {
        ZStack(alignment: .top) {
            Rectangle()
                .fill(EstormiColor.orClair.opacity(0.85))
                .frame(width: 1, height: height)
            Rectangle()
                .fill(EstormiColor.orClair)
                .frame(width: 6, height: 6)
                .rotationEffect(.degrees(45))
                .overlay(
                    Rectangle()
                        .stroke(EstormiColor.orAncien, lineWidth: 0.6)
                        .rotationEffect(.degrees(45))
                        .frame(width: 6, height: 6)
                )
                .offset(y: -3)
        }
        .frame(width: 6, alignment: .top)
        .position(x: x, y: height / 2)
        .allowsHitTesting(false)
    }

    // MARK: - Legend + scrub breakdown

    private var legend: some View {
        FlowLayout(spacing: 8) {
            ForEach(Array(allSources.enumerated()), id: \.element) { index, src in
                let isHidden = hidden.contains(src)
                Button {
                    if isHidden { hidden.remove(src) } else { hidden.insert(src) }
                } label: {
                    HStack(spacing: 5) {
                        Rectangle()
                            .fill(ChartPalette.colour(index))
                            .frame(width: 7, height: 7)
                            .rotationEffect(.degrees(45))
                            .opacity(isHidden ? 0.35 : 1)
                        Text(src.uppercased())
                            .font(EstormiFont.display(10, bold: false))
                            .tracking(1.4)
                            .strikethrough(isHidden, color: EstormiColor.orSombre)
                            .foregroundStyle(
                                isHidden
                                    ? EstormiColor.parchemin.opacity(0.4)
                                    : EstormiColor.parcheminOs)
                    }
                }
                .buttonStyle(.plain)
            }
        }
    }

    // On scrub, the legend is replaced by the selected day's per-source
    // breakdown (visible sources, busiest first) with a plain-numeral date.
    private func breakdown(for point: VaultTimeseriesPoint) -> some View {
        let rows =
            visibleSources
            .map { (name: $0, n: point.bySource[$0] ?? 0) }
            .filter { $0.n > 0 }
            .sorted { $0.n > $1.n }
        return VStack(alignment: .leading, spacing: 3) {
            HStack {
                Text(Self.folioDate(point.day))
                    .font(EstormiFont.display(10, bold: true))
                    .tracking(2)
                    .foregroundStyle(EstormiColor.orClair)
                Spacer()
                Text("\(rows.reduce(0) { $0 + $1.n })")
                    .font(EstormiFont.display(11, bold: true))
                    .foregroundStyle(EstormiColor.orClair)
            }
            if rows.isEmpty {
                Text("no activity")
                    .font(EstormiTypeScale.micro)
                    .foregroundStyle(EstormiColor.parchemin.opacity(0.5))
            } else {
                ForEach(rows, id: \.name) { row in
                    HStack(spacing: 6) {
                        Rectangle()
                            .fill(ChartPalette.colour(allSources.firstIndex(of: row.name) ?? 0))
                            .frame(width: 6, height: 6)
                            .rotationEffect(.degrees(45))
                        Text(row.name)
                            .font(EstormiTypeScale.micro)
                            .foregroundStyle(EstormiColor.parcheminOs)
                        Spacer()
                        Text("\(row.n)")
                            .font(EstormiFont.body(12))
                            .foregroundStyle(EstormiColor.parchemin.opacity(0.8))
                    }
                }
            }
        }
        .animation(nil, value: point.day)
    }

    // Folio date in natural form: `2026-05-31` → `31 May 2026`.
    // Falls back to the input on parse failure.
    private static let months = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ]
    private static func folioDate(_ iso: String) -> String {
        let parts = iso.split(separator: "-")
        guard parts.count == 3,
            let d = Int(parts[2]), let m = Int(parts[1]), let y = Int(parts[0]),
            (1...12).contains(m)
        else { return iso }
        return "\(d) \(months[m - 1]) \(y)"
    }
}

// Minimal wrap-flow layout for the legend chips (SwiftUI Layout, iOS 16+).
struct FlowLayout: Layout {
    var spacing: CGFloat = 8

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let maxWidth = proposal.width ?? .infinity
        var x: CGFloat = 0
        var y: CGFloat = 0
        var rowHeight: CGFloat = 0
        for view in subviews {
            let size = view.sizeThatFits(.unspecified)
            if x + size.width > maxWidth, x > 0 {
                x = 0
                y += rowHeight + spacing
                rowHeight = 0
            }
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
        }
        return CGSize(width: maxWidth == .infinity ? x : maxWidth, height: y + rowHeight)
    }

    func placeSubviews(
        in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()
    ) {
        var x = bounds.minX
        var y = bounds.minY
        var rowHeight: CGFloat = 0
        for view in subviews {
            let size = view.sizeThatFits(.unspecified)
            if x + size.width > bounds.maxX, x > bounds.minX {
                x = bounds.minX
                y += rowHeight + spacing
                rowHeight = 0
            }
            view.place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(size))
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
        }
    }
}

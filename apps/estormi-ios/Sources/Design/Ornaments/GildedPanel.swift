import SwiftUI

// Gilded glass panel — Estormi's signature card surface. Uses iOS 26 Liquid
// Glass for the substrate, then layers a gold hairline border and optional
// corner flourishes — the manuscript card frame.

enum GildedTone {
    case gold
    case burgundy
    case neutral

    // Most panels keep a gilt (gold) hairline frame; the tone varies the faint
    // ground wash beneath it. Burgundy is the house ground colour.
    var stroke: Color {
        switch self {
        case .gold: return EstormiColor.orAncien
        case .burgundy: return EstormiColor.orAncien
        case .neutral: return EstormiColor.parchemin.opacity(0.18)
        }
    }

    var tint: Color {
        switch self {
        case .gold: return EstormiColor.orAncien.opacity(0.06)
        case .burgundy: return EstormiBrand.burgundy.opacity(0.20)
        case .neutral: return Color.clear
        }
    }
}

struct GildedPanel<Content: View>: View {
    var tone: GildedTone = .gold
    var cornerOrnaments: Bool = false
    @ViewBuilder var content: () -> Content

    var body: some View {
        contentBody
            .padding(16)
            .background(
                ZStack {
                    RoundedRectangle(
                        cornerRadius: EstormiMetric.radiusPanel, style: .continuous
                    )
                    .fill(tone.tint)
                    if cornerOrnaments {
                        flourishOverlay
                    }
                }
            )
            .glassEffect(
                in: RoundedRectangle(
                    cornerRadius: EstormiMetric.radiusPanel, style: .continuous)
            )
            .overlay(
                RoundedRectangle(
                    cornerRadius: EstormiMetric.radiusPanel, style: .continuous
                )
                .stroke(tone.stroke, lineWidth: 0.6)
            )
    }

    private var contentBody: some View {
        content()
    }

    private var flourishOverlay: some View {
        GeometryReader { geo in
            let s = min(geo.size.width, geo.size.height) * 0.18
            ZStack(alignment: .topLeading) {
                CornerFlourish(size: s, color: tone.stroke)
                    .position(x: s / 2, y: s / 2)
                CornerFlourish(size: s, color: tone.stroke)
                    .rotationEffect(.degrees(90))
                    .position(x: geo.size.width - s / 2, y: s / 2)
                CornerFlourish(size: s, color: tone.stroke)
                    .rotationEffect(.degrees(270))
                    .position(x: s / 2, y: geo.size.height - s / 2)
                CornerFlourish(size: s, color: tone.stroke)
                    .rotationEffect(.degrees(180))
                    .position(
                        x: geo.size.width - s / 2, y: geo.size.height - s / 2)
            }
        }
        .allowsHitTesting(false)
        .opacity(0.7)
    }
}

#Preview {
    ZStack {
        EstormiColor.charbon.ignoresSafeArea()
        VStack(spacing: 16) {
            GildedPanel(
                tone: .gold, cornerOrnaments: true
            ) {
                Text("Ars Memoriae")
                    .font(EstormiTypeScale.h3)
                    .foregroundStyle(EstormiColor.parchemin)
            }
            GildedPanel(tone: .burgundy) {
                Text("Burgundy tone")
                    .font(EstormiTypeScale.bodyLarge)
                    .foregroundStyle(EstormiColor.parchemin)
            }
        }
        .padding(24)
    }
}

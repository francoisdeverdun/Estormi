import SwiftUI

// Estormi brand marks. The logo is a blocked illuminated initial: a coloured
// ground (burgundy by default), one heavy gold border, and the gold Cinzel
// majuscule — a bold, modern take on a manuscript lettrine that holds up at any
// size. Used in the Briefings masthead and as the app-icon artwork.

// Brand ground colours. Deliberately NOT the app's charbon background — the
// logo should sit on top of it, not melt into it. Gold-on-burgundy is the
// house pairing.
enum EstormiBrand {
    static let burgundy = Color(hex: 0x6E1F2E)
    static let burgundyBright = Color(hex: 0x842A41)
}

// MARK: - Logo mark (the illuminated initial)

struct EstormiLogoMark: View {
    var letter: String = "E"
    var size: CGFloat = 64
    var background: Color = EstormiBrand.burgundy

    var body: some View {
        let r = size * 0.20
        ZStack {
            RoundedRectangle(cornerRadius: r, style: .continuous)
                .fill(background)
            RoundedRectangle(cornerRadius: r, style: .continuous)
                .inset(by: size * 0.05)
                .stroke(EstormiColor.capGradient, lineWidth: max(1.5, size * 0.055))
            Text(letter)
                .font(EstormiFont.display(size * 0.6, bold: true))
                .foregroundStyle(EstormiColor.capGradient)
        }
        .frame(width: size, height: size)
        .accessibilityLabel(letter)
    }
}

// MARK: - Masthead (wordmark + tagline + garland)

// The brand lockup used at the top of the Briefings page: the illuminated `E`
// flows straight into "stormi", with "Ars Memoriae" garlanded beneath.
struct EstormiMasthead: View {
    var markSize: CGFloat = 54
    var background: Color = EstormiBrand.burgundy

    var body: some View {
        VStack(spacing: 8) {
            // A real square initial (the E stays centred, uncrushed) set off
            // from the wordmark by a deliberate space — gilded device + word,
            // a masthead lockup rather than a single run-together word.
            HStack(alignment: .center, spacing: markSize * 0.16) {
                EstormiLogoMark(size: markSize, background: background)
                Text("STORMI")
                    .font(EstormiFont.display(markSize * 0.62, bold: true))
                    .tracking(2)
                    .foregroundStyle(EstormiColor.parcheminOs)
            }
            HStack(spacing: 10) {
                Fleuron(size: 10, color: EstormiColor.orSombre)
                Text("ARS MEMORIAE")
                    .font(EstormiFont.display(11, bold: true))
                    .tracking(5)
                    .foregroundStyle(EstormiColor.orClair)
                Fleuron(size: 10, color: EstormiColor.orSombre)
            }
            IlluminatedRule()
                .padding(.horizontal, 46)
                .padding(.top, 2)
        }
        .frame(maxWidth: .infinity)
    }
}

// MARK: - Illuminated rule

// A minimalist gilt divider: a hairline that fades at both ends, a centred
// burgundy lozenge in a gold frame, and two small symmetric diamond accents.
// The modern-minimal reading of a manuscript rule — geometry and air instead of
// a crowded vine.
struct IlluminatedRule: View {
    var gold: Color = EstormiColor.orAncien
    var goldBright: Color = EstormiColor.orClair
    var accent: Color = EstormiBrand.burgundy
    var height: CGFloat = 18

    var body: some View {
        Canvas { ctx, size in
            let w = size.width
            let cy = size.height / 2
            let cx = w / 2

            func diamond(_ c: CGPoint, _ r: CGFloat) -> Path {
                var p = Path()
                p.move(to: CGPoint(x: c.x, y: c.y - r))
                p.addLine(to: CGPoint(x: c.x + r, y: c.y))
                p.addLine(to: CGPoint(x: c.x, y: c.y + r))
                p.addLine(to: CGPoint(x: c.x - r, y: c.y))
                p.closeSubpath()
                return p
            }
            func dot(_ c: CGPoint, _ r: CGFloat, _ col: Color) {
                ctx.fill(Path(ellipseIn: CGRect(x: c.x - r, y: c.y - r, width: r * 2, height: r * 2)),
                         with: .color(col))
            }
            // A small modern quatrefoil: four gold petals, burgundy heart.
            func flower(_ c: CGPoint, _ petal: CGFloat) {
                let off = petal * 1.5
                for d in [CGPoint(x: 0, y: -off), CGPoint(x: off, y: 0),
                          CGPoint(x: 0, y: off), CGPoint(x: -off, y: 0)] {
                    dot(CGPoint(x: c.x + d.x, y: c.y + d.y), petal, gold)
                }
                dot(c, petal * 0.85, accent)
            }

            // Hairline, fading symmetrically to nothing at both ends.
            var line = Path()
            line.move(to: CGPoint(x: 0, y: cy))
            line.addLine(to: CGPoint(x: w, y: cy))
            ctx.stroke(
                line,
                with: .linearGradient(
                    Gradient(stops: [
                        .init(color: gold.opacity(0), location: 0.0),
                        .init(color: gold.opacity(0.85), location: 0.24),
                        .init(color: gold.opacity(0.85), location: 0.76),
                        .init(color: gold.opacity(0), location: 1.0),
                    ]),
                    startPoint: CGPoint(x: 0, y: cy),
                    endPoint: CGPoint(x: w, y: cy)),
                style: StrokeStyle(lineWidth: 0.7))

            // Outboard rhythm: a small gold lozenge with a faint pip beyond it.
            let outer = w * 0.26
            for sx in [cx - outer, cx + outer] {
                ctx.fill(diamond(CGPoint(x: sx, y: cy), 2.0), with: .color(gold))
                dot(CGPoint(x: sx + (sx < cx ? -6 : 6), y: cy), 0.9, gold.opacity(0.7))
            }

            // Quatrefoil flowers flanking the centre.
            let inner = w * 0.13
            for sx in [cx - inner, cx + inner] {
                flower(CGPoint(x: sx, y: cy), 1.7)
            }

            // Centre: a small burgundy lozenge in a thin gold frame, gold heart.
            let r: CGFloat = min(height * 0.34, 5.5)
            let c = CGPoint(x: cx, y: cy)
            ctx.fill(diamond(c, r), with: .color(accent))
            ctx.stroke(diamond(c, r), with: .color(gold), style: StrokeStyle(lineWidth: 0.9))
            dot(c, 1.1, goldBright)
        }
        .frame(height: height)
        .accessibilityHidden(true)
    }
}

// MARK: - App icon artwork

// Full-bleed icon artwork — the burgundy ground fills the whole square (iOS
// applies the squircle mask), a gold keyline sits inside the safe area, and the
// gold majuscule is centred. Rendered here for preview; the shipped asset is a
// flattened export of this at 1024².
struct EstormiIconArtwork: View {
    var size: CGFloat = 1024
    var background: Color = EstormiBrand.burgundy

    // iOS masks the icon to a squircle of this corner ratio; the gilt frame is
    // drawn concentric to it so it parallels the icon edge instead of floating.
    private let iconRadiusRatio: CGFloat = 0.2237

    var body: some View {
        // The gilt frame's outer edge must be the EXACT iOS icon silhouette, not
        // an approximated RoundedRectangle fighting Apple's real squircle mask.
        // So we fill the whole tile with gold and lay a burgundy panel on top,
        // inset by the border width: the iOS mask trims the gold, making its
        // outer contour the true icon shape, and the gold border is whatever
        // shows between the mask and the burgundy panel.
        let border = size * 0.052
        let innerRadius = max(0, size * iconRadiusRatio - border)
        ZStack {
            EstormiColor.capGradient  // gold, full bleed → becomes the frame

            RoundedRectangle(cornerRadius: innerRadius, style: .continuous)
                .fill(background)
                .overlay {
                    RadialGradient(
                        colors: [.white.opacity(0.06), .clear, .black.opacity(0.22)],
                        center: .center, startRadius: size * 0.08, endRadius: size * 0.66
                    )
                    .clipShape(RoundedRectangle(cornerRadius: innerRadius, style: .continuous))
                }
                .padding(border)

            Text("E")
                .font(EstormiFont.display(size * 0.68, bold: true))
                .foregroundStyle(EstormiColor.capGradient)
                .shadow(color: .black.opacity(0.3), radius: size * 0.008, x: 0, y: size * 0.006)
                // Cinzel caps have no descender, so the glyph sits high in its
                // text box — nudge down so the glyph's bounding box centres in
                // the tile (measured: ~0.058 lands the cap centre on the axis).
                .offset(y: size * 0.058)
        }
        .frame(width: size, height: size)
    }
}

#if DEBUG
// Preview wrapper — same artwork under the iOS squircle mask, for showing the
// icon inside the app. DEBUG-only: used solely by BrandPreview below; the
// shipped icon is the flattened AppIcon.appiconset asset.
struct EstormiAppIcon: View {
    var size: CGFloat = 180
    var background: Color = EstormiBrand.burgundy

    var body: some View {
        EstormiIconArtwork(size: size, background: background)
            .clipShape(RoundedRectangle(cornerRadius: size * 0.2237, style: .continuous))
    }
}

import UIKit
// Renders the icon artwork to a 1024² PNG in the app's Documents folder when
// launched with `-EstormiExportIcon YES`. We pull it out via
// `simctl get_app_container` and drop it into AppIcon.appiconset. DEBUG-only.
@MainActor
enum IconExporter {
    static func exportIfRequested() {
        guard UserDefaults.standard.bool(forKey: "EstormiExportIcon") else { return }
        let renderer = ImageRenderer(content: EstormiIconArtwork(size: 1024))
        renderer.scale = 1
        guard let image = renderer.uiImage,
            let docs = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first
        else { return }
        // ImageRenderer always emits an alpha channel, but App Store validation
        // rejects an app icon that carries one (ITMS-90717 "Invalid App Store
        // Icon"). Flatten onto an opaque context so the exported PNG is RGB with
        // no alpha — otherwise a regenerated icon would fail upload again.
        let format = UIGraphicsImageRendererFormat()
        format.opaque = true
        format.scale = 1
        let flattened = UIGraphicsImageRenderer(
            size: CGSize(width: 1024, height: 1024), format: format
        ).image { _ in
            image.draw(in: CGRect(x: 0, y: 0, width: 1024, height: 1024))
        }
        guard let data = flattened.pngData() else { return }
        let url = docs.appendingPathComponent("AppIcon-1024.png")
        try? data.write(to: url)
        print("ESTORMI_ICON_EXPORT \(url.path)")
    }
}
#endif

// MARK: - DEBUG brand preview

#if DEBUG
struct BrandPreview: View {
    private let burgundy = EstormiBrand.burgundy
    private let burgundyBright = EstormiBrand.burgundyBright
    private let lapis = EstormiColor.enluminure

    var body: some View {
        ScrollView {
            VStack(spacing: 28) {
                section("MASTHEAD")
                EstormiMasthead(background: burgundy)

                section("GROUND COLOUR OPTIONS")
                HStack(spacing: 22) {
                    swatch("Burgundy", EstormiLogoMark(size: 72, background: burgundy))
                    swatch("Burgundy +", EstormiLogoMark(size: 72, background: burgundyBright))
                    swatch("Lapis", EstormiLogoMark(size: 72, background: lapis))
                }

                section("APP ICON")
                HStack(spacing: 22) {
                    EstormiAppIcon(size: 120, background: burgundy)
                    EstormiAppIcon(size: 120, background: burgundyBright)
                }

                section("ON A HOME SCREEN")
                HStack(alignment: .top, spacing: 26) {
                    iconStack(180)
                    iconStack(120)
                    iconStack(80)
                    iconStack(60)
                }
            }
            .padding(.horizontal, 20)
            .padding(.vertical, 40)
            .frame(maxWidth: .infinity)
        }
        .background(EstormiColor.charbon.ignoresSafeArea())
    }

    private func section(_ t: String) -> some View {
        Text(t)
            .font(EstormiFont.display(11, bold: true))
            .tracking(4)
            .foregroundStyle(EstormiColor.orClair)
    }

    private func swatch(_ label: String, _ mark: EstormiLogoMark) -> some View {
        VStack(spacing: 8) {
            mark
            Text(label)
                .font(EstormiFont.body(11))
                .foregroundStyle(EstormiColor.parchemin.opacity(0.7))
        }
    }

    private func iconStack(_ s: CGFloat) -> some View {
        VStack(spacing: 6) {
            EstormiAppIcon(size: s)
            Text("\(Int(s))")
                .font(EstormiFont.body(10))
                .foregroundStyle(EstormiColor.parchemin.opacity(0.5))
        }
    }
}
#endif

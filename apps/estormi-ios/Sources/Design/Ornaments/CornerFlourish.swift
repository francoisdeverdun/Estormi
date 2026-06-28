import SwiftUI

// L-bracket corner ornament — double-line frame with three small flower marks
// along the curl. Estormi iOS only — no web-ui counterpart. The organic curve
// is approximated with a quad-curve sweep to keep the path under a screen's
// worth of code.

struct CornerFlourish: View {
    var size: CGFloat = 80
    var color: Color = EstormiColor.orAncien

    var body: some View {
        Canvas { ctx, canvasSize in
            let w = canvasSize.width
            let h = canvasSize.height
            let stroke = w / 80

            // Double-line L-bracket (outer 1px, inner 0.4px at 80pt baseline).
            var outer = Path()
            outer.move(to: CGPoint(x: 0, y: h * 0.85))
            outer.addLine(to: CGPoint(x: 0, y: 0))
            outer.addLine(to: CGPoint(x: w * 0.85, y: 0))
            ctx.stroke(
                outer,
                with: .color(color),
                style: StrokeStyle(lineWidth: stroke, lineCap: .round))

            var inner = Path()
            let inset: CGFloat = w * 0.08
            inner.move(to: CGPoint(x: inset, y: h * 0.75))
            inner.addLine(to: CGPoint(x: inset, y: inset))
            inner.addLine(to: CGPoint(x: w * 0.75, y: inset))
            ctx.stroke(
                inner,
                with: .color(color.opacity(0.55)),
                style: StrokeStyle(lineWidth: stroke * 0.4, lineCap: .round))

            // Curling vine — quad curve from inner corner sweeping outward.
            var vine = Path()
            vine.move(to: CGPoint(x: inset, y: h * 0.55))
            vine.addQuadCurve(
                to: CGPoint(x: w * 0.55, y: inset),
                control: CGPoint(x: w * 0.30, y: h * 0.30))
            ctx.stroke(
                vine,
                with: .color(color),
                style: StrokeStyle(lineWidth: stroke * 0.6, lineCap: .round))

            // Three small flowers along the vine (red / blue / gold) — match
            // the macOS macro palette.
            let blossoms: [(CGPoint, Color)] = [
                (CGPoint(x: inset, y: h * 0.55), Color(hex: 0xA84A3A)),
                (CGPoint(x: w * 0.32, y: h * 0.32), Color(hex: 0x2E4C7E)),
                (CGPoint(x: w * 0.55, y: inset), Color(hex: 0xE2C58A)),
            ]
            for (point, fill) in blossoms {
                let r = w * 0.045
                let rect = CGRect(
                    x: point.x - r, y: point.y - r, width: r * 2, height: r * 2)
                ctx.fill(Path(ellipseIn: rect), with: .color(fill))
            }
        }
        .frame(width: size, height: size)
    }
}

#Preview {
    ZStack {
        EstormiColor.charbon.ignoresSafeArea()
        CornerFlourish(size: 96)
    }
}

import SwiftUI

// 4-petal rosette ornament — rotated ellipses around a central disc. Ported
// from packages/ui-kit/src/components/marks.tsx (the Fleuron export).

struct Fleuron: View {
    var size: CGFloat = 24
    var color: Color = EstormiColor.orAncien
    var opacity: Double = 1.0

    var body: some View {
        Canvas { ctx, canvasSize in
            let cx = canvasSize.width / 2
            let cy = canvasSize.height / 2
            let petalW = canvasSize.width * 0.46
            let petalH = canvasSize.height * 0.18
            for i in 0..<4 {
                let angle = Double(i) * .pi / 2
                ctx.drawLayer { layer in
                    layer.translateBy(x: cx, y: cy)
                    layer.rotate(by: .radians(angle))
                    let rect = CGRect(
                        x: -petalW / 2, y: -petalH / 2, width: petalW, height: petalH)
                    layer.fill(Path(ellipseIn: rect), with: .color(color.opacity(opacity)))
                }
            }
            let centerR = canvasSize.width * 0.10
            let centerRect = CGRect(
                x: cx - centerR, y: cy - centerR, width: centerR * 2, height: centerR * 2)
            ctx.fill(Path(ellipseIn: centerRect), with: .color(color.opacity(opacity)))
        }
        .frame(width: size, height: size)
    }
}

#Preview {
    HStack(spacing: 24) {
        Fleuron(size: 24)
        Fleuron(size: 48, color: EstormiColor.pourpre)
        Fleuron(size: 64, color: EstormiColor.enluminure, opacity: 0.7)
    }
    .padding(24)
    .background(EstormiColor.charbon)
}

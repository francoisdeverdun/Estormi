import SwiftUI

// GENERATED FROM tokens.css — DO NOT EDIT BY HAND. Run `make tokens`.
//
// The EstormiColor solid-color members below are generated from the canonical
// palette in packages/ui-kit/src/tokens.css by packages/ui-kit/gen_tokens_swift.py.
// Colors are the exact hex values used by the macOS app's CSS variables so the
// briefing renders identically on both surfaces. The capGradient, EstormiMetric,
// and the Color(hex:) extension are hand-maintained and live in this file too.

enum EstormiColor {
    // Neutrals
    static let charbon = Color(hex: 0x1A1F29)
    static let charbon3 = Color(hex: 0x2E3441)
    static let parchemin = Color(hex: 0xF5F1E8)
    static let parcheminOs = Color(hex: 0xFAF8F4)

    // Gold accent family
    static let orAncien = Color(hex: 0xC8A96B)
    static let orClair = Color(hex: 0xDCBA8A)
    static let orSombre = Color(hex: 0x8A7142)

    // Semantic accents
    static let pourpre = Color(hex: 0xB82E2E)
    static let pourpreClair = Color(hex: 0xB83A57)
    static let rougeClair = Color(hex: 0xD97B7B)
    static let enluminure = Color(hex: 0x1E3A5F)
    static let enluminureClair = Color(hex: 0x4264BA)
    static let vertSauge = Color(hex: 0x6B8A5F)

    // Gold gradient used by the IlluminatedCap letter fill.
    static let capGradient = LinearGradient(
        stops: [
            .init(color: Color(hex: 0xFFE9B8), location: 0.0),
            .init(color: Color(hex: 0xDCBA8A), location: 0.40),
            .init(color: Color(hex: 0xC8A96B), location: 0.75),
            .init(color: Color(hex: 0x8A6A30), location: 1.0),
        ],
        startPoint: .top,
        endPoint: .bottom
    )
}

enum EstormiMetric {
    static let radiusPanel: CGFloat = 12
    static let radiusTight: CGFloat = 4

    enum Motion {
        static let fast: TimeInterval = 0.14
        static let medium: TimeInterval = 0.24
    }
}

extension Color {
    init(hex: UInt32, alpha: Double = 1.0) {
        let r = Double((hex >> 16) & 0xFF) / 255.0
        let g = Double((hex >> 8) & 0xFF) / 255.0
        let b = Double(hex & 0xFF) / 255.0
        self.init(.sRGB, red: r, green: g, blue: b, opacity: alpha)
    }
}

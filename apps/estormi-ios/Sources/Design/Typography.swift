import os
import SwiftUI
import UIKit

// Cinzel (display) + EB Garamond (body) ship as variable fonts:
//   Cinzel-Regular.ttf       PostScript "Cinzel-Regular"     (weight axis)
//   EBGaramond-Regular.ttf   PostScript "EBGaramond-Regular" (weight axis)
//   EBGaramond-Italic.ttf    PostScript "EBGaramond-Italic"  (weight axis)
//
// Bold variants come from applying `.weight(.bold)` to the variable axis,
// not a second file. If the .ttf failed to register we fall back to system
// serif so the app always renders.

enum EstormiFont {
    // `relativeTo:` anchors the custom font to a Dynamic Type text style so it
    // tracks the system text-size setting. Without it, `.custom(_:size:)` is
    // frozen: the briefing body (which scales via UIFontMetrics in
    // BriefingHTMLView) would grow while the native chrome around it — masthead,
    // date strip, audio bar, metrics, settings — stayed at fixed point sizes.
    // Defaulting to `.body` means every existing call site scales unchanged;
    // the larger roles pass a matching style for a natural scaling curve.
    static func display(
        _ size: CGFloat, bold: Bool = false, relativeTo textStyle: Font.TextStyle = .body
    ) -> Font {
        if FontAvailability.isRegistered("Cinzel-Regular") {
            let base = Font.custom("Cinzel-Regular", size: size, relativeTo: textStyle)
            return bold ? base.weight(.bold) : base
        }
        return .system(size: size, weight: bold ? .bold : .regular, design: .serif)
    }

    static func body(
        _ size: CGFloat, italic: Bool = false, relativeTo textStyle: Font.TextStyle = .body
    ) -> Font {
        let custom = italic ? "EBGaramond-Italic" : "EBGaramond-Regular"
        if FontAvailability.isRegistered(custom) {
            return .custom(custom, size: size, relativeTo: textStyle)
        }
        if italic {
            return .system(size: size, weight: .regular, design: .serif).italic()
        }
        return .system(size: size, weight: .regular, design: .serif)
    }
}

// Typography scale mirrors packages/ui-kit/src/tokens.css. Each role anchors to
// the Dynamic Type style closest to its size so it scales naturally.
enum EstormiTypeScale {
    static let h2 = EstormiFont.display(34, bold: true, relativeTo: .largeTitle)
    static let h3 = EstormiFont.display(22, bold: true, relativeTo: .title2)
    static let bodyLarge = EstormiFont.body(16, relativeTo: .body)
    static let bodySmall = EstormiFont.body(14, relativeTo: .callout)
    static let micro = EstormiFont.body(12, relativeTo: .caption)
}

// CTFontCreateWithName silently substitutes the system font for missing
// families, so we ask UIFont up front whether a name actually resolves.
private enum FontAvailability {
    private static let cache = OSAllocatedUnfairLock(initialState: [String: Bool]())

    static func isRegistered(_ name: String) -> Bool {
        cache.withLock { dict in
            if let hit = dict[name] { return hit }
            let resolved = UIFont(name: name, size: 12) != nil
            dict[name] = resolved
            return resolved
        }
    }
}

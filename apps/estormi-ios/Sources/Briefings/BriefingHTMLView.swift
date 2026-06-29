import SwiftUI
import WebKit

// Renders the briefing's htmlBody in a WKWebView with injected CSS that
// matches the macOS modal styling: charbon background, parchemin text,
// EB Garamond serif body, Cinzel headings, gold rules + drop caps.
//
// Why WKWebView and not AttributedString: the Mac writes structured HTML
// (h1/h2/blockquote/source spans, the `→ Impact : …` markers) and the macOS
// modal already styles it with CSS. Rebuilding that styling in
// AttributedString would mean re-implementing the cascade. WKWebView lets us
// reuse the exact CSS tokens.

/// The pure navigation-policy decision for the briefing WKWebView, factored out
/// of the `WKNavigationDelegate` so it can be unit-tested without UIKit/WebKit
/// delegate plumbing (the same way the Rust shell extracted `token_ok`). The
/// briefing body is untrusted LLM-generated HTML, so every navigation is denied
/// except our own in-memory load; a web URL is opened in the system browser
/// only when the user explicitly tapped a link.
enum BriefingNavigationPolicy {
    enum Decision: Equatable {
        /// Allow the navigation to proceed in the webview.
        case allow
        /// Hand this URL to the system browser, then cancel the in-webview load.
        case openExternally(URL)
        /// Deny the navigation outright.
        case cancel
    }

    static func decide(navigationType: WKNavigationType, url: URL?) -> Decision {
        let isWebURL = url.map { $0.scheme == "http" || $0.scheme == "https" } ?? false
        // Only allow our own in-memory document load — loadHTMLString with
        // baseURL: nil arrives as `.other` with a nil / about:blank URL.
        // Anything else (including file:// from a crafted meta-refresh) is
        // cancelled below.
        if navigationType == .other && (url == nil || url?.absoluteString == "about:blank") {
            return .allow
        }
        // Hand a web URL to the system browser ONLY when the user explicitly
        // tapped a link (`.linkActivated`). A crafted meta-refresh, an
        // auto-submitted form, or any other scripted/automatic navigation in
        // the hostile vault HTML arrives with a different navigationType — we
        // must NOT auto-open those, or we reopen the IP-beacon / phishing
        // vector the CSP meta was added to close. Cancel them silently.
        if isWebURL, let url, navigationType == .linkActivated {
            return .openExternally(url)
        }
        return .cancel
    }
}

struct BriefingHTMLView: UIViewRepresentable {
    let html: String
    var scrollToTopToken: Int = 0
    /// Pull-to-refresh handler — re-checks the vault for new briefings. The
    /// reader is where the user lives, so it must not require a detour through
    /// the Metrics page to force a sync.
    var onRefresh: (() async -> Void)?

    // Read so SwiftUI re-runs updateUIView when the user changes the system
    // text size — wrap() scales the reading font from the current setting.
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeUIView(context: Context) -> WKWebView {
        // The briefing body is LLM-generated HTML — untrusted. Disable
        // JavaScript entirely and (below) cancel every navigation except the
        // initial in-memory load so a crafted briefing cannot execute script or
        // navigate away. Passive subresources (e.g. a remote <img>) are not
        // navigation actions, so this delegate does not see them — the
        // Content-Security-Policy meta in wrap() blocks those off-device fetches
        // (default-src 'none'), closing the remote-subresource IP-beacon vector.
        let config = WKWebViewConfiguration()
        config.suppressesIncrementalRendering = false
        let pagePrefs = WKWebpagePreferences()
        pagePrefs.allowsContentJavaScript = false
        config.defaultWebpagePreferences = pagePrefs
        config.preferences.javaScriptCanOpenWindowsAutomatically = false
        let view = WKWebView(frame: .zero, configuration: config)
        view.navigationDelegate = context.coordinator
        view.scrollView.backgroundColor = UIColor(EstormiColor.charbon)
        view.backgroundColor = UIColor(EstormiColor.charbon)
        view.isOpaque = false
        view.scrollView.showsVerticalScrollIndicator = false
        // The body is clamped to the viewport width in CSS (no horizontal
        // overflow), so there is nothing to scroll sideways — hide the
        // indicator and kill any residual horizontal rubber-banding.
        view.scrollView.showsHorizontalScrollIndicator = false
        view.scrollView.alwaysBounceHorizontal = false
        view.scrollView.contentInsetAdjustmentBehavior = .never
        let refresh = UIRefreshControl()
        refresh.tintColor = UIColor(EstormiColor.orClair)
        refresh.addTarget(
            context.coordinator, action: #selector(Coordinator.handleRefresh(_:)),
            for: .valueChanged)
        view.scrollView.refreshControl = refresh
        return view
    }

    func updateUIView(_ webView: WKWebView, context: Context) {
        let coordinator = context.coordinator
        coordinator.onRefresh = onRefresh
        // Only reload when the rendered document actually changes — the body,
        // or the reading size after a system text-size change — not on every
        // state update (e.g. a scroll-to-top tap).
        let document = wrap(html)
        if coordinator.loadedDocument != document {
            coordinator.loadedDocument = document
            webView.loadHTMLString(document, baseURL: nil)
        }
        // Re-tapping the Briefings tab bumps the token → scroll back to the top.
        if coordinator.lastScrollToken != scrollToTopToken {
            coordinator.lastScrollToken = scrollToTopToken
            let top = CGPoint(x: 0, y: -webView.scrollView.adjustedContentInset.top)
            webView.scrollView.setContentOffset(top, animated: true)
        }
    }

    final class Coordinator: NSObject, WKNavigationDelegate {
        var loadedDocument: String?
        var lastScrollToken = 0
        var onRefresh: (() async -> Void)?

        @objc func handleRefresh(_ control: UIRefreshControl) {
            Task { @MainActor in
                await onRefresh?()
                control.endRefreshing()
            }
        }

        func webView(
            _ webView: WKWebView,
            decidePolicyFor navigationAction: WKNavigationAction,
            decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
        ) {
            switch BriefingNavigationPolicy.decide(
                navigationType: navigationAction.navigationType,
                url: navigationAction.request.url
            ) {
            case .allow:
                decisionHandler(.allow)
            case .openExternally(let url):
                // Hand the web URL to the system browser, then cancel the
                // in-webview navigation so the hostile document never actually
                // navigates.
                UIApplication.shared.open(url)
                decisionHandler(.cancel)
            case .cancel:
                decisionHandler(.cancel)
            }
        }
    }

    // The canonical briefing stylesheet, shared verbatim with the macOS/web-ui
    // renderer (packages/ui-kit/src/briefing.css, bundled here as a resource via
    // project.yml). Loading it — instead of duplicating the rules inline — is
    // what keeps iOS and macOS identical: editing that one file restyles both.
    // Empty string if the resource is somehow missing: the :root + body base
    // below still renders a readable briefing (graceful degradation).
    private static let sharedCSS: String = {
        guard let url = Bundle.main.url(forResource: "briefing", withExtension: "css"),
              let css = try? String(contentsOf: url, encoding: .utf8)
        else { return "" }
        return css
    }()

    private func wrap(_ body: String) -> String {
        // Reading size follows the system text-size setting (Dynamic Type):
        // 18px at the default category, scaled by UIFontMetrics. Derived from
        // the observed `dynamicTypeSize` (not the ambient process trait) so a
        // mid-session setting change re-renders the document. Clamped so the
        // accessibility extremes reflow the column instead of breaking it —
        // pinch-to-zoom stays available beyond the cap.
        let traits = UITraitCollection(
            preferredContentSizeCategory: Self.contentSizeCategory(for: dynamicTypeSize))
        let readingSize = min(
            28.0,
            max(15.0, UIFontMetrics(forTextStyle: .body).scaledValue(for: 18, compatibleWith: traits)))
        // Per-surface base only: design tokens, the page reset, the reading
        // font-size (larger on phone than the desktop modal), and iOS-only
        // elements (code/pre/img). Everything that must look identical across
        // surfaces — headings, drop cap, lists, links, rules, source spans —
        // comes from the shared briefing.css appended after. The body content is
        // wrapped in `.briefing-body`, the scope that stylesheet targets.
        return """
        <!doctype html>
        <html><head><meta charset="utf-8" />
        <!-- No maximum-scale/user-scalable lock: pinch-to-zoom must stay
             available so low-vision readers can enlarge the briefing text
             (WCAG 1.4.4). -->
        <meta name="viewport" content="width=device-width, initial-scale=1.0" />
        <!-- Content-Security-Policy: the briefing body is untrusted LLM-generated
             HTML that ingests world/news content, so it can contain a crafted
             passive subresource — e.g. `<img src="http://tracker/…">`. Disabling
             JavaScript and locking navigation (see makeUIView) stops script and
             page redirects, but a remote subresource is neither: WKWebView would
             still fetch it on open, beaconing the device's IP to an attacker.
             This policy blocks every off-device fetch the briefing could attempt:
               default-src 'none'  — deny all resource types not named below
                                     (scripts, frames, connect/XHR, media, …).
               img-src data:       — images only from inline data: URIs, never
                                     remote http(s); kills the IP-beacon vector.
               style-src 'unsafe-inline' — the page styling is the inline <style>
                                     block below; no external stylesheets.
               font-src data:      — fonts are bundled and system-registered via
                                     UIAppFonts (not CSS-loaded), so this only
                                     permits an inlined data: face and forbids
                                     fetching any remote font. -->
        <meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:;" />
        <style>
          /* Fallback token values for graceful degradation when the bundled
             briefing.css resource is missing (see sharedCSS above). The canonical
             palette lives in packages/ui-kit/src/tokens.css; keep these hexes in
             sync with it by hand — this is HTML/CSS, not a Swift token reference,
             so the `make tokens` generator (which owns Tokens.swift) does not
             touch it. Normally briefing.css supplies the real tokens. */
          :root {
            --encre: #0D1117;
            --charbon: #1A1F29;
            --charbon-2: #232936;
            --parchemin: #F5F1E8;
            --or-ancien: #C8A96B;
            --or-clair: #DCBA8A;
            --or-sombre: #8A7142;
            --pourpre: #B82E2E;
            --ink-dim: rgba(245,241,232,0.62);
            --font-body: "EB Garamond", "EBGaramond", Georgia, serif;
            --font-display: "Cinzel", Georgia, serif;
          }
          *, *::before, *::after { box-sizing: border-box; }
          html, body {
            margin: 0;
            padding: 0;
            background: var(--charbon);
            color: var(--parchemin);
            -webkit-font-smoothing: antialiased;
            -webkit-text-size-adjust: 100%;
            /* Pin the document to the viewport: no sideways scroll/bounce. */
            max-width: 100%;
            overflow-x: hidden;
          }
          body {
            font-family: var(--font-body);
            font-size: \(Int(readingSize.rounded()))px;
            line-height: 1.65;
            padding: 8px 22px 96px;
            /* Break long URLs / unspaced tokens instead of overflowing. */
            overflow-wrap: break-word;
            word-wrap: break-word;
          }
          p { margin: 0 0 1.1em; }
          code, pre {
            font-family: ui-monospace, Menlo, monospace;
            background: var(--charbon-2);
            border-radius: 4px;
            padding: 0.1em 0.35em;
            font-size: 0.92em;
          }
          pre { padding: 12px 14px; overflow-x: auto; }
          img { max-width: 100%; height: auto; border-radius: 8px; }
          \(Self.sharedCSS)
        </style>
        </head><body><div class="briefing-body">\(body)</div></body></html>
        """
    }

    // SwiftUI's DynamicTypeSize has no built-in inverse of
    // DynamicTypeSize(_: UIContentSizeCategory) — map back by hand so
    // UIFontMetrics can scale from the environment value.
    private static func contentSizeCategory(for size: DynamicTypeSize) -> UIContentSizeCategory {
        switch size {
        case .xSmall: return .extraSmall
        case .small: return .small
        case .medium: return .medium
        case .large: return .large
        case .xLarge: return .extraLarge
        case .xxLarge: return .extraExtraLarge
        case .xxxLarge: return .extraExtraExtraLarge
        case .accessibility1: return .accessibilityMedium
        case .accessibility2: return .accessibilityLarge
        case .accessibility3: return .accessibilityExtraLarge
        case .accessibility4: return .accessibilityExtraExtraLarge
        case .accessibility5: return .accessibilityExtraExtraExtraLarge
        @unknown default: return .large
        }
    }
}

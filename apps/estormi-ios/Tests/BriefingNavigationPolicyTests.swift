import Foundation
import Testing
import WebKit

@testable import Estormi

// Tests for `BriefingNavigationPolicy.decide` — the pure decision extracted from
// BriefingHTMLView's WKNavigationDelegate (the IP-beacon / phishing defense).
// The briefing body is untrusted LLM-generated HTML, so the policy denies every
// navigation except our own in-memory load, and hands a web URL to the system
// browser ONLY on an explicit user tap (`.linkActivated`). Each case here has a
// failure mode: allow the wrong load and a crafted meta-refresh navigates the
// device; open a non-tap web URL and the IP-beacon vector reopens.

@Suite("Briefing navigation policy")
struct BriefingNavigationPolicyTests {
    // The initial in-memory document load: loadHTMLString(_, baseURL: nil)
    // arrives as `.other` with a nil URL — must be allowed so the briefing
    // renders.
    @Test func allowsTheInitialInMemoryLoadWithNilURL() {
        #expect(BriefingNavigationPolicy.decide(navigationType: .other, url: nil) == .allow)
    }

    // Some WebKit builds surface the in-memory load with an explicit
    // about:blank URL — that variant must be allowed too.
    @Test func allowsTheInMemoryLoadWithAboutBlankURL() {
        let url = URL(string: "about:blank")
        #expect(BriefingNavigationPolicy.decide(navigationType: .other, url: url) == .allow)
    }

    // A user tapping a link to a web URL: open it externally, then cancel the
    // in-webview navigation (the .openExternally decision carries the URL the
    // delegate hands to UIApplication.open).
    @Test func opensAWebURLExternallyOnAnExplicitLinkTap() throws {
        let url = try #require(URL(string: "https://example.com/article"))
        #expect(
            BriefingNavigationPolicy.decide(navigationType: .linkActivated, url: url)
                == .openExternally(url))
    }

    // The crux of the IP-beacon defense: a web URL reached by any NON-tap
    // navigation type (a crafted meta-refresh / scripted redirect surfaces as
    // `.other`) must be cancelled, never auto-opened.
    @Test func cancelsAWebURLReachedByANonTapNavigation() throws {
        let url = try #require(URL(string: "https://tracker.example/beacon.gif"))
        #expect(BriefingNavigationPolicy.decide(navigationType: .other, url: url) == .cancel)
    }

    // An auto-submitted form in the hostile HTML (`.formSubmitted`) to a web URL
    // is likewise cancelled — only `.linkActivated` opens externally.
    @Test func cancelsAFormSubmissionToAWebURL() throws {
        let url = try #require(URL(string: "https://phish.example/submit"))
        #expect(BriefingNavigationPolicy.decide(navigationType: .formSubmitted, url: url) == .cancel)
    }

    // A crafted file:// meta-refresh arrives as `.other` but is NOT the
    // nil/about:blank in-memory load, so it is cancelled (not allowed).
    @Test func cancelsACraftedFileURLNavigation() throws {
        let url = try #require(URL(string: "file:///etc/passwd"))
        #expect(BriefingNavigationPolicy.decide(navigationType: .other, url: url) == .cancel)
    }

    // A non-web scheme (e.g. a `mailto:` link tap) is not http/https, so it is
    // cancelled rather than opened — the policy only forwards web URLs.
    @Test func cancelsANonWebSchemeEvenOnLinkActivation() throws {
        let url = try #require(URL(string: "mailto:someone@example.com"))
        #expect(
            BriefingNavigationPolicy.decide(navigationType: .linkActivated, url: url) == .cancel)
    }
}

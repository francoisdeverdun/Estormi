import Foundation
import UIKit
import UserNotifications

// Push-based new-briefing alerts (Option A: APNs, the Mac as the direct
// provider — no cloud server, no CloudKit).
//
// The Mac composes the briefing, signs an APNs JWT with its .p8 key and POSTs
// to api.push.apple.com. The only iOS→Mac channel is the device token, which
// we drop into the iCloud vault as `apns/<vendorID>.json`; the Mac reads it
// from the same folder it writes briefings into and fans the alert out to every
// registered device. See docs/ios-push-notifications.md and
// estormi_ingestion/shared/delivery/apns_push.py.
//
// Requires the Apple Developer Program: the Push Notifications capability and
// the `aps-environment` entitlement are unavailable on a free account, so
// `registerForRemoteNotifications` never yields a token there.

@MainActor
enum RemotePushRegistrar {
    private static let enabledKey = "estormi.notify.enabled"
    private static let lastTokenKey = "estormi.notify.lastToken"
    private static let tokenWriteFailedKey = "estormi.notify.tokenWriteFailed"
    private static let defaults = UserDefaults.standard
    // Reused across token writes — constructing an ISO8601DateFormatter is
    // surprisingly costly, so we don't spin one up per token.
    private static let iso8601 = ISO8601DateFormatter()

    static var isEnabled: Bool {
        get { defaults.bool(forKey: enabledKey) }
        set { defaults.set(newValue, forKey: enabledKey) }
    }

    /// `true` when the last APNs token write into the vault failed. Without
    /// this the alerts toggle reads "enabled" while the Mac can never receive
    /// the token (so no pushes ever arrive); the Settings UI surfaces it so the
    /// user knows to re-pick the vault folder or check iCloud Drive.
    static var tokenDeliveryFailed: Bool {
        get { defaults.bool(forKey: tokenWriteFailedKey) }
        set { defaults.set(newValue, forKey: tokenWriteFailedKey) }
    }

    /// The APNs environment this build's token belongs to. A build run from
    /// Xcode carries the `development` aps-environment entitlement → sandbox
    /// APNs; an App Store / TestFlight build → production. The Mac reads this to
    /// pick the matching APNs host — mixing them up yields a silent
    /// `BadDeviceToken`.
    static var environment: String {
        #if DEBUG
        return "sandbox"
        #else
        return "production"
        #endif
    }

    // MARK: - Permission + registration

    /// Ask for alert permission, then register with APNs. The token arrives
    /// asynchronously in `AppDelegate.didRegisterForRemoteNotifications…`.
    /// Returns whether notification permission was granted.
    @discardableResult
    static func enable() async -> Bool {
        let center = UNUserNotificationCenter.current()
        let granted =
            (try? await center.requestAuthorization(options: [.alert, .sound, .badge])) ?? false
        isEnabled = granted
        if granted {
            UIApplication.shared.registerForRemoteNotifications()
        }
        return granted
    }

    /// Re-register on cold launch when the user previously opted in, so a
    /// rotated token reaches the vault.
    static func registerIfEnabled() {
        guard isEnabled else { return }
        UIApplication.shared.registerForRemoteNotifications()
    }

    /// The system's *actual* notification authorization for this app — the
    /// source of truth the local `isEnabled` flag can drift from when the user
    /// revokes (or grants) permission in iOS Settings while the app is away.
    /// Reconciles the cached flag to match and returns it, so a UI toggle can
    /// re-sync on foreground.
    static func syncEnabledFromSystem() async -> Bool {
        let settings = await UNUserNotificationCenter.current().notificationSettings()
        let authorized = settings.authorizationStatus == .authorized
        if isEnabled != authorized { isEnabled = authorized }
        if authorized { UIApplication.shared.registerForRemoteNotifications() }
        return authorized
    }

    static func disable() {
        isEnabled = false
        tokenDeliveryFailed = false
        UIApplication.shared.unregisterForRemoteNotifications()
        defaults.removeObject(forKey: lastTokenKey)
        removeTokenFile()
    }

    // MARK: - Token plumbing (vault)

    /// Persist the APNs device token into the vault as `apns/<vendorID>.json`
    /// so the Mac can read it. Skips the write when the token is unchanged and
    /// the file still exists (avoids churning iCloud Drive on every launch).
    ///
    /// The actual coordinated I/O runs off the main actor (it blocks against the
    /// iCloud file provider, which can be slow) on a detached task; we only hop
    /// back here to record the new token or the failure flag the UI reads.
    static func handle(deviceToken: Data) {
        let hex = deviceToken.map { String(format: "%02x", $0) }.joined()
        let snapshot = TokenSnapshot(
            hex: hex,
            bundleId: Bundle.main.bundleIdentifier ?? "app.estormi.ios",
            environment: environment,
            updatedAt: iso8601.string(from: Date()))
        let lastToken = defaults.string(forKey: lastTokenKey)
        Task.detached {
            if hex == lastToken, tokenFileExists() {
                await MainActor.run { tokenDeliveryFailed = false }
                return
            }
            let wrote = writeTokenFile(snapshot)
            await MainActor.run {
                if wrote {
                    defaults.set(hex, forKey: lastTokenKey)
                    tokenDeliveryFailed = false
                } else {
                    // Leave lastToken untouched so a later registration retries
                    // the write rather than thinking it already succeeded.
                    tokenDeliveryFailed = true
                }
            }
        }
    }

    /// The fields the vault token file carries — captured on the main actor (it
    /// reads `Bundle`/`environment`) and passed to the off-actor writer so the
    /// coordinated I/O touches no main-actor state.
    private struct TokenSnapshot: Sendable {
        let hex: String
        let bundleId: String
        let environment: String
        let updatedAt: String
    }

    private static let cachedVendorID: String = {
        UIDevice.current.identifierForVendor?.uuidString ?? "unknown-device"
    }()

    private nonisolated static func tokenURL(in folder: URL) -> URL {
        folder
            .appendingPathComponent("apns", isDirectory: true)
            .appendingPathComponent("\(cachedVendorID).json")
    }

    private nonisolated static func tokenFileExists() -> Bool {
        guard let folder = try? VaultFolder.shared.resolveURL() else { return false }
        let scoped = folder.startAccessingSecurityScopedResource()
        defer { if scoped { folder.stopAccessingSecurityScopedResource() } }
        return FileManager.default.fileExists(atPath: tokenURL(in: folder).path)
    }

    @discardableResult
    private nonisolated static func writeTokenFile(_ snapshot: TokenSnapshot) -> Bool {
        guard let folder = try? VaultFolder.shared.resolveURL() else { return false }
        let scoped = folder.startAccessingSecurityScopedResource()
        defer { if scoped { folder.stopAccessingSecurityScopedResource() } }

        // Only what the Mac provider (apns_push.py) actually reads — token,
        // bundleId, environment. No device name or other PII is written into
        // the shared iCloud vault.
        let payload: [String: Any] = [
            "token": snapshot.hex,
            "bundleId": snapshot.bundleId,
            "environment": snapshot.environment,
            "updatedAt": snapshot.updatedAt,
        ]
        guard
            let data = try? JSONSerialization.data(
                withJSONObject: payload, options: [.prettyPrinted, .sortedKeys])
        else { return false }

        let url = tokenURL(in: folder)
        try? FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(), withIntermediateDirectories: true)

        var coordError: NSError?
        var wrote = false
        NSFileCoordinator().coordinate(
            writingItemAt: url, options: .forReplacing, error: &coordError
        ) { dst in
            wrote = (try? data.write(to: dst, options: .atomic)) != nil
        }
        return wrote && coordError == nil
    }

    private nonisolated static func removeTokenFile() {
        guard let folder = try? VaultFolder.shared.resolveURL() else { return }
        let scoped = folder.startAccessingSecurityScopedResource()
        defer { if scoped { folder.stopAccessingSecurityScopedResource() } }
        let url = tokenURL(in: folder)
        var coordError: NSError?
        NSFileCoordinator().coordinate(
            writingItemAt: url, options: .forDeleting, error: &coordError
        ) { dst in
            try? FileManager.default.removeItem(at: dst)
        }
    }
}

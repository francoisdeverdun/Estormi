import SwiftUI
import UIKit
import UserNotifications

@main
struct EstormiApp: App {
    @StateObject private var store = VaultStore()
    // The app delegate owns the APNs registration callbacks (SwiftUI's App
    // lifecycle doesn't surface `didRegisterForRemoteNotifications…`).
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(store)
        }
    }
}

// New-briefing alerts arrive as push notifications from the Mac (it is the APNs
// provider — see RemotePushRegistrar). The delegate registers for remote
// notifications when the user has opted in and forwards the device token to the
// vault so the Mac can reach this device.
final class AppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        // Own the notification center so a "new briefing" push that lands while
        // the app is foregrounded still surfaces (see willPresent below).
        UNUserNotificationCenter.current().delegate = self
        Task { @MainActor in
            RemotePushRegistrar.registerIfEnabled()
            // The CloudKit doorbell subscription is per iCloud account: retry
            // the bootstrap on every launch (no-op once saved) and re-save it
            // when the account changes.
            CloudKitDoorbell.observeAccountChanges()
            await CloudKitDoorbell.bootstrapIfNeeded()
        }
        return true
    }

    // Without this, a push delivered while the app is in the foreground is
    // swallowed silently. Show it as a banner with sound, same as in background.
    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification
    ) async -> UNNotificationPresentationOptions {
        [.banner, .sound]
    }

    func application(
        _ application: UIApplication,
        didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data
    ) {
        Task { @MainActor in RemotePushRegistrar.handle(deviceToken: deviceToken) }
    }

    func application(
        _ application: UIApplication,
        didFailToRegisterForRemoteNotificationsWithError error: Error
    ) {
        NSLog("Estormi: APNs registration failed — %@", error.localizedDescription)
    }
}

import CloudKit
import Foundation
import os

private let doorbellLog = Logger(subsystem: "app.estormi.ios", category: "doorbell")

// CloudKit "doorbell" (Option B): the Mac writes one tiny `Briefing` record
// into the user's PRIVATE database via a signed helper (apps/estormi-cloud),
// and the CKQuerySubscription saved here makes APPLE sign and deliver the
// visible banner — no push key on the Mac at all, so it works for
// Store-distributed builds. Coexists with Option A (RemotePushRegistrar): the
// Mac rings the doorbell first and only falls back to direct APNs when the
// doorbell is unavailable. See estormi_ingestion/shared/delivery/cloudkit_doorbell.py
// and docs/cloudkit-doorbell.md.

@MainActor
enum CloudKitDoorbell {
    // Must match the Mac helper — and NEVER CKContainer.default(): the two
    // apps have different bundle ids, so their implicit containers diverge.
    // (nonisolated: plain immutable constants, read by the nonisolated factory.)
    nonisolated static let containerID = "iCloud.app.estormi.ios"
    nonisolated static let recordType = "Briefing"
    nonisolated static let subscriptionID = "briefing-created"

    /// Pure factory — unit-testable without an iCloud session.
    nonisolated static func makeSubscription() -> CKQuerySubscription {
        let subscription = CKQuerySubscription(
            recordType: recordType,
            predicate: NSPredicate(value: true),
            subscriptionID: subscriptionID,
            options: .firesOnRecordCreation
        )
        let info = CKSubscription.NotificationInfo()
        // The app may be force-quit when the banner lands, so no app code runs
        // at delivery — the alert text must be self-sufficient. The
        // localization args pull the record's `date` field into the body.
        info.titleLocalizationKey = "BRIEFING_PUSH_TITLE"
        info.alertLocalizationKey = "BRIEFING_PUSH_BODY"
        info.alertLocalizationArgs = ["date"]
        info.desiredKeys = ["date"]
        info.soundName = "default"
        // Never content-available: silent pushes are throttled to a trickle
        // and are not delivered at all to a force-quit app.
        info.shouldSendContentAvailable = false
        subscription.notificationInfo = info
        return subscription
    }

    /// Ensure the subscription exists. Safe to call on every launch — the
    /// check is a server-side fetch, not a local flag, so it self-heals any
    /// state a past failure left behind (e.g. a save attempted while the
    /// freshly created container was still propagating on Apple's side).
    /// One cheap call per launch; silently retried next launch when iCloud
    /// is unavailable. Diagnostics go through os.Logger (visible in Console.app
    /// or `log stream` during a tethered launch, suppressed in release).
    static func bootstrapIfNeeded() async {
        guard RemotePushRegistrar.isEnabled else {
            doorbellLog.debug("alerts disabled — not subscribing")
            return
        }
        let container = CKContainer(identifier: containerID)
        guard let status = try? await container.accountStatus(), status == .available else {
            doorbellLog.debug("iCloud unavailable — will retry next launch")
            return
        }
        let database = container.privateCloudDatabase
        if (try? await database.subscription(for: subscriptionID)) != nil {
            doorbellLog.debug("subscription present")
            return
        }
        do {
            _ = try await database.save(makeSubscription())
            doorbellLog.debug("subscription saved")
        } catch {
            doorbellLog.debug("subscription save failed — \(error.localizedDescription, privacy: .public)")
        }
    }

    /// Remove the subscription when the user turns alerts off — without this
    /// the doorbell would keep ringing banners that the APNs path correctly
    /// stopped sending. Best-effort: an offline failure leaves a dead
    /// subscription that the next enable's bootstrap simply finds again.
    static func teardown() async {
        let container = CKContainer(identifier: containerID)
        try? await container.privateCloudDatabase.deleteSubscription(withID: subscriptionID)
    }

    /// Re-run the bootstrap when the iCloud account changes: subscriptions
    /// are per account, so the new account starts without one (the fetch in
    /// `bootstrapIfNeeded` is what notices).
    static func observeAccountChanges() {
        NotificationCenter.default.addObserver(
            forName: .CKAccountChanged, object: nil, queue: .main
        ) { _ in
            Task { @MainActor in await bootstrapIfNeeded() }
        }
    }
}

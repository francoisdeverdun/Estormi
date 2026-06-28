import CloudKit
import Testing

@testable import Estormi

// The subscription is the doorbell's whole contract with Apple: its shape is
// what makes the Mac helper's record write turn into a visible banner. The
// network side can only be exercised on a real device (see
// docs/cloudkit-doorbell.md), so these tests pin the pure factory — every
// field here has a failure mode attached (wrong record type or predicate =
// never fires; missing localization keys = invisible "low priority" push;
// content-available = throttled and dead after force-quit).

@Suite("CloudKit doorbell subscription")
struct CloudKitDoorbellTests {
    @Test func subscriptionTargetsTheBriefingRecordType() {
        let sub = CloudKitDoorbell.makeSubscription()
        #expect(sub.recordType == "Briefing")
        #expect(sub.subscriptionID == "briefing-created")
        #expect(sub.querySubscriptionOptions == .firesOnRecordCreation)
        #expect(sub.predicate == NSPredicate(value: true))
    }

    @Test func bannerIsVisibleAndSelfSufficient() throws {
        let info = try #require(CloudKitDoorbell.makeSubscription().notificationInfo)
        // Localized alert text — without alertLocalizationKey (or alertBody)
        // CloudKit sends a low-priority push that displays NOTHING.
        #expect(info.titleLocalizationKey == "BRIEFING_PUSH_TITLE")
        #expect(info.alertLocalizationKey == "BRIEFING_PUSH_BODY")
        // The date is substituted server-side from the record: the app may be
        // force-quit at delivery, so no app code can fill it in.
        #expect(info.alertLocalizationArgs == ["date"])
        #expect(info.desiredKeys == ["date"])
        #expect(info.soundName == "default")
    }

    @Test func neverContentAvailable() throws {
        let info = try #require(CloudKitDoorbell.makeSubscription().notificationInfo)
        // Silent pushes are throttled (~1-2/h across all apps) and are never
        // delivered to a force-quit app — the doorbell must stay a banner.
        #expect(info.shouldSendContentAvailable == false)
    }

    @Test func containerIsExplicitlyShared() {
        // CKContainer.default() would resolve this app's own implicit
        // container; the Mac helper writes into the shared, named one.
        #expect(CloudKitDoorbell.containerID == "iCloud.app.estormi.ios")
    }
}

// EstormiCloud — the CloudKit "doorbell" helper.
//
// Writes one tiny `Briefing` record into the PRIVATE CloudKit database of the
// Mac's iCloud session. The iOS companion keeps a CKQuerySubscription on the
// same container, so Apple — not the Mac — signs and delivers the visible
// banner. The record carries nothing beyond the alert text and a date; the
// vault in iCloud Drive remains the only data transport.
//
// This binary exists because CloudKit is gated by *restricted* entitlements,
// which a bare executable can never claim (TN3125): the entitlements bearer
// must be an .app-shaped bundle carrying its provisioning profile. The Python
// pipeline execs the inner Mach-O directly (Apple's "daemon in app's
// clothing" pattern) — see estormi_ingestion/shared/delivery/cloudkit_doorbell.py and
// docs/cloudkit-doorbell.md.
//
// Exit codes (consumed by cloudkit_doorbell.py — keep the two in sync):
//   0   record saved (or --status: account available)
//   1   unexpected CloudKit / runtime error
//   2   no iCloud account on this Mac (or access restricted)
//   3   transient network / service failure — worth retrying later
//   64  usage error

import CloudKit
import Foundation

// NEVER CKContainer.default(): that would resolve this helper's own implicit
// container (iCloud.app.estormi.doorbell), not the one the iOS app watches.
private let containerID = "iCloud.app.estormi.ios"
private let recordType = "Briefing"
private let purgeAfterDays: Double = 7
// Hard per-call ceiling; the Python caller enforces its own 30 s on top.
private let callTimeout: TimeInterval = 20

// Not private: top-level bindings below carry this type, and it doubles as
// the error side of the synchronous bridges' Result.
enum Exit: Int32, Error {
    case ok = 0
    case failure = 1
    case noAccount = 2
    case network = 3
    case usage = 64
}

private func die(_ code: Exit, _ message: String) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(code.rawValue)
}

private func classify(_ error: Error) -> Exit {
    guard let ck = error as? CKError else { return .failure }
    switch ck.code {
    case .notAuthenticated:
        return .noAccount
    case .networkUnavailable, .networkFailure, .serviceUnavailable,
         .requestRateLimited, .zoneBusy:
        return .network
    default:
        return .failure
    }
}

// ── Synchronous CloudKit bridges ─────────────────────────────────────────────
// CloudKit is callback-based; a semaphore keeps this process single-purpose
// with a hard internal timeout instead of an open-ended runloop.

private func accountStatus(of container: CKContainer) -> Result<CKAccountStatus, Exit> {
    let sem = DispatchSemaphore(value: 0)
    var status: CKAccountStatus = .couldNotDetermine
    container.accountStatus { s, _ in
        status = s
        sem.signal()
    }
    guard sem.wait(timeout: .now() + callTimeout) == .success else {
        return .failure(.network)
    }
    return .success(status)
}

private func save(_ record: CKRecord, into database: CKDatabase) -> Exit {
    let sem = DispatchSemaphore(value: 0)
    var outcome: Exit = .failure
    database.save(record) { _, error in
        outcome = error.map(classify) ?? .ok
        if let error { FileHandle.standardError.write(Data("save: \(error)\n".utf8)) }
        sem.signal()
    }
    guard sem.wait(timeout: .now() + callTimeout) == .success else { return .network }
    return outcome
}

/// Best-effort cleanup of doorbell records older than `purgeAfterDays`.
/// Queries our own `createdAt` field (just-in-time schema indexes it in the
/// Development environment; the promoted Production schema must keep that
/// index). Failures are logged and swallowed — purging never affects the
/// exit code or delays the banner meaningfully.
private func purgeOldRecords(in database: CKDatabase) {
    let cutoff = Date(timeIntervalSinceNow: -purgeAfterDays * 24 * 3600)
    let query = CKQuery(
        recordType: recordType,
        predicate: NSPredicate(format: "createdAt < %@", cutoff as NSDate)
    )
    let sem = DispatchSemaphore(value: 0)
    var staleIDs: [CKRecord.ID] = []
    database.fetch(
        withQuery: query, inZoneWith: nil, desiredKeys: [], resultsLimit: 50
    ) { result in
        if case .success(let response) = result {
            staleIDs = response.matchResults.map(\.0)
        } else if case .failure(let error) = result {
            // Most likely a missing `createdAt` Queryable index in a promoted
            // Production schema (CloudKit has no just-in-time indexing there).
            // Non-fatal — old records simply accumulate until the index is added.
            FileHandle.standardError.write(
                Data("purge query (createdAt index missing in Production?): \(error)\n".utf8))
        }
        sem.signal()
    }
    guard sem.wait(timeout: .now() + 10) == .success, !staleIDs.isEmpty else { return }

    let done = DispatchSemaphore(value: 0)
    database.modifyRecords(saving: [], deleting: staleIDs) { result in
        if case .success = result {
            print("purged \(staleIDs.count) record(s) older than \(Int(purgeAfterDays)) days")
        }
        done.signal()
    }
    _ = done.wait(timeout: .now() + 10)
}

// ── CLI ──────────────────────────────────────────────────────────────────────

private func parseArguments() -> (status: Bool, subscriptions: Bool, subscribe: Bool, unsubscribe: String, title: String, body: String, date: String) {
    var status = false
    var subscriptions = false
    var subscribe = false
    var unsubscribe = ""
    var title = "", body = "", date = ""
    var args = Array(CommandLine.arguments.dropFirst())
    while !args.isEmpty {
        let flag = args.removeFirst()
        switch flag {
        case "--status":
            status = true
        case "--subscriptions":
            subscriptions = true
        case "--subscribe":
            subscribe = true
        case "--unsubscribe":
            guard !args.isEmpty else { die(.usage, "missing value for \(flag)") }
            unsubscribe = args.removeFirst()
        case "--title", "--body", "--date":
            guard !args.isEmpty else { die(.usage, "missing value for \(flag)") }
            let value = args.removeFirst()
            switch flag {
            case "--title": title = value
            case "--body": body = value
            default: date = value
            }
        default:
            die(.usage, """
            usage: EstormiCloud --status | --subscriptions | --subscribe | --unsubscribe <id>
                   EstormiCloud --title <t> --body <b> --date <YYYY-MM-DD>
            """)
        }
    }
    return (status, subscriptions, subscribe, unsubscribe, title, body, date)
}

/// Save the `briefing-created` subscription from the Mac. Subscriptions are
/// per user × container × environment — ANY signed-in client can hold the
/// one subscription, and Apple fans the banner out to every device of the
/// user registered for the container's pushes. Normally the iOS app saves it
/// (CloudKitDoorbell.makeSubscription — keep the two in sync); this flag
/// exists to bootstrap or repair it from the Mac, e.g. while the phone's
/// CloudKit client sits in a post-503 throttle window.
private func saveSubscription(in database: CKDatabase) -> Exit {
    let subscription = CKQuerySubscription(
        recordType: recordType,
        predicate: NSPredicate(value: true),
        subscriptionID: "briefing-created",
        options: .firesOnRecordCreation
    )
    let info = CKSubscription.NotificationInfo()
    info.titleLocalizationKey = "BRIEFING_PUSH_TITLE"
    info.alertLocalizationKey = "BRIEFING_PUSH_BODY"
    info.alertLocalizationArgs = ["date"]
    info.desiredKeys = ["date"]
    info.soundName = "default"
    info.shouldSendContentAvailable = false
    subscription.notificationInfo = info

    let sem = DispatchSemaphore(value: 0)
    var outcome: Exit = .failure
    database.save(subscription) { _, error in
        if let error {
            FileHandle.standardError.write(Data("save subscription: \(error)\n".utf8))
            outcome = classify(error)
        } else {
            print("subscription saved: briefing-created")
            outcome = .ok
        }
        sem.signal()
    }
    guard sem.wait(timeout: .now() + callTimeout) == .success else { return .network }
    return outcome
}

/// Delete one subscription by ID — the repair-side complement of
/// `--subscribe` (e.g. removing a stale or experimental subscription that
/// would otherwise ring a duplicate banner on every record).
private func deleteSubscription(in database: CKDatabase, id: String) -> Exit {
    let sem = DispatchSemaphore(value: 0)
    var outcome: Exit = .failure
    database.delete(withSubscriptionID: id) { _, error in
        if let error {
            FileHandle.standardError.write(Data("delete subscription: \(error)\n".utf8))
            outcome = classify(error)
        } else {
            print("subscription deleted: \(id)")
            outcome = .ok
        }
        sem.signal()
    }
    guard sem.wait(timeout: .now() + callTimeout) == .success else { return .network }
    return outcome
}

/// Diagnostic: list this user's subscriptions in the private database. The
/// iPhone's `briefing-created` subscription must appear here (subscriptions
/// are per user × container × environment, visible from any signed-in
/// device) — its absence explains a silent doorbell better than any log.
private func printSubscriptions(in database: CKDatabase) -> Exit {
    let sem = DispatchSemaphore(value: 0)
    var outcome: Exit = .failure
    database.fetchAllSubscriptions { subs, error in
        if let error {
            FileHandle.standardError.write(Data("fetchAllSubscriptions: \(error)\n".utf8))
            outcome = classify(error)
        } else {
            let subs = subs ?? []
            print("subscriptions: \(subs.count)")
            for sub in subs {
                let q = sub as? CKQuerySubscription
                let info = sub.notificationInfo
                print(
                    "  id=\(sub.subscriptionID) type=\(q?.recordType ?? "?") "
                        + "alertKey=\(info?.alertLocalizationKey ?? "nil") "
                        + "contentAvailable=\(info?.shouldSendContentAvailable ?? false)")
            }
            outcome = .ok
        }
        sem.signal()
    }
    guard sem.wait(timeout: .now() + callTimeout) == .success else { return .network }
    return outcome
}

private func describe(_ status: CKAccountStatus) -> String {
    switch status {
    case .available: return "available"
    case .noAccount: return "noAccount"
    case .restricted: return "restricted"
    case .temporarilyUnavailable: return "temporarilyUnavailable"
    case .couldNotDetermine: return "couldNotDetermine"
    @unknown default: return "unknown"
    }
}

let arguments = parseArguments()
let container = CKContainer(identifier: containerID)

let account: CKAccountStatus
switch accountStatus(of: container) {
case .failure(let code):
    die(code, "accountStatus: timed out")
case .success(let status):
    account = status
}

if arguments.status {
    print("container=\(containerID) account=\(describe(account))")
    exit(account == .available ? Exit.ok.rawValue : Exit.noAccount.rawValue)
}

if arguments.subscriptions {
    guard account == .available else {
        die(.noAccount, "iCloud account is \(describe(account))")
    }
    exit(printSubscriptions(in: container.privateCloudDatabase).rawValue)
}

if arguments.subscribe || !arguments.unsubscribe.isEmpty {
    guard account == .available else {
        die(.noAccount, "iCloud account is \(describe(account))")
    }
    let database = container.privateCloudDatabase
    if arguments.subscribe {
        exit(saveSubscription(in: database).rawValue)
    }
    exit(deleteSubscription(in: database, id: arguments.unsubscribe).rawValue)
}

guard !arguments.title.isEmpty, !arguments.body.isEmpty, !arguments.date.isEmpty else {
    die(.usage, "all of --title, --body and --date are required")
}
guard account == .available else {
    die(.noAccount, "iCloud account is \(describe(account)) — doorbell needs a signed-in session")
}

// One record per notification event: the health refresh re-announces the same
// date, and a subscription firing on record CREATION would stay silent on an
// update — hence the per-event UUID in the record name.
let recordID = CKRecord.ID(recordName: "briefing-\(arguments.date)-\(UUID().uuidString)")
let record = CKRecord(recordType: recordType, recordID: recordID)
record["date"] = arguments.date as CKRecordValue
record["title"] = arguments.title as CKRecordValue
record["body"] = arguments.body as CKRecordValue
record["createdAt"] = Date() as CKRecordValue

let database = container.privateCloudDatabase
let outcome = save(record, into: database)
guard outcome == .ok else {
    die(outcome, "doorbell record not saved (exit \(outcome.rawValue))")
}
print("saved \(recordID.recordName)")
purgeOldRecords(in: database)
exit(Exit.ok.rawValue)

import Foundation
import UIKit
import UniformTypeIdentifiers

// Vault folder selection + bookmark persistence.
//
// The Mac writes briefings and engine history into a folder on the user's
// iCloud Drive (see `estormi_ingestion/shared/delivery/vault_sync.py`).
// The user grants this app access to that folder once via the system folder
// picker; we persist a security-scoped bookmark and re-read it on demand.
// No CloudKit, no iCloud entitlement, no Apple Developer Program required.

enum VaultFolderStatus: String {
    case ready
    case noFolder
    case stale
}

struct VaultError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

@MainActor
final class VaultFolder {
    static let shared = VaultFolder()

    private let key = "estormi.vault.bookmark"
    private let defaults = UserDefaults.standard
    // Held only while the picker is on screen so its delegate isn't released.
    private var activeDelegate: FolderPickerDelegate?

    func status() -> VaultFolderStatus {
        #if DEBUG
        if debugFallbackURL() != nil { return .ready }
        #endif
        guard defaults.data(forKey: key) != nil else { return .noFolder }
        return (try? resolveURL()) != nil ? .ready : .stale
    }

    func displayName() -> String? {
        #if DEBUG
        if let url = debugFallbackURL() { return "Debug · \(url.lastPathComponent)" }
        #endif
        return (try? resolveURL())?.lastPathComponent
    }

    #if DEBUG
    // Sim / dev-build fallback. When no real folder has been picked, but a
    // `DebugVault` directory exists in the app's Documents folder, treat it
    // as the vault. Lets us seed fake JSON via `simctl get_app_container`
    // and validate the UI without driving the document picker.
    nonisolated func debugFallbackURL() -> URL? {
        guard
            let docs = FileManager.default.urls(
                for: .documentDirectory, in: .userDomainMask
            ).first
        else { return nil }
        let url = docs.appendingPathComponent("DebugVault", isDirectory: true)
        var isDir: ObjCBool = false
        let exists = FileManager.default.fileExists(atPath: url.path, isDirectory: &isDir)
        return exists && isDir.boolValue ? url : nil
    }
    #endif

    func clear() {
        defaults.removeObject(forKey: key)
    }

    // Resolve the stored bookmark to the vault folder URL. iOS document-picker
    // bookmarks are implicitly security-scoped — `.withSecurityScope` is macOS
    // only — so we pass an empty options set. Callers must bracket reads with
    // `startAccessingSecurityScopedResource` / `stop…`.
    // nonisolated: pure Foundation (UserDefaults + bookmark resolution),
    // called from the off-main-actor vault readers and the APNs token writer.
    nonisolated func resolveURL() throws -> URL {
        #if DEBUG
        if let url = debugFallbackURL() { return url }
        #endif
        guard let data = defaults.data(forKey: key) else {
            throw VaultError(message: "No vault folder selected.")
        }
        var isStale = false
        let url = try URL(
            resolvingBookmarkData: data,
            options: [],
            relativeTo: nil,
            bookmarkDataIsStale: &isStale)
        if isStale {
            // Try to refresh in place; if it fails the user re-picks.
            if url.startAccessingSecurityScopedResource() {
                defer { url.stopAccessingSecurityScopedResource() }
                if let fresh = try? url.bookmarkData(
                    options: [], includingResourceValuesForKeys: nil, relativeTo: nil)
                {
                    defaults.set(fresh, forKey: key)
                }
            }
        }
        return url
    }

    func pick() async throws {
        guard let presenter = Self.topViewController() else {
            throw VaultError(message: "No screen is available to present the folder picker.")
        }
        let picked: URL = try await withCheckedThrowingContinuation { continuation in
            let picker = UIDocumentPickerViewController(
                forOpeningContentTypes: [UTType.folder], asCopy: false)
            picker.allowsMultipleSelection = false
            let delegate = FolderPickerDelegate { [weak self] result in
                self?.activeDelegate = nil
                continuation.resume(with: result)
            }
            self.activeDelegate = delegate
            picker.delegate = delegate
            presenter.present(picker, animated: true)
        }
        let scoped = picked.startAccessingSecurityScopedResource()
        defer { if scoped { picked.stopAccessingSecurityScopedResource() } }
        let bookmark = try picked.bookmarkData(
            options: [],
            includingResourceValuesForKeys: nil,
            relativeTo: nil)
        defaults.set(bookmark, forKey: key)
    }

    private static func topViewController() -> UIViewController? {
        let scenes = UIApplication.shared.connectedScenes
            .compactMap { $0 as? UIWindowScene }
        let window =
            scenes.flatMap { $0.windows }.first { $0.isKeyWindow }
            ?? scenes.first?.windows.first
        var top = window?.rootViewController
        while let presented = top?.presentedViewController { top = presented }
        return top
    }
}

private final class FolderPickerDelegate: NSObject, UIDocumentPickerDelegate {
    private let completion: (Result<URL, Error>) -> Void
    private var done = false

    init(completion: @escaping (Result<URL, Error>) -> Void) {
        self.completion = completion
    }

    private func finish(_ result: Result<URL, Error>) {
        guard !done else { return }
        done = true
        completion(result)
    }

    func documentPicker(
        _ controller: UIDocumentPickerViewController, didPickDocumentsAt urls: [URL]
    ) {
        if let url = urls.first {
            finish(.success(url))
        } else {
            finish(.failure(VaultError(message: "No folder was selected.")))
        }
    }

    func documentPickerWasCancelled(_ controller: UIDocumentPickerViewController) {
        finish(.failure(VaultError(message: "Folder selection was cancelled.")))
    }
}

import Foundation

// Shared date helpers for the vault's ISO-8601 timestamps. The Mac writes
// second- or fractional-second UTC strings; the UI shows them humanised.
enum EstormiDate {
    private static let withFraction: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()
    private static let plain = ISO8601DateFormatter()
    private static let relativeFormatter: RelativeDateTimeFormatter = {
        let f = RelativeDateTimeFormatter()
        f.unitsStyle = .abbreviated
        return f
    }()
    private static let shortDateTimeFormatter: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale.current
        f.dateStyle = .medium
        f.timeStyle = .short
        return f
    }()

    static func parse(_ iso: String) -> Date? {
        withFraction.date(from: iso) ?? plain.date(from: iso)
    }

    // "5 hr ago", "2 days ago" — relative to now.
    static func relative(_ iso: String) -> String {
        guard let date = parse(iso) else { return iso }
        return relativeFormatter.localizedString(for: date, relativeTo: Date())
    }

    // "30 May, 07:39" — absolute, compact.
    static func shortDateTime(_ iso: String) -> String {
        guard let date = parse(iso) else { return iso }
        return shortDateTimeFormatter.string(from: date)
    }
}

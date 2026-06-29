import SwiftUI

// Horizontal date archive — oldest on the left, the latest briefing on the
// right (a timeline reading left→right). Opens scrolled to the right end; scroll
// left to step back through the archive. Tap to switch which briefing is open.

struct BriefingDateStrip: View {
    let dates: [String]
    @Binding var selected: String?
    // Bumped when the Briefings tab is re-tapped: re-centre the strip on the
    // open day, so stepping far back through the archive always has a one-tap
    // way home.
    var recenterToken: Int = 0

    // `dates` arrives newest-first; lay it out oldest→latest so the most recent
    // sits at the right edge.
    private var ordered: [String] { dates.reversed() }
    private var latest: String? { dates.first }

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 8) {
                    ForEach(ordered, id: \.self) { date in
                        DateChip(
                            date: date,
                            isSelected: date == selected
                        )
                        .id(date)
                        .onTapGesture {
                            selected = date
                        }
                    }
                }
                .padding(.horizontal, 20)
            }
            .onChange(of: selected) { _, new in
                guard let new else { return }
                withAnimation(.easeInOut(duration: EstormiMetric.Motion.medium)) {
                    proxy.scrollTo(new, anchor: new == latest ? .trailing : .center)
                }
            }
            .onChange(of: recenterToken) { _, _ in
                guard let target = selected ?? latest else { return }
                withAnimation(.easeInOut(duration: EstormiMetric.Motion.medium)) {
                    proxy.scrollTo(target, anchor: target == latest ? .trailing : .center)
                }
            }
            .onAppear {
                if let latest { proxy.scrollTo(latest, anchor: .trailing) }
            }
        }
        .sensoryFeedback(.selection, trigger: selected)
    }
}

private struct DateChip: View {
    let date: String
    let isSelected: Bool

    var body: some View {
        VStack(spacing: 2) {
            Text(weekday)
                .font(EstormiFont.display(10, bold: true))
                .tracking(2)
                .foregroundStyle(EstormiColor.orSombre)
            Text(dayNumber)
                .font(EstormiFont.display(20, bold: true))
                .foregroundStyle(
                    isSelected ? EstormiColor.parcheminOs : EstormiColor.orClair)
            Text(monthShort)
                .font(EstormiFont.body(10))
                .foregroundStyle(EstormiColor.orSombre)
        }
        .padding(.vertical, 10)
        .padding(.horizontal, 12)
        .frame(minWidth: 56)
        .background(
            RoundedRectangle(
                cornerRadius: EstormiMetric.radiusTight, style: .continuous
            )
            .fill(
                isSelected
                    ? EstormiColor.orAncien.opacity(0.18)
                    : Color.clear
            )
        )
        .overlay(
            RoundedRectangle(
                cornerRadius: EstormiMetric.radiusTight, style: .continuous
            )
            .stroke(
                isSelected ? EstormiColor.orAncien : EstormiColor.orSombre.opacity(0.3),
                lineWidth: isSelected ? 1.0 : 0.5)
        )
        // The chip is three stacked Texts (letter / number / month abbrev);
        // collapse them into one element so VoiceOver reads a single spoken
        // date instead of three disconnected fragments.
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(accessibilityDate)
        .accessibilityAddTraits(.isButton)
        .accessibilityAddTraits(isSelected ? .isSelected : [])
    }

    private var components: (year: String, month: Int, day: String) {
        // YYYY-MM-DD is the canonical filename stem. Defensive — return empty
        // strings if anything is off so we never crash a chip.
        let parts = date.split(separator: "-")
        guard parts.count == 3 else { return ("", 1, "") }
        return (String(parts[0]), Int(parts[1]) ?? 1, String(parts[2]))
    }

    private var dayNumber: String { components.day }

    private static let monthFormatter: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale.current
        f.dateFormat = "MMM"
        return f
    }()

    private static let weekdayLetterFormatter: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale.current
        f.dateFormat = "EEEEE" // single-letter weekday
        return f
    }()

    private static let accessibilityFormatter: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale.current
        f.dateStyle = .full
        return f
    }()

    private var parsedDate: Date? {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withFullDate]
        return formatter.date(from: date)
    }

    private var monthShort: String {
        guard let d = parsedDate else {
            let m = max(1, min(12, components.month)) - 1
            let fallback = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
                            "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
            return fallback[m]
        }
        return Self.monthFormatter.string(from: d).uppercased()
    }

    private var weekday: String {
        guard let d = parsedDate else { return "" }
        return Self.weekdayLetterFormatter.string(from: d).uppercased()
    }

    // Spelled-out date for VoiceOver, e.g. "samedi 16 juin 2026". Falls back
    // to the raw stem if the date can't be parsed.
    private var accessibilityDate: String {
        guard let d = parsedDate else { return date }
        return Self.accessibilityFormatter.string(from: d)
    }
}

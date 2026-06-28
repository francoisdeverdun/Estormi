import SwiftUI

// Read-only catalogue of every registered connector with its full parameter
// set. Mirrors the macOS SourcesPanel, but the phone never mutates: each row
// expands to reveal the source's live config (enabled, historic depth,
// filesystem root, last watermark) joined to its static spec metadata.

struct SourcesCatalogCard: View {
    let sources: [VaultSourceInfo]

    var body: some View {
        GildedPanel(tone: .neutral) {
            VStack(alignment: .leading, spacing: 12) {
                HStack(alignment: .firstTextBaseline) {
                    Text("SOURCES")
                        .font(EstormiFont.display(11, bold: true))
                        .tracking(3.4)
                        .foregroundStyle(EstormiColor.orSombre)
                    Spacer()
                    Text("\(sources.count)")
                        .font(EstormiFont.display(13, bold: true))
                        .foregroundStyle(EstormiColor.orClair)
                }
                if sources.isEmpty {
                    Text("No sources recorded yet.")
                        .font(EstormiTypeScale.bodySmall)
                        .foregroundStyle(EstormiColor.parchemin.opacity(0.6))
                } else {
                    ForEach(Array(sources.enumerated()), id: \.element.id) { index, source in
                        if index > 0 {
                            Rectangle()
                                .fill(EstormiColor.orAncien.opacity(0.14))
                                .frame(height: 0.5)
                        }
                        SourceRow(source: source)
                    }
                }
            }
        }
    }
}

private struct SourceRow: View {
    let source: VaultSourceInfo
    @State private var expanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Button {
                withAnimation(.easeInOut(duration: EstormiMetric.Motion.fast)) {
                    expanded.toggle()
                }
            } label: {
                HStack(spacing: 10) {
                    Circle()
                        .fill(
                            (source.enabled ?? false)
                                ? EstormiColor.vertSauge : EstormiColor.orSombre
                        )
                        .frame(width: 7, height: 7)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(source.title ?? source.name)
                            .font(EstormiTypeScale.bodyLarge)
                            .foregroundStyle(EstormiColor.parcheminOs)
                        Text(source.name)
                            .font(EstormiFont.body(11))
                            .foregroundStyle(EstormiColor.parchemin.opacity(0.5))
                    }
                    Spacer()
                    Text("\(source.chunks ?? 0)")
                        .font(EstormiFont.display(13, bold: true))
                        .foregroundStyle(EstormiColor.orClair)
                    Image(systemName: expanded ? "chevron.up" : "chevron.down")
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(EstormiColor.orSombre)
                }
            }
            .buttonStyle(.plain)

            if expanded {
                VStack(alignment: .leading, spacing: 6) {
                    if let desc = source.description, !desc.isEmpty {
                        Text(desc)
                            .font(EstormiFont.body(13, italic: true))
                            .foregroundStyle(EstormiColor.parchemin.opacity(0.75))
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    param("Status", (source.enabled ?? false) ? "Enabled" : "Disabled")
                    if let depth = source.historicDepth {
                        param("Historic depth", depth.uppercased())
                    }
                    if let env = source.depthWindowEnv {
                        param("Depth env", env)
                    }
                    if let root = source.root, !root.isEmpty {
                        param("Root", root)
                    }
                    if let last = source.lastFetchedAt {
                        param("Last sync", EstormiDate.relative(last))
                    }
                    if let perms = source.permissions, !perms.isEmpty {
                        param("Permissions", perms.joined(separator: ", "))
                    }
                    param("Watermarked", (source.usesWatermark ?? false) ? "Yes" : "No")
                    param("Needs root", (source.requiresRoot ?? false) ? "Yes" : "No")
                    if source.dagStage ?? false {
                        param("Pipeline order", "\(source.dagOrder ?? 0)")
                    }
                }
                .padding(.leading, 17)
                .transition(.opacity)
            }
        }
        .padding(.vertical, 2)
    }

    private func param(_ label: String, _ value: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(label.uppercased())
                .font(EstormiFont.display(9, bold: true))
                .tracking(1.6)
                .foregroundStyle(EstormiColor.orSombre)
                .frame(width: 104, alignment: .leading)
            Text(value)
                .font(EstormiTypeScale.bodySmall)
                .foregroundStyle(EstormiColor.parcheminOs)
                .frame(maxWidth: .infinity, alignment: .leading)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

import SwiftUI

// Slim gilt "listen" bar that sits between the date strip's gold rule and the
// briefing body. At rest it offers a play control and the narration's length;
// while playing it becomes a gold progress filet with a time readout and a
// speed toggle. Driven by BriefingAudioPlayer (plays the Mac-synthesized .m4a
// from the vault). Shown only when the briefing has narration audio.

struct BriefingAudioBar: View {
    @ObservedObject var player: BriefingAudioPlayer

    // While the user drags the filet we preview the target position locally and
    // only commit the seek on release. nil when not scrubbing.
    @State private var scrubFraction: Double?

    /// Fraction shown by the filet and time readout — the live drag preview if
    /// scrubbing, otherwise the player's real progress.
    private var shownFraction: Double { scrubFraction ?? player.progress }

    var body: some View {
        HStack(spacing: 12) {
            playButton
            if player.mode == .idle {
                idleLabel
            } else {
                progressFilet
                timeReadout
                speedToggle
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 9)
        .background(
            RoundedRectangle(cornerRadius: EstormiMetric.radiusTight, style: .continuous)
                .fill(EstormiColor.orAncien.opacity(0.10))
                .overlay(
                    RoundedRectangle(
                        cornerRadius: EstormiMetric.radiusTight, style: .continuous
                    )
                    .stroke(EstormiColor.orSombre.opacity(0.35), lineWidth: 0.5)
                )
        )
        .padding(.horizontal, 20)
        .padding(.vertical, 8)
        .animation(.easeInOut(duration: EstormiMetric.Motion.medium), value: player.mode)
    }

    // MARK: - Play / pause

    private var playButton: some View {
        Button(action: player.toggle) {
            Image(systemName: player.mode == .playing ? "pause.fill" : "play.fill")
                .font(.system(size: 13, weight: .bold))
                .foregroundStyle(EstormiColor.orClair)
                .frame(width: 30, height: 30)
                .background(
                    Circle().fill(EstormiColor.orAncien.opacity(0.14))
                )
                .overlay(
                    Circle().stroke(EstormiColor.orAncien.opacity(0.55), lineWidth: 0.75)
                )
        }
        .buttonStyle(.plain)
        .accessibilityLabel(player.mode == .playing ? "Pause" : "Listen to the briefing")
    }

    // MARK: - Idle

    private var idleLabel: some View {
        HStack(spacing: 0) {
            Text("Listen to the briefing")
                .font(EstormiFont.display(12, bold: true))
                .tracking(1.5)
                .foregroundStyle(EstormiColor.orClair)
            Spacer(minLength: 8)
            Text(durationText)
                .font(EstormiFont.body(12))
                .foregroundStyle(EstormiColor.orSombre)
        }
    }

    private var durationText: String {
        let secs = player.duration
        guard secs > 0 else { return "" }
        let minutes = max(1, Int((secs / 60).rounded()))
        return "~\(minutes) min"
    }

    // MARK: - Playing

    // Draggable gilt scrubber: tap or drag anywhere along it to move the cursor
    // through the narration. The thumb tracks the live drag; the seek is
    // committed to the player on release (which also drives the lock screen).
    private var progressFilet: some View {
        GeometryReader { geo in
            let width = geo.size.width
            let fraction = min(1, max(0, shownFraction))
            ZStack(alignment: .leading) {
                Capsule()
                    .fill(EstormiColor.orSombre.opacity(0.25))
                    .frame(height: 2)
                Capsule()
                    .fill(
                        LinearGradient(
                            colors: [EstormiColor.orSombre, EstormiColor.orClair],
                            startPoint: .leading, endPoint: .trailing)
                    )
                    .frame(width: max(2, width * fraction), height: 2)
                Circle()
                    .fill(EstormiColor.orClair)
                    .frame(width: scrubFraction == nil ? 9 : 12)
                    .overlay(Circle().stroke(EstormiColor.charbon.opacity(0.6), lineWidth: 0.5))
                    .offset(x: width * fraction - (scrubFraction == nil ? 4.5 : 6))
            }
            .frame(maxHeight: .infinity, alignment: .center)
            .contentShape(Rectangle())
            .gesture(
                DragGesture(minimumDistance: 0)
                    .onChanged { value in
                        guard width > 0 else { return }
                        scrubFraction = min(1, max(0, value.location.x / width))
                    }
                    .onEnded { value in
                        guard width > 0 else { return }
                        let target = min(1, max(0, value.location.x / width))
                        player.seek(toFraction: target)
                        scrubFraction = nil
                    }
            )
        }
        .frame(height: 30)
        .animation(scrubFraction == nil ? .linear(duration: EstormiMetric.Motion.medium) : nil,
                   value: player.progress)
        // VoiceOver can't perceive or move the gilt filet, so expose it as a
        // single adjustable element: announce the spoken time ("1:05 of 4:20")
        // and let a swipe up/down seek by 5%, mirroring the drag gesture.
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Narration position")
        .accessibilityValue("\(clock(shownFraction * player.duration)) of \(clock(player.duration))")
        .accessibilityAdjustableAction { direction in
            let step = 0.05
            switch direction {
            case .increment:
                player.seek(toFraction: min(1, player.progress + step))
            case .decrement:
                player.seek(toFraction: max(0, player.progress - step))
            @unknown default:
                break
            }
        }
    }

    private var timeReadout: some View {
        Text("\(clock(shownFraction * player.duration)) / \(clock(player.duration))")
            .font(EstormiFont.display(11, bold: true))
            .foregroundStyle(EstormiColor.orSombre)
            .monospacedDigit()
            .layoutPriority(1)
            // The scrubber already announces position, so skip the redundant
            // readout for VoiceOver (it stays visible on screen).
            .accessibilityHidden(true)
    }

    private var speedToggle: some View {
        Button(action: player.cycleSpeed) {
            Text(speedText)
                .font(EstormiFont.display(11, bold: true))
                .foregroundStyle(EstormiColor.orClair)
                .frame(minWidth: 38)
                .padding(.vertical, 4)
                .background(
                    RoundedRectangle(cornerRadius: EstormiMetric.radiusTight)
                        .stroke(EstormiColor.orSombre.opacity(0.4), lineWidth: 0.5)
                )
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Playback speed \(speedText)")
    }

    private var speedText: String { BriefingAudioPlayer.label(forSpeed: player.speed) }

    private func clock(_ seconds: Double) -> String {
        let total = Int(seconds.rounded())
        return String(format: "%d:%02d", total / 60, total % 60)
    }
}

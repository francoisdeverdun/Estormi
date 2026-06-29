import AVFoundation
import MediaPlayer
import SwiftUI

// Plays a briefing's narration — the .m4a the Mac synthesized with Voxtral and
// wrote next to the briefing JSON in the iCloud vault (see memory_core/tts_local.py
// and estormi_ingestion/shared/delivery/vault_sync.py). The companion is a read-only viewer: it
// never synthesizes speech itself (the old on-device sherpa-onnx voice is gone),
// it just plays the file the Mac produced.
//
// Backed by a plain AVAudioPlayer over the local file, so position, duration and
// scrubbing are exact rather than estimated. Drives BriefingAudioBar and the
// lock-screen / Control-Center Now Playing transport.
@MainActor
final class BriefingAudioPlayer: NSObject, ObservableObject {
    enum Mode { case idle, playing, paused }

    @Published private(set) var mode: Mode = .idle
    /// 0…1 over the whole clip.
    @Published private(set) var progress: Double = 0
    @Published private(set) var speedIndex = 0
    /// True once a track is loaded — gates the bar's visibility.
    @Published private(set) var isLoaded = false

    /// Playback-speed multipliers offered by the UI.
    static let speeds: [Float] = [1.0, 1.25, 1.5]
    var speed: Float { Self.speeds[speedIndex] }

    /// UserDefaults key for the user's preferred default playback speed,
    /// chosen in Settings and applied to each fresh playback.
    static let defaultSpeedKey = "estormi.playback.defaultSpeedIndex"

    /// The default speed index from Settings, clamped to a valid index (0 when
    /// unset, since `UserDefaults.integer` returns 0 for a missing key).
    static var defaultSpeedIndex: Int {
        min(max(0, UserDefaults.standard.integer(forKey: defaultSpeedKey)), speeds.count - 1)
    }

    /// Human label for a speed multiplier — "1×", "1.25×", "1.5×".
    static func label(forSpeed s: Float) -> String {
        s == s.rounded() ? "\(Int(s))×" : "\(s)×"
    }

    /// Real clip length in seconds (0 until a track is loaded).
    var duration: Double { player?.duration ?? 0 }
    var elapsedSeconds: Double { (player?.currentTime ?? 0) }

    private var player: AVAudioPlayer?
    private var loadedDate: String?
    private var ticker: Timer?
    /// Observer tokens for the audio-session interruption / route-change
    /// notifications, removed in deinit.
    private var sessionObservers: [NSObjectProtocol] = []

    override init() {
        super.init()
        speedIndex = Self.defaultSpeedIndex
        configureRemoteCommands()
        observeAudioSession()
    }

    // MARK: - Loading

    /// Point the player at a briefing's audio file. Resets playback when the day
    /// changes; a no-op when already loaded for the same date.
    func load(url: URL, date: String) {
        guard date != loadedDate else { return }
        stop()
        speedIndex = Self.defaultSpeedIndex
        loadedDate = date
        do {
            let p = try AVAudioPlayer(contentsOf: url)
            p.delegate = self
            p.enableRate = true
            p.rate = speed
            p.prepareToPlay()
            player = p
            isLoaded = true
        } catch {
            player = nil
            isLoaded = false
        }
    }

    /// Forget the current track (the briefing has no audio, or the view is gone).
    func unload() {
        stop()
        player = nil
        isLoaded = false
        loadedDate = nil
    }

    // MARK: - Transport

    func toggle() {
        switch mode {
        // Pick up any change made in Settings since the last fresh playback.
        case .idle: speedIndex = Self.defaultSpeedIndex; start()
        case .playing: pause()
        case .paused: resume()
        }
    }

    func cycleSpeed() {
        speedIndex = (speedIndex + 1) % Self.speeds.count
        player?.rate = speed
        updateNowPlaying()
    }

    /// Jump to a fraction (0…1) of the clip and keep playing from there.
    func seek(toFraction fraction: Double) {
        guard let player else { return }
        let clamped = min(1, max(0, fraction))
        player.currentTime = clamped * player.duration
        progress = clamped
        updateNowPlaying()
    }

    /// Halt and rewind to the start. Called when switching days or leaving.
    func stop() {
        stopTicker()
        player?.stop()
        player?.currentTime = 0
        mode = .idle
        progress = 0
        deactivateSession()
        clearNowPlaying()
    }

    deinit {
        // Player + ticker teardown happens in stop()/unload(), which the owning
        // view calls from onDisappear, so by here they're already torn down.
        // Touching those MainActor-isolated properties from this nonisolated
        // deinit is exactly what Swift 6 strict concurrency forbids — so we only
        // release the shared audio session here, a global, thread-safe call,
        // and drop our notification observers (removeObserver is thread-safe).
        for observer in sessionObservers {
            NotificationCenter.default.removeObserver(observer)
        }
        let center = MPRemoteCommandCenter.shared()
        center.playCommand.removeTarget(nil)
        center.pauseCommand.removeTarget(nil)
        center.togglePlayPauseCommand.removeTarget(nil)
        center.changePlaybackPositionCommand.removeTarget(nil)
        try? AVAudioSession.sharedInstance().setActive(
            false, options: .notifyOthersOnDeactivation)
    }

    // MARK: - Playback core

    private func start() {
        guard let player else { return }
        activateSession()
        player.rate = speed
        player.play()
        mode = .playing
        startTicker()
        updateNowPlaying()
    }

    private func resume() {
        activateSession()
        player?.play()
        mode = .playing
        startTicker()
        updateNowPlaying()
    }

    private func pause() {
        player?.pause()
        mode = .paused
        stopTicker()
        updateNowPlaying()
    }

    private func finish() {
        stopTicker()
        player?.currentTime = 0
        mode = .idle
        progress = 1
        deactivateSession()
        clearNowPlaying()
    }

    // Drive the scrubber/time readout while playing. AVAudioPlayer has no
    // progress callback, so poll a few times a second — cheap, and only while
    // actually playing.
    private func startTicker() {
        stopTicker()
        ticker = Timer.scheduledTimer(withTimeInterval: 0.2, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.tick() }
        }
    }
    private func stopTicker() {
        ticker?.invalidate()
        ticker = nil
    }
    private func tick() {
        guard let player, player.duration > 0 else { return }
        progress = min(1, player.currentTime / player.duration)
    }

    // MARK: - Audio session

    // .playback so the briefing is audible in silent mode and keeps going when
    // the screen locks.
    private func activateSession() {
        let session = AVAudioSession.sharedInstance()
        try? session.setCategory(.playback, mode: .spokenAudio)
        try? session.setActive(true)
    }
    private func deactivateSession() {
        try? AVAudioSession.sharedInstance().setActive(
            false, options: .notifyOthersOnDeactivation)
    }

    // MARK: - Interruptions & route changes

    // A phone call, Siri or another app can interrupt us; unplugging the
    // headphones tears down the old route. Without handling these the player
    // keeps its `.playing` state while no sound comes out, and audio would
    // blast from the speaker when the headset is yanked. Observe both and keep
    // `mode` honest.
    private func observeAudioSession() {
        let center = NotificationCenter.default
        let queue = OperationQueue.main
        sessionObservers.append(
            center.addObserver(
                forName: AVAudioSession.interruptionNotification,
                object: AVAudioSession.sharedInstance(), queue: queue
            ) { [weak self] note in
                Task { @MainActor in self?.handleInterruption(note) }
            })
        sessionObservers.append(
            center.addObserver(
                forName: AVAudioSession.routeChangeNotification,
                object: AVAudioSession.sharedInstance(), queue: queue
            ) { [weak self] note in
                Task { @MainActor in self?.handleRouteChange(note) }
            })
    }

    private func handleInterruption(_ note: Notification) {
        guard
            let raw = note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
            let type = AVAudioSession.InterruptionType(rawValue: raw)
        else { return }
        switch type {
        case .began:
            if mode == .playing { pause() }
        case .ended:
            guard
                let optsRaw = note.userInfo?[AVAudioSessionInterruptionOptionKey] as? UInt
            else { return }
            let options = AVAudioSession.InterruptionOptions(rawValue: optsRaw)
            if options.contains(.shouldResume), mode == .paused { resume() }
        @unknown default:
            break
        }
    }

    private func handleRouteChange(_ note: Notification) {
        guard
            let raw = note.userInfo?[AVAudioSessionRouteChangeReasonKey] as? UInt,
            let reason = AVAudioSession.RouteChangeReason(rawValue: raw)
        else { return }
        // Headphones unplugged (or any old output going away): pause rather than
        // dump the briefing out the speaker.
        if reason == .oldDeviceUnavailable, mode == .playing { pause() }
    }

    // MARK: - Lock-screen Now Playing

    private func configureRemoteCommands() {
        let center = MPRemoteCommandCenter.shared()
        center.playCommand.addTarget { [weak self] _ in
            Task { @MainActor in self?.resume() }
            return .success
        }
        center.pauseCommand.addTarget { [weak self] _ in
            Task { @MainActor in self?.pause() }
            return .success
        }
        center.togglePlayPauseCommand.addTarget { [weak self] _ in
            Task { @MainActor in self?.toggle() }
            return .success
        }
        center.changePlaybackPositionCommand.addTarget { [weak self] event in
            guard let event = event as? MPChangePlaybackPositionCommandEvent else {
                return .commandFailed
            }
            let position = event.positionTime
            Task { @MainActor in
                guard let self, self.duration > 0 else { return }
                self.seek(toFraction: position / self.duration)
            }
            return .success
        }
        // Track skip controls make no sense for a single briefing.
        center.nextTrackCommand.isEnabled = false
        center.previousTrackCommand.isEnabled = false
    }

    private func updateNowPlaying() {
        var info: [String: Any] = [
            MPMediaItemPropertyTitle: loadedDate.map { "Briefing · \($0)" } ?? "Daily Briefing",
            MPMediaItemPropertyArtist: "Estormi · Ars Memoriae",
            MPMediaItemPropertyPlaybackDuration: duration,
            MPNowPlayingInfoPropertyElapsedPlaybackTime: elapsedSeconds,
            MPNowPlayingInfoPropertyPlaybackRate: mode == .playing ? Double(speed) : 0.0,
            MPNowPlayingInfoPropertyDefaultPlaybackRate: 1.0,
        ]
        info[MPNowPlayingInfoPropertyMediaType] = MPNowPlayingInfoMediaType.audio.rawValue
        MPNowPlayingInfoCenter.default().nowPlayingInfo = info
    }

    private func clearNowPlaying() {
        MPNowPlayingInfoCenter.default().nowPlayingInfo = nil
    }
}

extension BriefingAudioPlayer: AVAudioPlayerDelegate {
    nonisolated func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        Task { @MainActor in self.finish() }
    }
}

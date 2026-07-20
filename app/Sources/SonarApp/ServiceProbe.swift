import Foundation

/// Liveness of the two localhost services the popover reports beyond the harness
/// (which the HealthPoller already covers): the voice/bridge WS server and the
/// Notes HTTP server. "Up" just means the port answered — for the WS server a
/// plain GET returns an HTTP error status, which still proves it is listening.
struct ServiceStatus {
    let voiceUp: Bool
    let notesUp: Bool

    static let down = ServiceStatus(voiceUp: false, notesUp: false)
}

/// Polls the voice + notes ports on a timer with URLSession and publishes a
/// combined reading on the main thread. Kept separate from HealthPoller so the
/// harness `/health` decode stays focused; both feed the status popover.
final class ServiceProbe {
    private let voiceURL: URL
    private let notesURL: URL
    private let interval: TimeInterval
    private let session: URLSession
    private var timer: Timer?

    /// Delivered on the main thread on every poll.
    var onUpdate: ((ServiceStatus) -> Void)?

    init(voiceURL: URL, notesURL: URL, interval: TimeInterval = 5.0) {
        self.voiceURL = voiceURL
        self.notesURL = notesURL
        self.interval = interval
        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = 2.5
        cfg.timeoutIntervalForResource = 2.5
        cfg.waitsForConnectivity = false
        self.session = URLSession(configuration: cfg)
    }

    func start() {
        stop()
        let timer = Timer(timeInterval: interval, repeats: true) { [weak self] _ in
            self?.poll()
        }
        RunLoop.main.add(timer, forMode: .common)
        self.timer = timer
        poll()
    }

    func stop() {
        timer?.invalidate()
        timer = nil
    }

    deinit {
        stop()
        session.invalidateAndCancel()
    }

    private func poll() {
        // Fan out both probes, join, publish once. The group is completed on a
        // background queue, then the callback hops to main.
        let group = DispatchGroup()
        var voiceUp = false
        var notesUp = false

        probe(voiceURL, group: group) { voiceUp = $0 }
        probe(notesURL, group: group) { notesUp = $0 }

        let publish = onUpdate
        group.notify(queue: .main) {
            publish?(ServiceStatus(voiceUp: voiceUp, notesUp: notesUp))
        }
    }

    /// A port is "up" if it produced any HTTP response (even a 4xx/426 from a WS
    /// server); a refused/timed-out connection yields no response → down. The
    /// completion is called exactly once per request, inside the group.
    private func probe(_ url: URL, group: DispatchGroup, completion: @escaping (Bool) -> Void) {
        group.enter()
        var request = URLRequest(url: url)
        request.timeoutInterval = 2.5
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.httpMethod = "GET"
        let task = session.dataTask(with: request) { _, response, _ in
            completion(response != nil)
            group.leave()
        }
        task.resume()
    }
}

import Foundation

/// Liveness of the two localhost ports the popover reports beyond the harness
/// (which the HealthPoller already covers): the overlay bridge socket and the
/// Notes HTTP server. "Up" means only that the port answered.
///
/// `bridgeUp` deliberately does NOT mean "the voice loop is running": :8770 has
/// two mutually exclusive owners (overlay/bridge.py, or the voice loop serving
/// the same socket instead of it) and their responses are indistinguishable, so
/// the reading is about the port, not about who holds it.
struct ServiceStatus {
    let bridgeUp: Bool
    let notesUp: Bool

    static let down = ServiceStatus(bridgeUp: false, notesUp: false)
}

/// Polls the bridge + notes ports on a timer with URLSession and publishes a
/// combined reading on the main thread. Kept separate from HealthPoller so the
/// harness `/health` decode stays focused; both feed the status popover.
final class ServiceProbe {
    private let bridgeURL: URL
    private let notesURL: URL
    private let interval: TimeInterval
    private let session: URLSession
    private var timer: Timer?

    /// Delivered on the main thread on every poll.
    var onUpdate: ((ServiceStatus) -> Void)?

    init(bridgeURL: URL, notesURL: URL, interval: TimeInterval = 5.0) {
        self.bridgeURL = bridgeURL
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
        var bridgeUp = false
        var notesUp = false

        probe(bridgeURL, group: group) { bridgeUp = $0 }
        probe(notesURL, group: group) { notesUp = $0 }

        let publish = onUpdate
        group.notify(queue: .main) {
            publish?(ServiceStatus(bridgeUp: bridgeUp, notesUp: notesUp))
        }
    }

    /// A port is "up" if it produced any HTTP response (including the non-200 a
    /// WS server answers a plain GET with); a refused/timed-out connection yields
    /// no response → down. The completion is called exactly once per request,
    /// inside the group.
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

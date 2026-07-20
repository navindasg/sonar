import Foundation

/// Decoded {SONAR_HARNESS_URL}/health payload (harness server.py health()).
private struct HealthPayload: Decodable {
    let status: String
    let tools: [String]
    let chunks: Int
    let default_model: String
}

/// A single readiness reading, published to the status item.
struct HealthSnapshot {
    let up: Bool
    let model: String?
    let toolCount: Int
    let chunkCount: Int

    static let down = HealthSnapshot(up: false, model: nil, toolCount: 0, chunkCount: 0)
}

/// Polls the harness `/health` endpoint on a timer with a native URLSession
/// (no CORS — FastAPI ships none). `onUpdate` is always delivered on the main
/// thread so callers can touch AppKit directly.
final class HealthPoller {
    private let healthURL: URL
    private let interval: TimeInterval
    private let session: URLSession
    private var timer: Timer?

    /// Delivered on the main thread on every poll.
    var onUpdate: ((HealthSnapshot) -> Void)?

    init(healthURL: URL, interval: TimeInterval = 5.0) {
        self.healthURL = healthURL
        self.interval = interval
        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = 3.0
        cfg.timeoutIntervalForResource = 3.0
        cfg.waitsForConnectivity = false
        self.session = URLSession(configuration: cfg)
    }

    func start() {
        stop()
        // Non-scheduling initializer + a single .common registration. (The
        // scheduled variant auto-adds to .default, which is already inside
        // .common, so combining the two double-registers the timer.)
        let timer = Timer(timeInterval: interval, repeats: true) { [weak self] _ in
            self?.poll()
        }
        // Keep firing while a modal/tracking run loop (e.g. a menu) is up.
        RunLoop.main.add(timer, forMode: .common)
        self.timer = timer
        poll() // immediate first reading
    }

    func stop() {
        timer?.invalidate()
        timer = nil
    }

    deinit {
        stop()
        // Break the session's self-retain so it isn't leaked if the poller is
        // ever dropped without an explicit stop().
        session.invalidateAndCancel()
    }

    private func poll() {
        var request = URLRequest(url: healthURL)
        request.timeoutInterval = 3.0
        request.cachePolicy = .reloadIgnoringLocalCacheData
        let task = session.dataTask(with: request) { [weak self] data, response, error in
            guard let self = self else { return }
            let snapshot = HealthPoller.decode(data: data, response: response, error: error)
            let publish = self.onUpdate
            DispatchQueue.main.async {
                publish?(snapshot)
            }
        }
        task.resume()
    }

    private static func decode(data: Data?, response: URLResponse?, error: Error?) -> HealthSnapshot {
        guard error == nil,
              let http = response as? HTTPURLResponse, http.statusCode == 200,
              let data = data,
              let payload = try? JSONDecoder().decode(HealthPayload.self, from: data),
              payload.status == "ok"
        else {
            return .down
        }
        return HealthSnapshot(
            up: true,
            model: payload.default_model,
            toolCount: payload.tools.count,
            chunkCount: payload.chunks
        )
    }
}

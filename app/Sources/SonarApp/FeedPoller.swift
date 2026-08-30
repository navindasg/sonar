import Foundation

/// The two harness readings the popover surfaces beyond liveness, kept
/// independent: coupling them would let one endpoint's state speak for the
/// other's.
struct FeedSnapshot {
    let nudges: NudgeReading
    let activity: ActivityReading

    static let unknown = FeedSnapshot(nudges: .unknown, activity: .unknown)
}

/// Polls `/nudges` and `/events` while the popover is on screen.
///
/// Pull-only, and that is a hard line rather than a default: this never
/// notifies, never speaks, never schedules, never badges, and does not run at
/// all while the popover is closed (AppDelegate gates start()/stop() on popover
/// visibility, exactly as it does for HealthPoller and ServiceProbe). Sonar does
/// not interrupt — see harness/sonar_harness/nudges.py's module header.
final class FeedPoller {
    /// Also the grace window StatusItemController adds to the nudge age ceiling,
    /// so the displayed bound stays valid right up to the next push.
    static let interval: TimeInterval = 5.0

    private let nudgesURL: URL
    private let eventsURL: URL
    private let session: URLSession
    private var timer: Timer?

    /// Delivered on the main thread on every poll.
    var onUpdate: ((FeedSnapshot) -> Void)?

    init(nudgesURL: URL, eventsURL: URL) {
        self.nudgesURL = nudgesURL
        self.eventsURL = eventsURL
        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = 3.0
        cfg.timeoutIntervalForResource = 3.0
        cfg.waitsForConnectivity = false
        // Neither endpoint sends Cache-Control. A re-served cached body would
        // carry age_s = 0.0 while being minutes old, which is precisely the
        // invisible-staleness failure this feature exists to prevent.
        cfg.urlCache = nil
        cfg.requestCachePolicy = .reloadIgnoringLocalCacheData
        self.session = URLSession(configuration: cfg)
    }

    func start() {
        stop()
        let timer = Timer(timeInterval: FeedPoller.interval, repeats: true) { [weak self] _ in
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
        let group = DispatchGroup()
        // Seeded to `.unreachable`, not to a success shape: a task that somehow
        // never invokes its completion must publish "we don't know", never a
        // fabricated reading.
        var nudges: NudgeReading = .unreachable
        var activity: ActivityReading = .unreachable

        group.enter()
        session.dataTask(with: request(nudgesURL)) { data, response, error in
            nudges = NudgeFeed.decode(data: data, response: response, error: error,
                                      at: ContinuousClock.now)
            group.leave()
        }.resume()

        group.enter()
        session.dataTask(with: request(eventsURL)) { data, response, error in
            activity = ActivityFeed.decode(data: data, response: response, error: error)
            group.leave()
        }.resume()

        let publish = onUpdate
        group.notify(queue: .main) {
            publish?(FeedSnapshot(nudges: nudges, activity: activity))
        }
    }

    private func request(_ url: URL) -> URLRequest {
        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.timeoutInterval = 3.0
        request.cachePolicy = .reloadIgnoringLocalCacheData
        return request
    }
}

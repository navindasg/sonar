import Foundation

/// Why the notes backend couldn't be brought up. Carried out to the caller so
/// the app can say what went wrong instead of raising a window at a URL nothing
/// is serving.
enum NotesBackendError: Error {
    /// No Sonar checkout resolved, so there is no `voice/` to run from.
    case repoRootUnknown
    /// voice/ isn't where the resolved checkout says it should be.
    case voiceDirMissing(path: String)
    /// No `uv` at any of the well-known locations.
    case uvNotFound
    /// Process.run() itself failed.
    case spawnFailed(reason: String)
    /// The child launched and then exited before :8771 came up.
    case backendExited

    /// One sentence, naming the concrete cause and the way out.
    var message: String {
        switch self {
        case .repoRootUnknown:
            return "Couldn't locate the Sonar checkout, so there is no voice/ to "
                + "run the notes backend from. Set SONAR_REPO_ROOT, or rebuild "
                + "the app with app/build-app.sh."
        case .voiceDirMissing(let path):
            return "The notes backend needs voice/, but nothing is at \(path). "
                + "Point SONAR_REPO_ROOT at the Sonar checkout."
        case .uvNotFound:
            return "Couldn't find `uv`, which runs the notes backend. Install it "
                + "or set SONAR_UV to its absolute path."
        case .spawnFailed(let reason):
            return "Couldn't launch the notes backend: \(reason)"
        case .backendExited:
            return "The notes backend exited immediately — check Console for the "
                + "`uv run python -m notes` output."
        }
    }
}

/// What `ensureRunning` concluded. `pending` exists because a slow start is not
/// a failure: a cold `uv run` resolves the voice deps before python even starts,
/// and NotesURLWatcher raises the window on its own once notes.url lands.
enum NotesBackendOutcome {
    /// :8771 answered — safe to load the page.
    case ready
    /// Nothing bound before the deadline, but nothing proved it dead either.
    case pending
    /// Concrete, reportable failure.
    case failed(NotesBackendError)
}

/// Spawns the standalone `python -m notes` backend ONLY when :8771 is down, so
/// it never double-binds a live voice loop that already owns the port.
///
/// The child gets SONAR_NOTES_OPEN=0 (keep _publish_url writing notes.url for
/// the watcher, but skip the `open <url>` shell-out so no browser tab races the
/// native window) plus the same seam env the daemons use. `uv` is resolved to
/// an absolute path because a GUI-launched .app has a minimal PATH.
final class NotesBackend {
    /// How long to wait for the child to bind :8771. Generous on purpose — a
    /// cold `uv run` in voice/ resolves mlx/parakeet/silero before python runs,
    /// which routinely beats 10s, and calling that a failure would be a lie.
    private static let bindDeadline: TimeInterval = 30.0
    private static let pollInterval: TimeInterval = 0.5
    /// Loopback probe budget — the request timeout plus a little slack.
    private static let probeTimeout: TimeInterval = 1.5
    private static let probeWait: TimeInterval = 2.0

    private let config: Config
    private let queue = DispatchQueue(label: "com.sonar.app.notes-backend")
    private let lock = NSLock()
    private var process: Process?
    private var terminating = false
    private var childExited = false

    init(config: Config) {
        self.config = config
    }

    private func isTerminating() -> Bool {
        lock.lock(); defer { lock.unlock() }; return terminating
    }

    private func hasChildExited() -> Bool {
        lock.lock(); defer { lock.unlock() }; return childExited
    }

    /// Ensure a notes server is reachable, then report the outcome on the main
    /// thread. If :8771 is already up (e.g. the voice loop) it's reused; only
    /// when down do we spawn the standalone backend and wait for it to bind.
    func ensureRunning(then completion: @escaping (NotesBackendOutcome) -> Void) {
        queue.async { [weak self] in
            guard let self = self else { return }
            func finish(_ outcome: NotesBackendOutcome) {
                DispatchQueue.main.async { completion(outcome) }
            }

            if self.isPortUp() {
                finish(.ready)
                return
            }
            if let error = self.spawn() {
                finish(.failed(error))
                return
            }
            // Wait for the child to bind :8771. Bail early if the app is quitting
            // so terminate() never blocks on this loop, and the moment the child
            // is observed dead so we report that instead of stalling.
            let deadline = Date().addingTimeInterval(NotesBackend.bindDeadline)
            while Date() < deadline {
                if self.isTerminating() { return }
                if self.isPortUp() {
                    finish(.ready)
                    return
                }
                if self.hasChildExited() {
                    finish(.failed(.backendExited))
                    return
                }
                Thread.sleep(forTimeInterval: NotesBackend.pollInterval)
            }
            finish(.pending)
        }
    }

    /// Synchronous loopback probe — runs on `queue`, never the main thread. Any
    /// HTTP answer means the port is bound. `isUp` is only ever read after the
    /// semaphore is signalled (i.e. ordered after the handler's write); on the
    /// timeout path we cancel the task and report down without reading it, so
    /// the two never touch it concurrently.
    private func isPortUp() -> Bool {
        var isUp = false
        let semaphore = DispatchSemaphore(value: 0)
        var request = URLRequest(url: config.notesURL)
        request.timeoutInterval = NotesBackend.probeTimeout
        request.cachePolicy = .reloadIgnoringLocalCacheData
        let task = URLSession.shared.dataTask(with: request) { _, response, error in
            isUp = (error == nil && response is HTTPURLResponse)
            semaphore.signal()
        }
        task.resume()
        guard semaphore.wait(timeout: .now() + NotesBackend.probeWait) == .success else {
            task.cancel()
            return false
        }
        return isUp
    }

    /// Launch the standalone backend. Returns nil when one is running (or was
    /// just started), else the reason it couldn't start.
    private func spawn() -> NotesBackendError? {
        lock.lock(); let alreadyRunning = process?.isRunning ?? false; lock.unlock()
        if alreadyRunning { return nil }
        guard let uv = config.uvPath else {
            NSLog("[Sonar] cannot spawn notes backend: no `uv` on disk (set SONAR_UV)")
            return .uvNotFound
        }
        guard let voiceDir = config.voiceDir else {
            NSLog("[Sonar] cannot spawn notes backend: no Sonar checkout resolved")
            return .repoRootUnknown
        }
        guard FileManager.default.fileExists(atPath: voiceDir.path) else {
            NSLog("[Sonar] cannot spawn notes backend: voice dir not found at %@",
                  voiceDir.path)
            return .voiceDirMissing(path: voiceDir.path)
        }

        let uvURL = URL(fileURLWithPath: uv)
        let task = Process()
        task.executableURL = uvURL
        task.arguments = ["run", "python", "-m", "notes"]
        task.currentDirectoryURL = voiceDir

        var env = ProcessInfo.processInfo.environment
        env["SONAR_NOTES_OPEN"] = "0"
        env["SONAR_NOTES_PORT"] = String(config.notesPort)
        env["SONAR_VAULT_PATH"] = config.vaultPath
        env["SONAR_OLLAMA_URL"] = config.ollamaURL
        // uv (and any tool it shells out to) needs a usable PATH; the GUI app's
        // inherited PATH is minimal, so prepend uv's dir + the usual bins.
        let uvDir = uvURL.deletingLastPathComponent().path
        let extraPath = "\(uvDir):/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
        if let current = env["PATH"], !current.isEmpty {
            env["PATH"] = "\(extraPath):\(current)"
        } else {
            env["PATH"] = extraPath
        }
        task.environment = env

        lock.lock(); childExited = false; lock.unlock()
        // Installed BEFORE run(): a child that dies instantly (unsynced deps,
        // import error) can exit before run() returns, and the handler has to be
        // there to see it. It tolerates a nil/replaced `process` for the same
        // reason — it may fire before the store below happens.
        task.terminationHandler = { [weak self] finished in
            guard let self = self else { return }
            self.lock.lock()
            if self.process === finished { self.process = nil }
            self.childExited = true
            self.lock.unlock()
            NSLog("[Sonar] notes backend exited (status %d)", finished.terminationStatus)
        }

        do {
            try task.run()
            lock.lock()
            if terminating {
                // Raced with app quit — don't leave an orphan child behind.
                lock.unlock()
                task.terminate()
                return nil
            }
            process = task
            lock.unlock()
            NSLog("[Sonar] spawned notes backend: %@ run python -m notes (cwd=%@)",
                  uv, voiceDir.path)
            return nil
        } catch {
            NSLog("[Sonar] failed to spawn notes backend: %@", String(describing: error))
            return .spawnFailed(reason: error.localizedDescription)
        }
    }

    /// Give :8771 back if WE are holding it, and report whether we did.
    ///
    /// Called when the Notes window closes: an idle standalone backend that
    /// squats the port for the app's whole lifetime is what makes a later spoken
    /// "take notes" fail, because the voice loop's own bind then hits EADDRINUSE.
    /// A reused voice-loop server is never touched — `process` is nil unless we
    /// spawned it — and the next Open Notes just spawns a fresh one.
    @discardableResult
    func releaseSpawned() -> Bool {
        lock.lock()
        let task = process
        process = nil
        lock.unlock()
        guard let task = task, task.isRunning else { return false }
        task.terminate()
        return true
    }

    /// Terminate a backend we spawned (no-op if we reused a live voice loop).
    /// Non-blocking: grabs the process under a fast lock and terminates it
    /// directly, so quitting never waits behind the bind poll loop (which bails
    /// on the `terminating` flag).
    func terminate() {
        lock.lock()
        terminating = true
        lock.unlock()
        releaseSpawned()
    }
}

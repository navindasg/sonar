import Foundation

/// Spawns the standalone `python -m notes` backend ONLY when :8771 is down, so
/// it never double-binds a live voice loop that already owns the port.
///
/// The child gets SONAR_NOTES_OPEN=0 (keep _publish_url writing notes.url for
/// the watcher, but skip the `open <url>` shell-out so no browser tab races the
/// native window) plus the same seam env the daemons use. `uv` is resolved to
/// an absolute path because a GUI-launched .app has a minimal PATH.
final class NotesBackend {
    private let config: Config
    private let queue = DispatchQueue(label: "com.sonar.app.notes-backend")
    private let lock = NSLock()
    private var process: Process?
    private var terminating = false

    init(config: Config) {
        self.config = config
    }

    private func isTerminating() -> Bool {
        lock.lock(); defer { lock.unlock() }; return terminating
    }

    /// Ensure a notes server is reachable, then invoke `then` on the main
    /// thread. If :8771 is already up (e.g. the voice loop) it's reused; only
    /// when down do we spawn the standalone backend and wait for it to bind.
    func ensureRunning(then completion: @escaping () -> Void) {
        queue.async { [weak self] in
            guard let self = self else { return }
            if self.isPortUp() {
                DispatchQueue.main.async(execute: completion)
                return
            }
            self.spawn()
            // Give the backend time to bind :8771 (model-free, so quick). The
            // NotesURLWatcher independently raises the window when notes.url is
            // written; this poll just makes Open Notes converge too. Bail early if
            // the app is quitting so terminate() never blocks on this loop.
            for _ in 0..<20 {
                if self.isTerminating() { return }
                if self.isPortUp() { break }
                Thread.sleep(forTimeInterval: 0.5)
            }
            DispatchQueue.main.async(execute: completion)
        }
    }

    /// Synchronous loopback probe — runs on `queue`, never the main thread. Any
    /// HTTP answer means the port is bound.
    private func isPortUp() -> Bool {
        var isUp = false
        let semaphore = DispatchSemaphore(value: 0)
        var request = URLRequest(url: config.notesURL)
        request.timeoutInterval = 1.5
        request.cachePolicy = .reloadIgnoringLocalCacheData
        let task = URLSession.shared.dataTask(with: request) { _, response, error in
            if error == nil, response is HTTPURLResponse {
                isUp = true
            }
            semaphore.signal()
        }
        task.resume()
        _ = semaphore.wait(timeout: .now() + 2.0)
        return isUp
    }

    private func spawn() {
        lock.lock(); let alreadyRunning = process?.isRunning ?? false; lock.unlock()
        if alreadyRunning { return }
        guard let uv = config.uvPath else {
            NSLog("[Sonar] cannot spawn notes backend: no `uv` on disk (set SONAR_UV)")
            return
        }
        guard FileManager.default.fileExists(atPath: config.voiceDir.path) else {
            NSLog("[Sonar] cannot spawn notes backend: voice dir not found at %@",
                  config.voiceDir.path)
            return
        }

        let uvURL = URL(fileURLWithPath: uv)
        let task = Process()
        task.executableURL = uvURL
        task.arguments = ["run", "python", "-m", "notes"]
        task.currentDirectoryURL = config.voiceDir

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

        do {
            try task.run()
            lock.lock()
            if terminating {
                // Raced with app quit — don't leave an orphan child behind.
                lock.unlock()
                task.terminate()
                return
            }
            process = task
            lock.unlock()
            NSLog("[Sonar] spawned notes backend: %@ run python -m notes (cwd=%@)",
                  uv, config.voiceDir.path)
        } catch {
            NSLog("[Sonar] failed to spawn notes backend: %@", String(describing: error))
        }
    }

    /// Terminate a backend we spawned (no-op if we reused a live voice loop).
    /// Non-blocking: grabs the process under a fast lock and terminates it
    /// directly, so quitting never waits behind the up-to-10s spawn poll loop
    /// (which bails on the `terminating` flag).
    func terminate() {
        lock.lock()
        terminating = true
        let task = process
        process = nil
        lock.unlock()
        if let task = task, task.isRunning {
            task.terminate()
        }
    }
}

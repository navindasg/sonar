import Foundation

/// Edge-triggered watcher on `~/.sonar/run/notes.url` — the single "raise the
/// Notes window" signal. NotesController._publish_url writes this file AFTER
/// the :8771 server is already bound, so its appearance/write implies the page
/// is serveable; `onChange(url)` fires and the app raises the window.
///
/// notes.url is never cleaned up and its write isn't atomic, so we do NOT trust
/// stale content at launch: nothing is emitted until a fresh write/create is
/// observed. All source work runs on `queue`; `onChange` is dispatched to the
/// main thread.
final class NotesURLWatcher {
    private let fileURL: URL
    private let dirURL: URL
    private let queue = DispatchQueue(label: "com.sonar.app.notes-url-watcher")

    private var fileSource: DispatchSourceFileSystemObject?
    private var dirSource: DispatchSourceFileSystemObject?
    private var debounce: DispatchWorkItem?

    /// Delivered on the main thread when a fresh URL is observed.
    var onChange: ((URL) -> Void)?

    init(fileURL: URL) {
        self.fileURL = fileURL
        self.dirURL = fileURL.deletingLastPathComponent()
    }

    func start() {
        queue.async { [weak self] in self?.arm() }
    }

    func stop() {
        queue.async { [weak self] in self?.teardown() }
    }

    // MARK: - queue-confined internals

    private func arm() {
        armDir()
        if FileManager.default.fileExists(atPath: fileURL.path) {
            armFile()
        }
        // Edge-triggered: no emit on launch even if notes.url already exists.
    }

    /// Watch the run/ directory so we catch the file first appearing (or being
    /// re-created by a rename-swap) even when it doesn't exist yet at launch.
    private func armDir() {
        guard dirSource == nil else { return }
        // The dir is created lazily by _publish_url; make sure it exists so the
        // vnode watch has something to attach to (harmless mkdir -p).
        try? FileManager.default.createDirectory(at: dirURL, withIntermediateDirectories: true)

        let fd = open(dirURL.path, O_EVTONLY)
        guard fd >= 0 else { return }
        let source = DispatchSource.makeFileSystemObjectSource(
            fileDescriptor: fd, eventMask: [.write], queue: queue)
        source.setEventHandler { [weak self] in
            guard let self = self else { return }
            // Only react when notes.url newly exists and we aren't already
            // watching it — ignores churn from sibling pidfiles in run/.
            if self.fileSource == nil,
               FileManager.default.fileExists(atPath: self.fileURL.path) {
                self.armFile()
                self.scheduleEmit()
            }
        }
        source.setCancelHandler { close(fd) }
        dirSource = source
        source.resume()
    }

    private func armFile() {
        guard fileSource == nil else { return }
        let fd = open(fileURL.path, O_EVTONLY)
        guard fd >= 0 else { return }
        let source = DispatchSource.makeFileSystemObjectSource(
            fileDescriptor: fd,
            eventMask: [.write, .extend, .rename, .delete, .revoke],
            queue: queue)
        source.setEventHandler { [weak self, weak source] in
            guard let self = self, let source = source else { return }
            let flags = source.data
            if flags.contains(.rename) || flags.contains(.delete) || flags.contains(.revoke) {
                // The watched inode is gone (rename-swap / delete). Drop the
                // stale source; if a replacement file already exists, re-arm on
                // it and treat that as a fresh write.
                self.disarmFile()
                if FileManager.default.fileExists(atPath: self.fileURL.path) {
                    self.armFile()
                    self.scheduleEmit()
                }
                // Otherwise the dir watch re-arms when it reappears.
            } else {
                self.scheduleEmit()
            }
        }
        source.setCancelHandler { close(fd) }
        fileSource = source
        source.resume()
    }

    private func disarmFile() {
        fileSource?.cancel() // cancel handler closes the fd exactly once
        fileSource = nil
    }

    private func scheduleEmit() {
        debounce?.cancel()
        let work = DispatchWorkItem { [weak self] in self?.emit() }
        debounce = work
        queue.asyncAfter(deadline: .now() + 0.15, execute: work)
    }

    private func emit() {
        guard let raw = try? String(contentsOf: fileURL, encoding: .utf8) else { return }
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, let url = URL(string: trimmed) else { return }
        let callback = onChange
        DispatchQueue.main.async {
            callback?(url)
        }
    }

    private func teardown() {
        debounce?.cancel()
        debounce = nil
        disarmFile()
        dirSource?.cancel()
        dirSource = nil
    }
}

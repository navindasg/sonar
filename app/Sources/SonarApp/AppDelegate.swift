import AppKit

/// Wires the pieces together on launch and tears down spawned resources on
/// quit. Everything here runs on the main thread (AppKit lifecycle); the async
/// helpers (health poll, URL watch, backend probe) all marshal their callbacks
/// back to main before touching AppKit.
final class AppDelegate: NSObject, NSApplicationDelegate {
    private let config = Config.load()

    private var statusItem: StatusItemController?
    private var notesWindow: NotesWindowController?
    private var watcher: NotesURLWatcher?
    private var health: HealthPoller?
    private var backend: NotesBackend?

    func applicationDidFinishLaunching(_ notification: Notification) {
        let notesWindow = NotesWindowController()
        self.notesWindow = notesWindow

        let backend = NotesBackend(config: config)
        self.backend = backend

        let statusItem = StatusItemController()
        statusItem.onOpenNotes = { [weak self] in self?.openNotes() }
        self.statusItem = statusItem

        let health = HealthPoller(healthURL: config.healthURL)
        health.onUpdate = { [weak self] snapshot in
            self?.statusItem?.setHealth(snapshot)
        }
        health.start()
        self.health = health

        // notes.url appearing/updating means the page is already serveable —
        // raise the window at whatever URL it names.
        let watcher = NotesURLWatcher(fileURL: config.notesUrlFile)
        watcher.onChange = { [weak self] url in
            self?.notesWindow?.show(url: url)
        }
        watcher.start()
        self.watcher = watcher
    }

    /// Status-item "Open Notes": ensure a backend is up (reuse a live voice
    /// loop, else spawn the standalone one), then raise the window.
    private func openNotes() {
        let url = config.notesURL
        backend?.ensureRunning { [weak self] in
            self?.notesWindow?.show(url: url)
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        watcher?.stop()
        health?.stop()
        backend?.terminate()
    }
}

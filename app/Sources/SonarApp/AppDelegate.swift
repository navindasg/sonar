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
    private var services: ServiceProbe?
    private var feeds: FeedPoller?
    private var backend: NotesBackend?

    func applicationDidFinishLaunching(_ notification: Notification) {
        let notesWindow = NotesWindowController()
        self.notesWindow = notesWindow

        let backend = NotesBackend(config: config)
        self.backend = backend

        // Closing the Notes window hands :8771 back: a standalone backend that
        // squats it for the app's lifetime is what makes a later spoken "take
        // notes" fail to bind. Nothing happens when the page is served by a live
        // voice loop we merely reused. Drop the page too — it retries its socket
        // every 1.2s, and a hidden window must not hammer a port we just closed.
        notesWindow.onHide = { [weak self] in
            guard self?.backend?.releaseSpawned() == true else { return }
            self?.notesWindow?.unload()
        }

        let statusItem = StatusItemController()
        statusItem.onOpenNotes = { [weak self] in self?.openNotes() }
        self.statusItem = statusItem

        let health = HealthPoller(healthURL: config.healthURL)
        health.onUpdate = { [weak self] snapshot in
            self?.statusItem?.setHealth(snapshot)
        }
        self.health = health

        // Bridge (:8770) + Notes (:8771) liveness for the popover's stack-status
        // rows; the harness row comes from the health poll above.
        let services = ServiceProbe(bridgeURL: config.bridgeProbeURL, notesURL: config.notesURL)
        services.onUpdate = { [weak self] status in
            self?.statusItem?.setServices(status)
        }
        self.services = services

        // /nudges + /events — the two harness endpoints the popover surfaces.
        // Pull-only: this poller runs ONLY while the popover is on screen, and
        // its results are never turned into a notification, badge, or spoken
        // line. A 163-day-overdue to-do is something you find when you look, not
        // something Sonar interrupts you with.
        let feeds = FeedPoller(nudgesURL: config.nudgesURL, eventsURL: config.eventsURL)
        feeds.onUpdate = { [weak self] snapshot in
            self?.statusItem?.setFeeds(snapshot)
        }
        self.feeds = feeds

        // Poll ONLY while the popover is on screen — its WebView is the sole
        // consumer, so an always-on 5s timer would spend the app's whole
        // lifetime hitting three localhost ports nobody is looking at (and each
        // :8770 GET is a rejected handshake in the voice log). start() re-polls
        // immediately and the status item keeps the last reading, so the popover
        // paints known state at once and refreshes within one round trip.
        statusItem.onPopoverVisibilityChanged = { [weak self] visible in
            guard let self = self else { return }
            if visible {
                self.health?.start()
                self.services?.start()
                self.feeds?.start()
            } else {
                self.health?.stop()
                self.services?.stop()
                self.feeds?.stop()
            }
        }

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
        backend?.ensureRunning { [weak self] outcome in
            switch outcome {
            case .ready:
                self?.notesWindow?.show(url: url)
            case .pending:
                // Slow start, not a failure — NotesURLWatcher raises the window
                // when notes.url lands. Raising it now would show a dead page.
                NSLog("[Sonar] notes backend still starting; waiting for notes.url")
            case .failed(let error):
                self?.presentNotesFailure(error)
            }
        }
    }

    /// The app's one error surface: say why Notes couldn't start instead of
    /// leaving an NSLog line nobody reads (and a blank window nobody can fix).
    private func presentNotesFailure(_ error: NotesBackendError) {
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "Couldn't open Sonar Notes"
        alert.informativeText = error.message
        alert.addButton(withTitle: "OK")
        NSApp.activate(ignoringOtherApps: true)
        alert.runModal()
    }

    func applicationWillTerminate(_ notification: Notification) {
        watcher?.stop()
        health?.stop()
        services?.stop()
        feeds?.stop()
        backend?.terminate()
    }
}

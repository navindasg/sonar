import AppKit
import WebKit

/// Hosts the Notes UI (served at http://127.0.0.1:<port>/) inside a WKWebView.
///
/// The web page's WS + full `{op:...}` protocol run verbatim: the load is
/// same-origin loopback, which NotesServer's Origin gate allows. Closing the
/// window just hides it so the controller (and its live WebView) is reused.
///
/// All methods here must be called on the main thread (AppKit + WebKit).
final class NotesWindowController: NSWindowController, NSWindowDelegate {
    private let webView: WKWebView
    private var loadedURL: URL?

    init() {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        let webView = WKWebView(frame: NSRect(x: 0, y: 0, width: 900, height: 680),
                                configuration: configuration)
        webView.autoresizingMask = [.width, .height]
        self.webView = webView

        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 900, height: 680),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Sonar Notes"
        window.contentView = webView
        window.isReleasedWhenClosed = false
        window.setFrameAutosaveName("SonarNotesWindow")
        window.center()

        super.init(window: window)
        window.delegate = self
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("NotesWindowController is not created from a nib/storyboard")
    }

    /// Load `url` into the WebView (only if it changed), then raise + focus the
    /// window. Idempotent — safe to call from both the watcher and Open Notes.
    /// URLs are compared canonically so the trailing-slash difference between
    /// config.notesURL (…:8771/) and the notes.url the backend writes (…:8771)
    /// doesn't trigger a needless reload that drops the live WebSocket.
    func show(url: URL) {
        if loadedURL.map(Self.canonical) != Self.canonical(url) {
            webView.load(URLRequest(url: url))
            loadedURL = url
        }
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    /// Canonical form for equivalence: drop trailing slashes so "…/" == "…".
    private static func canonical(_ url: URL) -> String {
        var s = url.absoluteString
        while s.hasSuffix("/") { s.removeLast() }
        return s
    }

    // MARK: NSWindowDelegate

    /// Hide instead of destroy so the WebView (and any in-progress session)
    /// survives a window close and the next Open Notes reuses it.
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        sender.orderOut(nil)
        return false
    }
}

import AppKit
import WebKit

/// Hosts the Notes UI (served at http://127.0.0.1:<port>/) inside a WKWebView.
///
/// The web page's WS + full `{op:...}` protocol run verbatim: the load is
/// same-origin loopback, which NotesServer's Origin gate allows. Closing the
/// window just hides it so the controller (and its live WebView) is reused.
///
/// All methods here must be called on the main thread (AppKit + WebKit).
final class NotesWindowController: NSWindowController, NSWindowDelegate, WKNavigationDelegate {
    private let webView: WKWebView
    /// Last URL whose load actually COMMITTED — a load that failed is never
    /// recorded, so the next show() retries instead of raising a blank window.
    private var loadedURL: URL?
    /// Load in flight; dedupes repeat show() calls during a slow load.
    private var pendingURL: URL?

    /// Fired on the main thread when the window is closed (hidden), so the
    /// owner can release resources standing behind the page.
    var onHide: (() -> Void)?

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
        webView.navigationDelegate = self
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
        let key = Self.canonical(url)
        if loadedURL.map(Self.canonical) != key, pendingURL.map(Self.canonical) != key {
            pendingURL = url
            webView.load(URLRequest(url: url))
        }
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    /// Drop the page and forget the loaded URL, so the next `show(url:)` does a
    /// real load. Called when the server behind the page is going away: the UI
    /// retries its WebSocket every 1.2s, and a hidden window must not sit there
    /// hammering a dead port for the rest of the app's life.
    func unload() {
        pendingURL = nil
        loadedURL = nil
        webView.loadHTMLString("", baseURL: nil)
    }

    /// Canonical form for equivalence: drop trailing slashes so "…/" == "…".
    private static func canonical(_ url: URL) -> String {
        var s = url.absoluteString
        while s.hasSuffix("/") { s.removeLast() }
        return s
    }

    // MARK: WKNavigationDelegate

    /// Only a committed load counts: `loadedURL` is the guard that suppresses a
    /// needless reload, so recording a URL the WebView never actually rendered
    /// would pin a blank window for the life of the process.
    func webView(_ webView: WKWebView, didCommit navigation: WKNavigation!) {
        guard let pending = pendingURL else { return }
        loadedURL = pending
        pendingURL = nil
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        handleLoadFailure(error)
    }

    func webView(_ webView: WKWebView,
                 didFailProvisionalNavigation navigation: WKNavigation!,
                 withError error: Error) {
        handleLoadFailure(error)
    }

    /// Forget a load that never committed so the next show() (Open Notes, or the
    /// watcher when notes.url is written) retries it. A cancelled navigation was
    /// superseded by a newer load — clearing state there would wipe the key that
    /// load just set and cause the spurious reload the guard exists to prevent.
    private func handleLoadFailure(_ error: Error) {
        guard (error as NSError).code != NSURLErrorCancelled else { return }
        pendingURL = nil
        loadedURL = nil
    }

    // MARK: NSWindowDelegate

    /// Hide instead of destroy so the WebView is reused by the next Open Notes.
    /// `onHide` lets the owner tear down whatever stands behind the page (see
    /// AppDelegate, which releases a notes backend it spawned).
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        sender.orderOut(nil)
        onHide?()
        return false
    }
}

import AppKit
import WebKit

/// A borderless panel that can hold the keyboard.
///
/// `NSWindow.canBecomeKey` is false for `.borderless`, so without this override
/// the bar renders and can never be typed into. This is the whole trick behind a
/// Spotlight-style overlay.
final class CommandPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    /// Never main: the bar is an accessory surface and must not present itself
    /// as the app's primary window.
    override var canBecomeMain: Bool { false }
}

/// The native F5 command bar: a floating panel hosting the same Gotham Noir page
/// the Hammerspoon overlay shows, wired to the `:8770` bridge.
///
/// FOCUS MODEL, established by spike rather than assumption. A
/// `.nonactivatingPanel` becomes key inside its own app WITHOUT changing the
/// frontmost application — but keys typed by the user still go to the frontmost
/// app, because "key window in my app" is not "holds the system keyboard focus".
/// To actually receive typed input the app must activate. So the bar does what
/// Spotlight and Raycast do: remember who was frontmost, activate, and hand
/// activation BACK on dismiss. Measured end to end — keystrokes land in the
/// WKWebView and the previous app is restored.
final class CommandBarController: NSObject, WKScriptMessageHandler, WKNavigationDelegate {
    private let panel: CommandPanel
    /// The page handle. Internal rather than private so integration tests can
    /// read the rendered DOM back — this is an executable module with no
    /// external consumers, and the alternative is test-only API on the class.
    let webView: WKWebView
    private let bridge: BridgeClient

    /// Whoever held activation when the bar opened, so it can be given back.
    private var previousApp: NSRunningApplication?
    private var pageReady = false
    private var didAllowInitialLoad = false
    /// Deduped like the Lua overlay's `M.pushState`: the state stream runs at
    /// ~10/s and pushing identical renders would spam evaluateJavaScript.
    private var lastState: String?

    private static let margin: CGFloat = 22
    private static let topInset: CGFloat = 40

    init(bridgeURL: URL) {
        panel = CommandPanel(
            contentRect: NSRect(x: 0, y: 0,
                                width: CGFloat(CommandBarHTML.width),
                                height: CGFloat(CommandBarHTML.height)),
            styleMask: [.nonactivatingPanel, .borderless],
            backing: .buffered,
            defer: false
        )

        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        webView = WKWebView(frame: panel.contentLayoutRect, configuration: configuration)
        bridge = BridgeClient(url: bridgeURL)

        super.init()

        // Sit above normal windows, follow the user across spaces, and never
        // hide when the app deactivates — the bar is dismissed explicitly.
        panel.level = .floating
        panel.isFloatingPanel = true
        panel.hidesOnDeactivate = false
        panel.backgroundColor = .clear
        panel.isOpaque = false
        panel.hasShadow = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]

        webView.autoresizingMask = [.width, .height]
        // Let the page's own dark glass show instead of a white webview plate.
        webView.setValue(false, forKey: "drawsBackground")
        webView.navigationDelegate = self
        // Must go on the webView's OWN configuration: WKWebView COPIES the
        // configuration passed to its initializer, so registering on the
        // pre-copy object silently no-ops and no message ever arrives.
        webView.configuration.userContentController.add(
            WeakBarMessageHandler(self), name: "sonar")
        panel.contentView = webView

        bridge.onEvent = { [weak self] event in self?.apply(event) }
        bridge.onConnectionChange = { [weak self] connected in
            guard let self = self, !connected else { return }
            self.evalBar("window.sonar && sonar.setBusy(false)")
        }

        webView.loadHTMLString(CommandBarHTML.html, baseURL: nil)
    }

    // MARK: Show / hide

    var isVisible: Bool { panel.isVisible }

    func toggle() { isVisible ? dismiss() : show() }

    func show() {
        positionOnActiveScreen()

        // Capture BEFORE activating, or we record ourselves.
        if let front = NSWorkspace.shared.frontmostApplication,
           front.processIdentifier != ProcessInfo.processInfo.processIdentifier {
            previousApp = front
        }

        bridge.connect()
        // Start every session blank. The socket stays connected after a dismiss
        // (so the next summon is instant), and when the VOICE LOOP owns :8770 it
        // BROADCASTS display events to every client rather than replying only to
        // the asker — so a hidden bar can accumulate a turn it was never part of.
        // Clearing on show means the bar never opens onto someone else's answer.
        lastState = nil
        evalBar("window.sonar && sonar.clearTurn()")
        evalBar("window.sonar && sonar.setBusy(false)")
        evalBar("window.sonar && sonar.setState('idle')")

        NSApp.activate(ignoringOtherApps: true)
        panel.makeKeyAndOrderFront(nil)
        panel.makeFirstResponder(webView)
        evalBar("window.focusCmd && focusCmd()")
        bridge.send(command: "start")
    }

    /// Esc is advertised as "close" in the bar footer, so it must FULLY dismiss:
    /// hide the panel, tell the bridge the box closed, and hand the user's app
    /// back. Leaving activation with a hidden bar is what makes a hotkey overlay
    /// feel like it stole the window.
    func dismiss() {
        guard panel.isVisible else { return }
        bridge.send(command: "stop")
        panel.orderOut(nil)
        lastState = nil
        evalBar("window.sonar && sonar.clearTurn()")
        previousApp?.activate()
        previousApp = nil
    }

    /// Top-right, just under the menu bar, on whichever screen has the mouse —
    /// matching `barRect()` in spike/glow/init.lua.
    private func positionOnActiveScreen() {
        let mouse = NSEvent.mouseLocation
        let screen = NSScreen.screens.first { NSMouseInRect(mouse, $0.frame, false) }
            ?? NSScreen.main
        guard let frame = screen?.frame else { return }
        let size = panel.frame.size
        panel.setFrameOrigin(NSPoint(
            x: frame.maxX - size.width - CommandBarController.margin,
            y: frame.maxY - size.height - CommandBarController.topInset
        ))
    }

    /// The page reports its document height; grow upward so the bar stays
    /// pinned under the menu bar instead of walking down the screen.
    private func resize(toHeight height: CGFloat) {
        let clamped = min(max(height, 80), 620)
        guard abs(clamped - panel.frame.height) > 0.5 else { return }
        var frame = panel.frame
        frame.origin.y += frame.height - clamped
        frame.size.height = clamped
        panel.setFrame(frame, display: true)
    }

    // MARK: Bridge -> page

    private func apply(_ event: BridgeEvent) {
        switch event {
        case .turn(let start):
            evalBar("window.sonar && sonar.setBusy(\(start ? "true" : "false"))")
        case .state(let state, let level):
            if state != lastState {
                lastState = state
                evalBar("window.sonar && sonar.setState(\(jsString(state)))")
            }
            if let level = level {
                evalBar(String(format: "window.sonar && sonar.setLevel(%.3f)", level))
            }
        case .step(let kind, let tool, let detail, let status):
            let label = detail.isEmpty ? status : detail
            evalBar("window.sonar && sonar.addStep(\(jsString(kind)), \(jsString(tool)), \(jsString(label)))")
        case .answer(let delta):
            evalBar("window.sonar && sonar.appendAnswer(\(jsString(delta)))")
        }
    }

    /// A JSON-encoded string is a valid JS string literal, which is what makes
    /// this safe for arbitrary model and vault text — no hand-rolled escaping.
    /// U+2028/U+2029 are legal in ES2019 literals (verified against
    /// JavaScriptCore) but are escaped anyway so the seam survives being inlined
    /// into a <script> block later.
    private func jsString(_ value: String) -> String {
        guard let data = try? JSONEncoder().encode(value),
              let encoded = String(data: data, encoding: .utf8) else { return "\"\"" }
        return encoded
            .replacingOccurrences(of: "\u{2028}", with: "\\u2028")
            .replacingOccurrences(of: "\u{2029}", with: "\\u2029")
    }

    private func evalBar(_ js: String) {
        guard pageReady else { return }
        webView.evaluateJavaScript(js) { _, error in
            if let error = error { NSLog("[Sonar] bar eval failed: \(error)") }
        }
    }

    // MARK: WKNavigationDelegate

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        pageReady = true
    }

    /// Allow the initial `loadHTMLString`, cancel everything after. The page is
    /// static and has no links; this keeps a drifted WebView — after which every
    /// eval silently no-ops and the bar looks frozen — unreachable.
    func webView(_ webView: WKWebView,
                 decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if !didAllowInitialLoad {
            didAllowInitialLoad = true
            decisionHandler(.allow)
            return
        }
        decisionHandler(.cancel)
    }

    // MARK: Page -> host

    func userContentController(_ ucc: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let body = message.body as? String else { return }

        if body == "__esc__" {
            dismiss()
            return
        }
        if body.hasPrefix("__h__:"),
           let height = Double(body.dropFirst("__h__:".count)) {
            resize(toHeight: CGFloat(height))
            return
        }

        // Anything else is a typed question. The page has already echoed it and
        // set itself busy, so a failure here must clear that — otherwise the bar
        // spins forever on a turn that was never sent.
        if !bridge.send(text: body) {
            evalBar("window.sonar && sonar.appendAnswer(\(jsString("[bridge not connected — start overlay/bridge.py]")))")
            evalBar("window.sonar && sonar.setBusy(false)")
        }
    }
}

/// Breaks the config -> userContentController -> handler retain cycle the
/// WKWebView would otherwise pin forever.
private final class WeakBarMessageHandler: NSObject, WKScriptMessageHandler {
    weak var delegate: WKScriptMessageHandler?
    init(_ delegate: WKScriptMessageHandler) { self.delegate = delegate }
    func userContentController(_ ucc: WKUserContentController, didReceive message: WKScriptMessage) {
        delegate?.userContentController(ucc, didReceive: message)
    }
}

import AppKit
import WebKit

/// Breaks the retain cycle a WKUserContentController would otherwise create:
/// the config → userContentController → messageHandler chain is strongly held
/// by the WKWebView, so registering `self` directly would pin the controller
/// forever. This proxy holds the real handler weakly.
private final class WeakScriptMessageHandler: NSObject, WKScriptMessageHandler {
    weak var delegate: WKScriptMessageHandler?
    init(_ delegate: WKScriptMessageHandler) { self.delegate = delegate }
    func userContentController(_ ucc: WKUserContentController, didReceive message: WKScriptMessage) {
        delegate?.userContentController(ucc, didReceive: message)
    }
}

/// The live data the popover renders: harness `/health` plus the two probed
/// services. Immutable snapshot recomputed and pushed on every reading.
private struct PopoverPayload: Encodable {
    let harnessUp: Bool
    let voiceUp: Bool
    let notesUp: Bool
    let model: String
    let tools: Int
    let chunks: Int
}

/// Owns the NSStatusItem (template SF Symbol button) and a transient NSPopover
/// whose content is a WKWebView rendering the Gotham Noir popover (PopoverHTML).
/// Primary entry point since the app is `.accessory` (no Dock menu). Main-thread
/// only (AppKit + WebKit).
final class StatusItemController: NSObject, NSPopoverDelegate, WKScriptMessageHandler, WKNavigationDelegate {
    private let statusItem: NSStatusItem
    private let popover = NSPopover()
    private let webView: WKWebView
    private let contentController = NSViewController()

    /// Invoked on the main thread when Open Notes is chosen.
    var onOpenNotes: (() -> Void)?

    // Latest readings; either can arrive first, so we keep both and push the
    // merged payload whenever one updates (and once the page is ready).
    private var health: HealthSnapshot = .down
    private var services: ServiceStatus = .down
    private var pageReady = false

    // Popover height sizing: seeded, then driven by the page's reported height.
    private static let popoverWidth = CGFloat(PopoverHTML.width)
    private var popoverHeight: CGFloat = 300

    override init() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)

        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        let webView = WKWebView(
            frame: NSRect(x: 0, y: 0, width: StatusItemController.popoverWidth, height: 300),
            configuration: configuration
        )
        webView.autoresizingMask = [.width, .height]
        webView.wantsLayer = true
        // Match the page background so there is no white flash before first paint.
        webView.layer?.backgroundColor = NSColor(calibratedRed: 0.012, green: 0.020, blue: 0.027, alpha: 1).cgColor
        self.webView = webView

        super.init()

        // Register the click/height bridge through the weak proxy. This MUST go
        // on the webView's OWN configuration — WKWebView copies the config passed
        // to its initializer, so adding to the original `configuration` here would
        // silently no-op and the button/height messages would never arrive.
        webView.configuration.userContentController.add(WeakScriptMessageHandler(self), name: "sonar")
        webView.navigationDelegate = self

        contentController.view = webView

        popover.behavior = .transient
        popover.animates = true
        popover.appearance = NSAppearance(named: .darkAqua)
        popover.contentViewController = contentController
        popover.contentSize = NSSize(width: StatusItemController.popoverWidth, height: popoverHeight)
        popover.delegate = self

        webView.loadHTMLString(PopoverHTML.html, baseURL: nil)

        if let button = statusItem.button {
            let image = NSImage(systemSymbolName: "waveform",
                                accessibilityDescription: "Sonar")
            image?.isTemplate = true
            button.image = image
            button.imagePosition = .imageOnly
            button.target = self
            button.action = #selector(togglePopover)
            button.toolTip = "Sonar"
        }
    }

    // MARK: Live data

    /// Push a fresh harness health reading (safe whether the popover is shown).
    func setHealth(_ snapshot: HealthSnapshot) {
        health = snapshot
        pushPayload()
    }

    /// Push a fresh voice/notes liveness reading.
    func setServices(_ status: ServiceStatus) {
        services = status
        pushPayload()
    }

    private func pushPayload() {
        guard pageReady else { return }
        let payload = PopoverPayload(
            harnessUp: health.up,
            voiceUp: services.voiceUp,
            notesUp: services.notesUp,
            model: health.model ?? "?",
            tools: health.toolCount,
            chunks: health.chunkCount
        )
        guard let data = try? JSONEncoder().encode(payload),
              let json = String(data: data, encoding: .utf8) else { return }
        webView.evaluateJavaScript("window.sonarPopover && window.sonarPopover.apply(\(json))",
                                   completionHandler: nil)
    }

    // MARK: WKNavigationDelegate

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        pageReady = true
        pushPayload() // flush whatever we already have
    }

    // MARK: WKScriptMessageHandler

    func userContentController(_ ucc: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let body = message.body as? String else { return }
        switch body {
        case "open-notes":
            popover.performClose(nil)
            onOpenNotes?()
        case "quit":
            NSApp.terminate(nil)
        default:
            if let heightString = body.split(separator: ":", maxSplits: 1).last,
               body.hasPrefix("__h__:"),
               let height = Double(heightString) {
                resizePopover(to: CGFloat(height))
            }
        }
    }

    private func resizePopover(to height: CGFloat) {
        let clamped = min(max(height, 200), 640)
        guard abs(clamped - popoverHeight) > 0.5 else { return }
        popoverHeight = clamped
        popover.contentSize = NSSize(width: StatusItemController.popoverWidth, height: clamped)
    }

    // MARK: Toggle

    @objc private func togglePopover() {
        guard let button = statusItem.button else { return }
        if popover.isShown {
            popover.performClose(nil)
        } else {
            popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
            NSApp.activate(ignoringOtherApps: true)
        }
    }
}

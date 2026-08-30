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

/// The live data the popover renders: harness `/health`, the two probed
/// services, and the harness's own `/nudges` + `/events` readings. Immutable
/// snapshot recomputed and pushed on every reading.
private struct PopoverPayload: Encodable {
    let harnessUp: Bool
    let bridgeUp: Bool
    let notesUp: Bool
    let model: String
    let tools: Int
    let chunks: Int
    let nudges: NudgeSection
    let activity: ActivitySection
}

/// `items` is EMPTY unless `state == "ok"`. Stale, unreachable, malformed and
/// unknown readings are serialized without rows, so the page has nothing to
/// render in those states even if a future renderer tried to.
private struct NudgeSection: Encodable {
    /// unknown | unreachable | malformed | stale | empty | ok
    let state: String
    let items: [NudgeRow]
    /// 0 unless state == "ok".
    let returned: Int
    /// -1 when we have no honest bound.
    let ageCeilingS: Int
}

private struct NudgeRow: Encodable {
    /// Allowlisted by the renderer; never used as markup.
    let severity: String
    let line: String
}

private struct ActivitySection: Encodable {
    /// unknown | unreachable | malformed | empty | ok
    let state: String
    let items: [ActivityRow]
    /// -1 when unknown.
    let newestAgeS: Int
}

private struct ActivityRow: Encodable {
    /// `step`, verbatim; "" when null or absent.
    let kind: String
    /// tool -> step -> turn id. Never invented.
    let label: String
    /// "" when absent or empty; the renderer omits the span rather than filling it.
    let detail: String
    /// Allowlisted downstream.
    let status: String
    /// -1 when the stamp is unusable.
    let ageS: Int
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

    /// Invoked on the main thread when the popover appears (true) / closes
    /// (false). The popover's WebView is the only consumer of the live readings,
    /// so the owner uses this to run the pollers exactly while they're visible.
    var onPopoverVisibilityChanged: ((Bool) -> Void)?

    // Latest readings; either can arrive first, so we keep both and push the
    // merged payload whenever one updates (and once the page is ready).
    private var health: HealthSnapshot = .down
    private var services: ServiceStatus = .down
    private var feeds: FeedSnapshot = .unknown
    private var pageReady = false

    /// The page is a static string with no links and no navigable content. The
    /// first navigation is `loadHTMLString`; every later one is cancelled, so a
    /// WebView that has drifted off PopoverHTML — after which every apply()
    /// silently no-ops and the popover looks frozen on old data — is not
    /// reachable. Tracked as state rather than matched on navigationType/URL so
    /// the load that matters always fails OPEN.
    private var didAllowInitialLoad = false

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

    /// Push a fresh bridge/notes liveness reading.
    func setServices(_ status: ServiceStatus) {
        services = status
        pushPayload()
    }

    /// Push a fresh `/nudges` + `/events` reading.
    func setFeeds(_ snapshot: FeedSnapshot) {
        feeds = snapshot
        pushPayload()
    }

    /// Drop the feed readings back to "unknown" and repaint.
    ///
    /// The WebView and its DOM survive a popover close, and the pollers are
    /// visibility-gated, so without this the previous session's nudges would
    /// repaint as if current until the first poll round-trips. Nudges are the
    /// one thing on this surface that can be WRONG rather than merely old — a
    /// to-do completed since the last look — so the reopen render must be
    /// "Checking…", never last week's list.
    private func beginRefresh() {
        feeds = .unknown
        pushPayload()
    }

    /// Serializes the merged reading. ALL age arithmetic happens here, at push
    /// time, so every number written into the DOM was true at the instant it was
    /// written; drift is bounded by one poll interval, which the age ceiling
    /// already absorbs.
    private func pushPayload() {
        guard pageReady else { return }

        let now = ContinuousClock.now
        let nowMs = ActivityFeed.nowMs()

        // Exhaustive with no `default`, so a state added later without a render
        // is a compile error rather than a silently mis-rendered section.
        let nudgeSection: NudgeSection
        switch feeds.nudges {
        case .unknown:
            nudgeSection = NudgeSection(state: "unknown", items: [], returned: 0, ageCeilingS: -1)
        case .unreachable:
            nudgeSection = NudgeSection(state: "unreachable", items: [], returned: 0, ageCeilingS: -1)
        case .malformed:
            nudgeSection = NudgeSection(state: "malformed", items: [], returned: 0, ageCeilingS: -1)
        case .ok(let snapshot):
            switch snapshot.freshness(now: now, graceS: FeedPoller.interval) {
            case .expired:
                // Past its TTL: we can no longer claim it is current, so the rows
                // are dropped rather than dimmed. This is the enforcement point
                // for "a stale nudge never crosses the wire".
                nudgeSection = NudgeSection(state: "stale", items: [], returned: 0, ageCeilingS: -1)
            case .current(let ceiling):
                let rows = snapshot.items.map { NudgeRow(severity: $0.severity, line: $0.line) }
                nudgeSection = NudgeSection(
                    state: rows.isEmpty ? "empty" : "ok",
                    items: rows,
                    returned: snapshot.returned,
                    ageCeilingS: ceiling
                )
            }
        }

        let activitySection: ActivitySection
        switch feeds.activity {
        case .unknown:
            activitySection = ActivitySection(state: "unknown", items: [], newestAgeS: -1)
        case .unreachable:
            activitySection = ActivitySection(state: "unreachable", items: [], newestAgeS: -1)
        case .malformed:
            activitySection = ActivitySection(state: "malformed", items: [], newestAgeS: -1)
        case .ok(let items):
            let rows = items.map {
                ActivityRow(kind: $0.step,
                            label: $0.label,
                            detail: $0.detail,
                            status: $0.status,
                            ageS: ActivityFeed.ageS(of: $0.tsMs, nowMs: nowMs))
            }
            activitySection = ActivitySection(
                state: rows.isEmpty ? "empty" : "ok",
                items: rows,
                newestAgeS: rows.first?.ageS ?? -1
            )
        }

        let payload = PopoverPayload(
            harnessUp: health.up,
            bridgeUp: services.bridgeUp,
            notesUp: services.notesUp,
            model: health.model ?? "?",
            tools: health.toolCount,
            chunks: health.chunkCount,
            nudges: nudgeSection,
            activity: activitySection
        )

        guard let data = try? JSONEncoder().encode(payload),
              var json = String(data: data, encoding: .utf8) else { return }

        // JSONEncoder does not escape U+2028/U+2029, and this JSON is
        // interpolated into JS SOURCE. They are legal in ES2019 string literals
        // so nothing breaks today (verified against JavaScriptCore), but nudge
        // text now comes from the vault and can carry either verbatim — escape
        // them so the seam stays safe if this is ever inlined into a <script>
        // block instead of passed to evaluateJavaScript.
        json = json.replacingOccurrences(of: "\u{2028}", with: "\\u2028")
                   .replacingOccurrences(of: "\u{2029}", with: "\\u2029")

        webView.evaluateJavaScript("window.sonarPopover && window.sonarPopover.apply(\(json))") { _, error in
            // A JS exception inside apply() aborts the remaining renders. On a
            // data-honesty surface that means a half-painted popover, so it must
            // not fail silently.
            if let error = error {
                NSLog("[Sonar] popover apply failed: \(error)")
            }
        }
    }

    // MARK: NSPopoverDelegate

    func popoverWillShow(_ notification: Notification) {
        // Invariant lives here, not in the subscriber: this class owns a WebView
        // that outlives the popover, so a caller that forgot to reset would
        // reintroduce stale rendering invisibly.
        beginRefresh()
        onPopoverVisibilityChanged?(true)
    }

    func popoverDidClose(_ notification: Notification) {
        onPopoverVisibilityChanged?(false)
    }

    // MARK: WKNavigationDelegate

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        pageReady = true
        pushPayload() // flush whatever we already have
    }

    /// Allow the initial `loadHTMLString`, cancel every navigation after it. The
    /// page carries vault-derived text and has no links by design; this makes
    /// that structural rather than conventional.
    func webView(_ webView: WKWebView,
                 decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if !didAllowInitialLoad {
            didAllowInitialLoad = true
            decisionHandler(.allow)
            return
        }
        NSLog("[Sonar] popover navigation blocked: \(navigationAction.request.url?.absoluteString ?? "nil")")
        decisionHandler(.cancel)
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
        case let body where body.hasPrefix("__err__:"):
            NSLog("[Sonar] popover render failed: \(body)")
        default:
            if let heightString = body.split(separator: ":", maxSplits: 1).last,
               body.hasPrefix("__h__:"),
               let height = Double(heightString) {
                resizePopover(to: CGFloat(height))
            }
        }
    }

    private func resizePopover(to height: CGFloat) {
        // Both feed lists are hard-capped at 3 rows in JS, so the page height is
        // bounded rather than open-ended. MEASURED against the live harness with
        // 3 nudges (one wrapping to 2 lines) + 3 activity rows: 723px. The
        // computed worst case, with every nudge wrapping and the doctor line at
        // its longest, is ~742px — the clamp keeps ~18px over that.
        //
        // COUPLED to NUDGE_MAX/EV_MAX in PopoverHTML and to the unclamped
        // .doctor .txt: content past this is clipped with no scrollbar, so
        // re-measure if any of the three changes.
        let clamped = min(max(height, 200), 760)
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

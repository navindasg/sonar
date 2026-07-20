import AppKit

/// The popover content shown from the menu-bar item: a title, a live harness
/// health row, an "Open Notes" button and "Quit". NSViewController is already
/// an NSObject, so @objc target/action works.
final class StatusPopoverViewController: NSViewController {
    private let titleLabel = NSTextField(labelWithString: "Sonar")
    private let healthLabel = NSTextField(labelWithString: "harness: checking…")

    /// Invoked on the main thread when the user clicks Open Notes.
    var onOpenNotes: (() -> Void)?
    /// Invoked on the main thread when the user clicks Quit.
    var onQuit: (() -> Void)?

    override func loadView() {
        titleLabel.font = .systemFont(ofSize: 15, weight: .semibold)
        healthLabel.font = .systemFont(ofSize: 12)
        healthLabel.textColor = .secondaryLabelColor
        healthLabel.lineBreakMode = .byTruncatingTail

        let openButton = NSButton(title: "Open Notes",
                                  target: self, action: #selector(openNotesClicked))
        openButton.bezelStyle = .rounded
        openButton.keyEquivalent = "\r"

        let quitButton = NSButton(title: "Quit Sonar",
                                  target: self, action: #selector(quitClicked))
        quitButton.bezelStyle = .rounded

        let stack = NSStackView(views: [titleLabel, healthLabel, openButton, quitButton])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 8
        stack.translatesAutoresizingMaskIntoConstraints = false

        let container = NSView(frame: NSRect(x: 0, y: 0, width: 260, height: 168))
        container.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: container.leadingAnchor, constant: 16),
            stack.trailingAnchor.constraint(equalTo: container.trailingAnchor, constant: -16),
            stack.topAnchor.constraint(equalTo: container.topAnchor, constant: 14),
            stack.bottomAnchor.constraint(lessThanOrEqualTo: container.bottomAnchor, constant: -14),
        ])
        self.view = container
    }

    func setHealth(_ snapshot: HealthSnapshot) {
        if snapshot.up {
            let model = snapshot.model ?? "?"
            healthLabel.stringValue =
                "harness: up · \(model) · \(snapshot.toolCount) tools · \(snapshot.chunkCount) chunks"
            healthLabel.textColor = .secondaryLabelColor
        } else {
            healthLabel.stringValue = "harness: down"
            healthLabel.textColor = .systemRed
        }
    }

    @objc private func openNotesClicked() {
        onOpenNotes?()
    }

    @objc private func quitClicked() {
        onQuit?()
    }
}

/// Owns the NSStatusItem (template SF Symbol button) and toggles the popover.
/// Primary entry point since the app is `.accessory` (no Dock menu). NSObject
/// so the button's @objc target/action resolves. Main-thread only.
final class StatusItemController: NSObject, NSPopoverDelegate {
    private let statusItem: NSStatusItem
    private let popover = NSPopover()
    private let contentController = StatusPopoverViewController()

    /// Invoked on the main thread when Open Notes is chosen.
    var onOpenNotes: (() -> Void)?

    override init() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        super.init()

        popover.behavior = .transient
        popover.contentViewController = contentController
        popover.delegate = self

        contentController.onOpenNotes = { [weak self] in
            self?.popover.performClose(nil)
            self?.onOpenNotes?()
        }
        contentController.onQuit = {
            NSApp.terminate(nil)
        }

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

    /// Push a fresh health reading into the popover (safe whether shown or not).
    func setHealth(_ snapshot: HealthSnapshot) {
        contentController.setHealth(snapshot)
    }

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

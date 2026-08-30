import AppKit
import Carbon.HIToolbox

/// A system-wide hotkey, via Carbon's `RegisterEventHotKey`.
///
/// Carbon is chosen over `NSEvent.addGlobalMonitorForEvents` deliberately: the
/// monitor API requires Accessibility permission (a TCC prompt on first run, and
/// a support burden for a one-click app), while `RegisterEventHotKey` needs no
/// permission at all. It is also the API Hammerspoon's `hs.hotkey` sits on, so
/// the native bar inherits the exact behavior the Lua overlay already had.
///
/// VERIFICATION NOTE: registration is checkable in code (`RegisterEventHotKey`
/// returns `noErr` and `-9878` when a combination is already taken), but the
/// FIRING is not automatable — synthesized `CGEvent`s do not match registered
/// hotkeys. That was confirmed here across all nine
/// CGEventSourceStateID x CGEventTapLocation combinations, while ordinary
/// synthesized keystrokes reached a WKWebView in the same session. So a real key
/// press is the only way to test this path.
final class GlobalHotKey {
    /// A parsed key combination.
    struct Combo {
        let keyCode: UInt32
        let modifiers: UInt32
        let label: String
    }

    private var ref: EventHotKeyRef?
    private var handler: EventHandlerRef?
    private let combo: Combo
    private let onFire: () -> Void

    /// Every instance gets its own id so several hotkeys can coexist.
    private static var nextID: UInt32 = 1
    private let id: UInt32

    /// Trampoline table: the Carbon callback is a C function pointer and cannot
    /// capture context, so it looks the instance up by hotkey id.
    private static var registry: [UInt32: GlobalHotKey] = [:]

    init(combo: Combo, onFire: @escaping () -> Void) {
        self.combo = combo
        self.onFire = onFire
        self.id = GlobalHotKey.nextID
        GlobalHotKey.nextID += 1
    }

    /// Registers the hotkey. Returns nil on success, or a human-readable reason.
    @discardableResult
    func register() -> String? {
        GlobalHotKey.registry[id] = self

        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard),
                                 eventKind: UInt32(kEventHotKeyPressed))
        let installStatus = InstallEventHandler(GetApplicationEventTarget(), { _, event, _ in
            guard let event = event else { return noErr }
            var hkID = EventHotKeyID()
            let got = GetEventParameter(event, EventParamName(kEventParamDirectObject),
                                        EventParamType(typeEventHotKeyID), nil,
                                        MemoryLayout<EventHotKeyID>.size, nil, &hkID)
            guard got == noErr, let target = GlobalHotKey.registry[hkID.id] else { return noErr }
            // Carbon delivers on the main thread, but the contract is not
            // documented as such — be explicit, since onFire touches AppKit.
            DispatchQueue.main.async { target.onFire() }
            return noErr
        }, 1, &spec, nil, &handler)

        guard installStatus == noErr else {
            GlobalHotKey.registry[id] = nil
            return "InstallEventHandler failed (\(installStatus))"
        }

        let status = RegisterEventHotKey(
            combo.keyCode, combo.modifiers,
            EventHotKeyID(signature: OSType(0x534E4152), id: id),   // 'SNAR'
            GetApplicationEventTarget(), 0, &ref
        )
        guard status == noErr else {
            GlobalHotKey.registry[id] = nil
            // -9878 is eventHotKeyExistsErr: something else already owns it.
            // Worth naming, because the likeliest cause is the Hammerspoon
            // overlay still running and holding the same combination.
            let reason = status == -9878
                ? "\(combo.label) is already taken by another app (Hammerspoon overlay still running?)"
                : "RegisterEventHotKey failed (\(status))"
            return reason
        }
        return nil
    }

    func unregister() {
        if let ref = ref { UnregisterEventHotKey(ref) }
        ref = nil
        if let handler = handler { RemoveEventHandler(handler) }
        handler = nil
        GlobalHotKey.registry[id] = nil
    }

    deinit { unregister() }
}

extension GlobalHotKey.Combo {
    /// Parses a combo like "f13", "cmd+alt+ctrl+g", "shift+f5".
    ///
    /// Kept permissive and total: an unparseable string returns nil so the
    /// caller can log and carry on rather than trapping. The app must still
    /// launch with a bad `SONAR_BAR_HOTKEY` — losing the bar is recoverable,
    /// failing to launch is not.
    static func parse(_ spec: String) -> GlobalHotKey.Combo? {
        let parts = spec.lowercased().split(separator: "+").map(String.init)
        guard let keyName = parts.last else { return nil }

        var modifiers: UInt32 = 0
        for part in parts.dropLast() {
            switch part {
            case "cmd", "command": modifiers |= UInt32(cmdKey)
            case "alt", "opt", "option": modifiers |= UInt32(optionKey)
            case "ctrl", "control": modifiers |= UInt32(controlKey)
            case "shift": modifiers |= UInt32(shiftKey)
            default: return nil
            }
        }

        guard let keyCode = GlobalHotKey.Combo.keyCodes[keyName] else { return nil }
        return GlobalHotKey.Combo(keyCode: keyCode, modifiers: modifiers, label: spec)
    }

    private static let keyCodes: [String: UInt32] = [
        "f1": UInt32(kVK_F1), "f2": UInt32(kVK_F2), "f3": UInt32(kVK_F3),
        "f4": UInt32(kVK_F4), "f5": UInt32(kVK_F5), "f6": UInt32(kVK_F6),
        "f7": UInt32(kVK_F7), "f8": UInt32(kVK_F8), "f9": UInt32(kVK_F9),
        "f10": UInt32(kVK_F10), "f11": UInt32(kVK_F11), "f12": UInt32(kVK_F12),
        "f13": UInt32(kVK_F13), "f14": UInt32(kVK_F14), "f15": UInt32(kVK_F15),
        "f16": UInt32(kVK_F16), "f17": UInt32(kVK_F17), "f18": UInt32(kVK_F18),
        "f19": UInt32(kVK_F19), "f20": UInt32(kVK_F20),
        "space": UInt32(kVK_Space),
        "a": UInt32(kVK_ANSI_A), "b": UInt32(kVK_ANSI_B), "c": UInt32(kVK_ANSI_C),
        "d": UInt32(kVK_ANSI_D), "e": UInt32(kVK_ANSI_E), "f": UInt32(kVK_ANSI_F),
        "g": UInt32(kVK_ANSI_G), "h": UInt32(kVK_ANSI_H), "i": UInt32(kVK_ANSI_I),
        "j": UInt32(kVK_ANSI_J), "k": UInt32(kVK_ANSI_K), "l": UInt32(kVK_ANSI_L),
        "m": UInt32(kVK_ANSI_M), "n": UInt32(kVK_ANSI_N), "o": UInt32(kVK_ANSI_O),
        "p": UInt32(kVK_ANSI_P), "q": UInt32(kVK_ANSI_Q), "r": UInt32(kVK_ANSI_R),
        "s": UInt32(kVK_ANSI_S), "t": UInt32(kVK_ANSI_T), "u": UInt32(kVK_ANSI_U),
        "v": UInt32(kVK_ANSI_V), "w": UInt32(kVK_ANSI_W), "x": UInt32(kVK_ANSI_X),
        "y": UInt32(kVK_ANSI_Y), "z": UInt32(kVK_ANSI_Z),
    ]
}

import AppKit

// Process entry point. Menu-bar-first (.accessory => no Dock icon, the runtime
// pair of Info.plist's LSUIElement=YES). The AppDelegate is retained by this
// top-level binding for the life of the process.
let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()

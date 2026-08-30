// End-to-end integration test for the native command bar, against a LIVE bridge
// on :8770 and a LIVE harness on :8787.
//
// Run it with app/tests/run_command_bar_e2e.sh, which compiles this against the
// real SonarApp sources — so it exercises the shipping code, not a copy.
//
// It drives the whole path the way a user does, with REAL synthesized keystrokes
// rather than by calling JS directly:
//
//   keystrokes -> WKWebView page -> "sonar" message handler -> BridgeClient
//     -> bridge.py -> harness /v1 (SSE) -> streamed answer -> evalBar -> DOM
//
// and checks the focus contract the whole design rests on: the bar takes the
// keyboard while open, and hands the user's app back on dismiss.
//
// WHAT THIS CANNOT COVER: the global hotkey. Synthesized CGEvents do not match
// a registered Carbon hotkey — confirmed across all nine
// CGEventSourceStateID x CGEventTapLocation combinations, in a session where
// ordinary synthesized keystrokes DID reach the WKWebView. Only a real key press
// exercises that path, so it stays a manual check.
import AppKit
import WebKit

func frontName() -> String { NSWorkspace.shared.frontmostApplication?.localizedName ?? "?" }

final class CommandBarE2E: NSObject, NSApplicationDelegate {
    var bar: CommandBarController!
    var before = ""
    var deadline: Date!

    func applicationDidFinishLaunching(_ n: Notification) {
        before = frontName()
        print("frontmost before: \(before)")
        bar = CommandBarController(bridgeURL: URL(string: "ws://127.0.0.1:8770/")!)
        after(1.5) { self.openBar() }      // let the page load and the socket open
    }

    func openBar() {
        bar.show()
        print("bar visible: \(bar.isVisible)")
        after(1.2) {
            print("frontmost while bar open: \(frontName())")
            self.typeNext(0)
        }
    }

    /// "hi" + Return — deliberately tiny so the harness turn is quick.
    let codes: [CGKeyCode] = [4, 34, 36]      // h, i, Return
    func typeNext(_ i: Int) {
        guard i < codes.count else {
            print("typed 'hi' + Return; waiting for the answer to stream back…")
            deadline = Date().addingTimeInterval(120)
            after(1.0) { self.pollAnswer() }
            return
        }
        let src = CGEventSource(stateID: .hidSystemState)
        CGEvent(keyboardEventSource: src, virtualKey: codes[i], keyDown: true)?.post(tap: .cghidEventTap)
        CGEvent(keyboardEventSource: src, virtualKey: codes[i], keyDown: false)?.post(tap: .cghidEventTap)
        after(0.12) { self.typeNext(i + 1) }
    }

    func pollAnswer() {
        let js = """
        JSON.stringify({
          heard: (document.getElementById('heardText')||{}).textContent || '',
          answer: (document.getElementById('answer')||{}).textContent || '',
          steps: document.querySelectorAll('#steps li').length
        })
        """
        bar.webView.evaluateJavaScript(js) { value, err in
            let obj = (value as? String).flatMap { $0.data(using: .utf8) }
                .flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] } ?? [:]
            let heard = obj["heard"] as? String ?? ""
            let answer = obj["answer"] as? String ?? ""
            let steps = obj["steps"] as? Int ?? 0
            if let err = err { print("DOM read error: \(err)") }
            if (answer.count > 3) || Date() > self.deadline {
                print("")
                print("heard:  '\(heard)'")
                print("answer: '\(answer.prefix(160))\(answer.count > 160 ? "…" : "")'")
                print("steps:  \(steps)")
                self.finish(heard: heard, answer: answer, steps: steps)
            } else {
                self.after(1.0) { self.pollAnswer() }
            }
        }
    }

    func finish(heard: String, answer: String, steps: Int) {
        bar.dismiss()
        after(1.0) {
            let restored = frontName()
            let heardOK = heard.contains("hi")
            let answerOK = answer.count > 3
            let restoredOK = restored == self.before
            print("")
            print("RESULT")
            print("  typed text reached the page:      \(heardOK ? "YES" : "NO")")
            print("  answer streamed back into DOM:    \(answerOK ? "YES (\(answer.count) chars)" : "NO")")
            print("  step events rendered:             \(steps > 0 ? "YES (\(steps))" : "none")")
            print("  previous app restored on dismiss: \(restoredOK ? "YES (\(restored))" : "NO (-> \(restored))")")
            exit(heardOK && answerOK && restoredOK ? 0 : 1)
        }
    }

    func after(_ s: Double, _ f: @escaping () -> Void) {
        DispatchQueue.main.asyncAfter(deadline: .now() + s, execute: f)
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let e2e = CommandBarE2E()
app.delegate = e2e
app.run()

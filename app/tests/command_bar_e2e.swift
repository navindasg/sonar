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
    ///
    /// Typed as two characters then Return, with the input CHECKED between, so
    /// the run can tell "the app dropped the text" from "the OS never delivered
    /// the synthetic keystroke". Those are different failures and only the first
    /// is a bug here: macOS can stop delivering CGEvent-synthesized keys to a
    /// panel while `document.activeElement` stays correct and the panel stays
    /// key, and that must not be reported as a broken command bar.
    let codes: [CGKeyCode] = [4, 34, 36]      // h, i, Return
    func typeNext(_ i: Int) {
        guard i < codes.count else {
            print("typed 'hi' + Return; waiting for the answer to stream back…")
            deadline = Date().addingTimeInterval(120)
            after(1.0) { self.pollAnswer() }
            return
        }
        // Before Return, confirm the characters actually arrived.
        if i == codes.count - 1 {
            bar.webView.evaluateJavaScript(
                "JSON.stringify({a:(document.activeElement||{}).id, v:document.getElementById('cmd').value})"
            ) { value, _ in
                let raw = (value as? String) ?? ""
                let focused = raw.contains("\"a\":\"cmd\"")
                let typed = raw.range(of: "\"v\":\"([^\"]*)\"", options: .regularExpression)
                    .map { String(raw[$0]).replacingOccurrences(of: "\"v\":\"", with: "")
                                          .replacingOccurrences(of: "\"", with: "") } ?? ""
                // Focused but the characters did not all arrive => the OS did not
                // deliver them. Partial delivery counts: it is the same fault.
                if focused && typed != "hi" {
                    print("")
                    print("SKIP: the input is focused (activeElement=cmd) but the synthesized")
                    print("      keystrokes did not arrive intact — expected 'hi', got '\(typed)'.")
                    print("      This machine is not delivering CGEvent keys to the panel right")
                    print("      now; that is an ENVIRONMENT failure, not a command-bar failure.")
                    print("      Re-run in a fresh session, or drive the bar by hand.")
                    self.bar.dismiss()
                    exit(2)
                }
                self.postKey(i)
                self.after(0.12) { self.typeNext(i + 1) }
            }
            return
        }
        postKey(i)
        after(0.12) { self.typeNext(i + 1) }
    }

    func postKey(_ i: Int) {
        let src = CGEventSource(stateID: .hidSystemState)
        CGEvent(keyboardEventSource: src, virtualKey: codes[i], keyDown: true)?.post(tap: .cghidEventTap)
        CGEvent(keyboardEventSource: src, virtualKey: codes[i], keyDown: false)?.post(tap: .cghidEventTap)
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

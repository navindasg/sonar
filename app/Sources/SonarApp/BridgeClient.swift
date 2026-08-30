import Foundation

/// One message from the `:8770` server, as documented in overlay/bridge.py.
///
/// The wire protocol is a flat, untagged union — each frame is a JSON object
/// carrying whichever of these keys apply — so this is decoded by inspecting
/// keys rather than by a discriminator field.
enum BridgeEvent {
    /// `{"turn": "start"|"end"}` — brackets a turn.
    case turn(start: Bool)
    /// `{"state": "...", "level": n}` — glow/one-light modulation.
    case state(String, level: Double?)
    /// `{"step": {step, tool, detail, status}}` — one harness step-event.
    case step(kind: String, tool: String, detail: String, status: String)
    /// `{"answer": "<delta>", "partial": bool}` — streamed answer text.
    case answer(String)
    /// `{"transcript": "...", "partial": bool}` — live/final STT into the box.
    /// Voice loop only; bridge.py never sends it.
    case transcript(String, partial: Bool)
    /// `{"summon": true, "text": "..."}` — a PROACTIVE push (the morning brief).
    /// Voice loop only. This is the one path where the bar appears without the
    /// user asking, so it must never steal focus.
    case summon(text: String)
}

/// WebSocket client for the overlay bridge on `:8770`.
///
/// That port has two mutually exclusive owners — `overlay/bridge.py` (the typed
/// path) and the voice loop, which serves the same socket instead of it. Both
/// speak this protocol, so this client does not care which is listening.
///
/// It sends no `Origin` header, deliberately. Both servers gate cross-origin
/// browser handshakes but explicitly allow clients that send none, because
/// Hammerspoon's `hs.websocket` sends none and refusing it would break F5. A
/// native app is in exactly that category.
final class BridgeClient: NSObject, URLSessionWebSocketDelegate {
    private let url: URL
    private var session: URLSession!
    private var task: URLSessionWebSocketTask?
    private var reconnectAttempt = 0
    private var intentionallyClosed = false
    /// A cmd frame that could not be sent because the handshake was still in
    /// flight; flushed by didOpenWithProtocol. See send(command:).
    private var pendingCommand: String?

    /// Delivered on the main thread.
    var onEvent: ((BridgeEvent) -> Void)?
    /// Connection state changes, on the main thread.
    var onConnectionChange: ((Bool) -> Void)?

    private(set) var isConnected = false {
        didSet {
            guard isConnected != oldValue else { return }
            let connected = isConnected
            DispatchQueue.main.async { self.onConnectionChange?(connected) }
        }
    }

    init(url: URL) {
        self.url = url
        super.init()
        let cfg = URLSessionConfiguration.ephemeral
        cfg.waitsForConnectivity = false
        session = URLSession(configuration: cfg, delegate: self, delegateQueue: .main)
    }

    func connect() {
        intentionallyClosed = false
        guard task == nil else { return }
        let task = session.webSocketTask(with: url)
        self.task = task
        task.resume()
        receive()
    }

    func disconnect() {
        intentionallyClosed = true
        pendingCommand = nil
        task?.cancel(with: .goingAway, reason: nil)
        task = nil
        isConnected = false
    }

    /// Runs one harness turn. No-op when not connected — the caller surfaces
    /// that to the user rather than queueing a question that would fire later
    /// out of context.
    @discardableResult
    func send(text: String) -> Bool {
        guard isConnected, let task = task else { return false }
        return send(json: ["text": text], on: task)
    }

    /// `{"cmd":"start"|"stop"}` — box opened/closed.
    ///
    /// Unlike `send(text:)` this DOES queue while the handshake is in flight.
    /// `connect()` only calls `resume()`, and `isConnected` cannot flip until
    /// `didOpenWithProtocol` is delivered on the main queue — which cannot
    /// preempt the caller's own main-queue block. So a `show()` that connects
    /// and immediately sends would drop the frame EVERY first time, not just
    /// occasionally. Under the voice loop that means the mic never turns on.
    ///
    /// Queueing is right here where it is wrong for a question: a command is a
    /// session marker that is still true a few milliseconds later, whereas a
    /// queued question would fire later, out of the context the user asked it in.
    /// Only the LAST command is kept — start-then-stop must not resurrect a
    /// session the user already closed.
    @discardableResult
    func send(command: String) -> Bool {
        guard isConnected, let task = task else {
            pendingCommand = command
            return false
        }
        pendingCommand = nil
        return send(json: ["cmd": command], on: task)
    }

    private func send(json: [String: String], on task: URLSessionWebSocketTask) -> Bool {
        guard let data = try? JSONSerialization.data(withJSONObject: json),
              let text = String(data: data, encoding: .utf8) else { return false }
        task.send(.string(text)) { error in
            if let error = error { NSLog("[Sonar] bridge send failed: \(error)") }
        }
        return true
    }

    private func receive() {
        task?.receive { [weak self] result in
            guard let self = self else { return }
            switch result {
            case .success(let message):
                if case .string(let text) = message { self.handle(text) }
                if case .data(let data) = message,
                   let text = String(data: data, encoding: .utf8) { self.handle(text) }
                self.receive()   // re-arm; receive() is one-shot
            case .failure:
                // Any receive failure means this socket is done. Do not re-arm —
                // that would spin on a dead task.
                self.isConnected = false
                self.task = nil
                self.scheduleReconnect()
            }
        }
    }

    private func handle(_ text: String) {
        guard let data = text.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return }

        // Untagged union: inspect keys. A frame can legitimately carry more than
        // one (state + level arrive together), so these are not exclusive.
        if let turn = obj["turn"] as? String {
            emit(.turn(start: turn == "start"))
        }
        if let state = obj["state"] as? String {
            emit(.state(state, level: obj["level"] as? Double))
        }
        if let step = obj["step"] as? [String: Any] {
            emit(.step(kind: step["step"] as? String ?? "",
                       tool: step["tool"] as? String ?? "",
                       detail: step["detail"] as? String ?? "",
                       status: step["status"] as? String ?? ""))
        }
        if let answer = obj["answer"] as? String {
            emit(.answer(answer))
        }
        if let transcript = obj["transcript"] as? String {
            emit(.transcript(transcript, partial: obj["partial"] as? Bool ?? false))
        }
        if obj["summon"] as? Bool == true {
            emit(.summon(text: obj["text"] as? String ?? ""))
        }
    }

    private func emit(_ event: BridgeEvent) {
        DispatchQueue.main.async { self.onEvent?(event) }
    }

    /// Capped exponential backoff. The bar is opened on a hotkey, so a dead
    /// bridge must not become a busy loop while the user is not even looking.
    private func scheduleReconnect() {
        guard !intentionallyClosed else { return }
        reconnectAttempt = min(reconnectAttempt + 1, 6)
        let delay = min(pow(2.0, Double(reconnectAttempt)) * 0.25, 15.0)
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
            guard let self = self, !self.intentionallyClosed, self.task == nil else { return }
            self.connect()
        }
    }

    // MARK: URLSessionWebSocketDelegate

    func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask,
                    didOpenWithProtocol proto: String?) {
        reconnectAttempt = 0
        isConnected = true
        if let command = pendingCommand {
            pendingCommand = nil
            send(command: command)
        }
    }

    func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask,
                    didCloseWith closeCode: URLSessionWebSocketTask.CloseCode,
                    reason: Data?) {
        isConnected = false
        task = nil
        scheduleReconnect()
    }
}

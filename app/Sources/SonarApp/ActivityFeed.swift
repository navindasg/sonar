import Foundation

/// One step event from `/events`.
///
/// `ts` is kept RAW (epoch milliseconds — the harness's only millisecond
/// timestamp) so its age is recomputed at push time rather than frozen at
/// receipt.
struct ActivityItem {
    let turnId: String
    let tsMs: Int64
    /// "" when null or absent. NOT a closed enum on the wire: the harness logs
    /// an unrecognized step and stores it anyway, and the column is nullable.
    let step: String
    /// "" when the key is absent (the harness omits it rather than sending null).
    let tool: String
    /// "" when absent OR present-and-empty — both occur live.
    let detail: String
    /// "" when absent; verbatim otherwise.
    let status: String

    /// Identity to print. Never invented: falls back through real fields to the
    /// turn id, which the schema guarantees NOT NULL.
    var label: String {
        if !tool.isEmpty { return tool }
        if !step.isEmpty { return step }
        return turnId
    }
}

/// What we know about `/events` right now. As with `NudgeReading`, only `.ok`
/// carries rows.
enum ActivityReading {
    case unknown
    case unreachable
    case malformed
    /// Newest-first, capped.
    case ok(items: [ActivityItem])
}

enum ActivityFeed {
    static let maxRows = 3

    /// Never let this reach 0: `/events` clamps limit to [1, 2000], so limit=0
    /// returns ONE event rather than none.
    static var requestLimit: Int { max(1, maxRows) }

    private struct WirePayload: Decodable {
        let events: [WireEvent]
    }

    private struct WireEvent: Decodable {
        let turn_id: String
        let ts: Int64
        let status: String?
        let step: String?
        /// Key OMITTED (never null) when unset — a non-optional decode would
        /// fail on the live `final` event and turn the section into a false
        /// `.malformed`.
        let tool: String?
        let detail: String?
    }

    static func decode(data: Data?, response: URLResponse?, error: Error?) -> ActivityReading {
        guard error == nil,
              let http = response as? HTTPURLResponse, http.statusCode == 200,
              let data = data
        else { return .unreachable }
        guard let payload = try? JSONDecoder().decode(WirePayload.self, from: data)
        else { return .malformed }

        // The endpoint always returns OLDEST-FIRST, even though `limit` selects
        // the newest N and then reverses. So events[0] is the OLDEST of the
        // window — reading it as "the latest" is the easiest mistake against
        // this endpoint. Reverse for display.
        let newestFirst = payload.events.reversed().prefix(maxRows).map { wire in
            ActivityItem(turnId: wire.turn_id,
                         tsMs: wire.ts,
                         step: wire.step ?? "",
                         tool: wire.tool ?? "",
                         detail: wire.detail ?? "",
                         status: wire.status ?? "")
        }
        return .ok(items: Array(newestFirst))
    }

    /// Whole seconds since `tsMs`, or -1 when the stamp is unusable (more than a
    /// minute in the future — clock skew). -1 renders as an em dash, never "now".
    static func ageS(of tsMs: Int64, nowMs: Int64) -> Int {
        let delta = nowMs - tsMs
        if delta < -60_000 { return -1 }
        return Int(max(delta, 0) / 1000)
    }

    static func nowMs() -> Int64 { Int64(Date().timeIntervalSince1970 * 1000) }
}

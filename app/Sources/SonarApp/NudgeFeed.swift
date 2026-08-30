import Foundation

/// One row of the harness `/nudges` snapshot.
///
/// Deliberately drops two wire fields. `rank` is an intra-source sort key whose
/// SIGN varies by source (calendar +minutes-until, todo/email -days) and is not
/// a magnitude, so it is never comparable across rows and never renderable.
/// `at` is mixed-format — an aware ISO datetime for calendar/email/notes, a bare
/// "2026-03-20" for todo, and sometimes "" — so any single parse of it would
/// eventually render a wrong time. `line` already carries the human timing
/// ("— 163 days overdue"), which is the only timing this surface can state.
struct NudgeItem {
    let id: String
    let source: String
    /// Verbatim from the server; the renderer allowlists it, never interpolates it.
    let severity: String
    /// Vault-derived free text. Arbitrary content, including HTML metacharacters.
    let line: String
}

/// What we know about `/nudges` right now.
///
/// `.ok` is the ONLY case that carries items. That is the load-bearing honesty
/// invariant of this feature: an unreachable, malformed, or expired reading has
/// no rows to hand anyone, so a stale nudge cannot reach the page by
/// construction rather than by a renderer remembering to check.
enum NudgeReading {
    /// Not polled yet in this popover session.
    case unknown
    /// Transport failure or non-200.
    case unreachable
    /// HTTP 200 whose body will not decode.
    case malformed
    case ok(Snapshot)

    struct Snapshot {
        /// Already capped to `NudgeFeed.maxRows`.
        let items: [NudgeItem]
        /// How many the harness sent, BEFORE our cap.
        let returned: Int
        let ageAtReceiptS: Double
        let ttlS: Double
        let receivedAt: ContinuousClock.Instant
    }
}

/// Freshness derived at PUSH time, never frozen at receipt time.
enum Freshness {
    case current(ageCeilingS: Int)
    case expired
}

extension NudgeReading.Snapshot {
    /// Recomputed on every push so the number written into the DOM was true when
    /// it was written. The ceiling adds one grace window, making the displayed
    /// value an UPPER BOUND that stays honest until the next push lands: it can
    /// over-report age, never under-report it.
    func freshness(now: ContinuousClock.Instant, graceS: Double) -> Freshness {
        let elapsed = NudgeFeed.seconds(from: receivedAt, to: now)
        let validForS = max(ttlS - ageAtReceiptS, 0)
        if elapsed > validForS + graceS { return .expired }
        return .current(ageCeilingS: Int(ceil(ageAtReceiptS + elapsed + graceS)))
    }
}

enum NudgeFeed {
    /// Render cap, applied Swift-side so the wire payload is bounded too.
    static let maxRows = 3

    /// A single vault entry is unbounded on the wire. Cap it before it crosses
    /// the JS-source seam — the 2-line CSS clamp would hide it, but a
    /// pathological note should not become a megabyte of JavaScript literal.
    ///
    /// Counted in UNICODE SCALARS, not Characters: a grapheme cluster has no
    /// length bound, so `prefix` on Swift's default Character view would let one
    /// "character" carrying 200k combining marks through untouched.
    static let maxLineScalars = 400

    private struct WirePayload: Decodable {
        let age_s: Double
        let ttl_s: Double
        let count: Int
        let nudges: [WireNudge]
    }

    private struct WireNudge: Decodable {
        let id: String
        let source: String
        let severity: String
        let line: String
        // `rank` and `at` intentionally not decoded; unknown keys are ignored.
    }

    static func decode(data: Data?, response: URLResponse?, error: Error?,
                       at receivedAt: ContinuousClock.Instant) -> NudgeReading {
        guard error == nil,
              let http = response as? HTTPURLResponse, http.statusCode == 200,
              let data = data
        else { return .unreachable }
        guard let payload = try? JSONDecoder().decode(WirePayload.self, from: data)
        else { return .malformed }

        let items = payload.nudges.prefix(maxRows).map { wire in
            NudgeItem(id: wire.id,
                      source: wire.source,
                      severity: wire.severity,
                      line: String(String.UnicodeScalarView(wire.line.unicodeScalars.prefix(maxLineScalars))))
        }
        return .ok(.init(
            items: Array(items),
            // Array length, NOT the wire `count`: the two are only incidentally
            // equal, and `count` is post-cap so it is not a pre-truncation total.
            returned: payload.nudges.count,
            ageAtReceiptS: max(payload.age_s, 0),
            ttlS: max(payload.ttl_s, 0),
            receivedAt: receivedAt
        ))
    }

    static func seconds(from start: ContinuousClock.Instant,
                        to end: ContinuousClock.Instant) -> Double {
        let duration = end - start
        return Double(duration.components.seconds)
            + Double(duration.components.attoseconds) / 1e18
    }
}

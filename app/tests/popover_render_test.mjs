// Drives the popover page through every render state in real WebKit — the same
// engine as the WKWebView it ships in — and asserts the data-honesty and
// escaping rules the feature exists to guarantee.
//
// The rules under test cannot be unit-tested from Swift as the package stands
// (app/Package.swift declares one executableTarget and no test target), and
// they are the kind that a later refactor silently breaks: a stale nudge
// surviving a state transition, vault text reaching an HTML parser, an invented
// timestamp. So they are pinned here instead.
//
//   node app/tests/popover_render_test.mjs
//
// The page is extracted from PopoverHTML.swift at run time, so this always
// tests the markup that actually ships. Exits 0 on pass, 1 on failure, and 2
// when no Playwright/WebKit is available (skip, not failure — there is no
// hermetic browser in this repo).
import { pathToFileURL } from 'node:url'
import { readFileSync, writeFileSync, mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createRequire } from 'node:module'

const HERE = dirname(fileURLToPath(import.meta.url))
const SWIFT = resolve(HERE, '../Sources/SonarApp/PopoverHTML.swift')

// Resolve Playwright from anywhere it happens to be installed. Nothing in this
// repo depends on node, so a missing browser is a SKIP: it must never turn a
// green build red, but it must also never silently pass.
function loadWebkit() {
  const req = createRequire(import.meta.url)
  const candidates = [
    'playwright',
    ...(process.env.PLAYWRIGHT_PATH ? [process.env.PLAYWRIGHT_PATH] : []),
  ]
  for (const c of candidates) {
    try { return req(c).webkit } catch { /* try the next */ }
  }
  return null
}

const webkit = loadWebkit()
if (!webkit) {
  console.error('SKIP: Playwright not found. Install it, or set PLAYWRIGHT_PATH to a playwright package dir.')
  process.exit(2)
}

// Pull the literal straight out of the Swift source so the test can never drift
// from the shipped page.
function extractPage() {
  const src = readFileSync(SWIFT, 'utf8')
  const start = src.indexOf('static let html = """')
  if (start < 0) throw new Error('could not find `static let html = """` in PopoverHTML.swift')
  const bodyStart = src.indexOf('\n', start) + 1
  const end = src.indexOf('\n"""', bodyStart)
  if (end < 0) throw new Error('unterminated html literal in PopoverHTML.swift')
  return src.slice(bodyStart, end)
}

const dir = mkdtempSync(join(tmpdir(), 'sonar-popover-'))
const pageFile = join(dir, 'popover.html')
writeFileSync(pageFile, extractPage(), 'utf8')
const PAGE = pathToFileURL(pageFile).href

const HEALTH = { harnessUp: true, bridgeUp: true, notesUp: false, model: 'gemma4:e4b-mlx', tools: 17, chunks: 760 }
const nudge = (state, extra = {}) => ({ state, items: [], returned: 0, ageCeilingS: -1, ...extra })
const act = (state, extra = {}) => ({ state, items: [], newestAgeS: -1, ...extra })

const N3 = [
  { severity: 'high', line: '“5 uses” — 163 days overdue' },
  { severity: 'high', line: '“demo and usage meeting” — 163 days overdue' },
  { severity: 'medium', line: '“release notes + instructions” — 101 days overdue' },
]
const EV3 = [
  { kind: 'final', label: 'final', detail: 'streaming reply', status: 'ok', ageS: 1944000 },
  { kind: 'tool_result_summary', label: 'daily.brief', detail: "read back today's brief (22:07)", status: 'ok', ageS: 1944011 },
  { kind: 'tool', label: 'daily.brief', detail: '', status: 'pending', ageS: 1944013 },
]

const results = []
const check = (name, pass, detail = '') => results.push({ name, pass, detail })

const browser = await webkit.launch()
const page = await browser.newPage({ viewport: { width: 340, height: 900 } })

const dialogs = []
page.on('dialog', async d => { dialogs.push(d.message()); await d.dismiss() })
const pageErrors = []
page.on('pageerror', e => pageErrors.push(String(e)))

await page.goto(PAGE)

const apply = (s) => page.evaluate(p => window.sonarPopover.apply(p), s)
const snap = () => page.evaluate(() => ({
  nudgeAux: document.getElementById('nudgeAux').textContent,
  evAux: document.getElementById('evAux').textContent,
  nudgeRows: document.querySelectorAll('#nudgeList .nudge').length,
  evRows: document.querySelectorAll('#evList .ev').length,
  nudgeText: document.getElementById('nudgeList').textContent.trim(),
  evText: document.getElementById('evList').textContent.trim(),
  noteHidden: document.getElementById('nudgeNote').hidden,
  noteText: document.getElementById('nudgeNote').textContent,
  height: document.body.scrollHeight,
}))

// ---- 1. unknown (first paint / reopen) -------------------------------------
await apply({ ...HEALTH, nudges: nudge('unknown'), activity: act('unknown') })
let s = await snap()
check('unknown: zero nudge rows', s.nudgeRows === 0)
check('unknown: nudge says Checking', s.nudgeText === 'Checking…', s.nudgeText)
check('unknown: activity says Checking', s.evText === 'Checking…', s.evText)
check('unknown: both aux are em dash', s.nudgeAux === '—' && s.evAux === '—', `${s.nudgeAux}/${s.evAux}`)

// ---- 2. empty, fresh -------------------------------------------------------
await apply({ ...HEALTH, nudges: nudge('empty', { ageCeilingS: 5 }), activity: act('empty') })
s = await snap()
check('empty: nudge copy describes the response', s.nudgeText === 'No nudges reported', s.nudgeText)
check('empty: activity copy describes the response', s.evText === 'No steps returned', s.evText)
check('empty: FRESH nothing shows an age ceiling', s.nudgeAux === '≤5s old', s.nudgeAux)
check('empty: activity aux is em dash (no newest event)', s.evAux === '—', s.evAux)

// ---- 3/4. populated --------------------------------------------------------
await apply({ ...HEALTH, nudges: nudge('ok', { items: N3, returned: 3, ageCeilingS: 12 }), activity: act('ok', { items: EV3, newestAgeS: 1944000 }) })
s = await snap()
check('ok: 3 nudge rows', s.nudgeRows === 3, String(s.nudgeRows))
check('ok: 3 activity rows', s.evRows === 3, String(s.evRows))
check('ok: nudge aux is a ceiling', s.nudgeAux === '≤12s old', s.nudgeAux)
check('ok: activity aux states newest age', s.evAux === 'newest 22d', s.evAux)
check('ok: no truncation note when nothing hidden', s.noteHidden === true)
const sevClasses = await page.evaluate(() => [...document.querySelectorAll('#nudgeList .sev')].map(e => e.className))
check('ok: severity maps to allowlisted classes', JSON.stringify(sevClasses) === JSON.stringify(['sev high', 'sev high', 'sev medium']), JSON.stringify(sevClasses))
const detSpans = await page.evaluate(() => [...document.querySelectorAll('#evList .ev')].map(r => r.querySelectorAll('.det').length))
check('ok: empty detail omits the span entirely', JSON.stringify(detSpans) === '[1,1,0]', JSON.stringify(detSpans))
check('ok: height under the 760 clamp', s.height <= 760, `height=${s.height}`)

// ---- 5. our own truncation -------------------------------------------------
await apply({ ...HEALTH, nudges: nudge('ok', { items: N3, returned: 5, ageCeilingS: 12 }), activity: act('empty') })
s = await snap()
check('truncation: note shown', s.noteHidden === false)
check('truncation: phrased as OUR truncation, not a total', s.noteText === '2 more returned, not shown', s.noteText)

// ---- 6/7/8. failure renders ------------------------------------------------
await apply({ ...HEALTH, nudges: nudge('unreachable'), activity: act('unreachable') })
s = await snap()
check('unreachable: zero rows', s.nudgeRows === 0 && s.evRows === 0)
check('unreachable: aux falls back to em dash', s.nudgeAux === '—' && s.evAux === '—')
check('unreachable: two sections read as two distinct facts', s.nudgeText !== s.evText, `${s.nudgeText} | ${s.evText}`)

await apply({ ...HEALTH, nudges: nudge('malformed'), activity: act('malformed') })
s = await snap()
check('malformed: distinct from unreachable', s.nudgeText === 'Unexpected response from harness', s.nudgeText)

// ---- 9. THE transition test: ok -> stale must leave zero rows ---------------
await apply({ ...HEALTH, nudges: nudge('ok', { items: N3, returned: 3, ageCeilingS: 12 }), activity: act('ok', { items: EV3, newestAgeS: 60 }) })
await apply({ ...HEALTH, nudges: nudge('stale'), activity: act('unreachable') })
s = await snap()
check('TRANSITION ok->stale: zero nudge rows survive', s.nudgeRows === 0, `rows=${s.nudgeRows}`)
check('TRANSITION ok->stale: copy is the expiry message', s.nudgeText === 'Snapshot expired — rechecking…', s.nudgeText)
check('TRANSITION ok->stale: stale aux drops the age', s.nudgeAux === '—', s.nudgeAux)
check('TRANSITION ok->unreachable: zero activity rows survive', s.evRows === 0, `rows=${s.evRows}`)

// ---- 10/11/12. escaping ----------------------------------------------------
const XSS = '<img src=x onerror="alert(1)"> & "quotes" </div><script>alert(2)</script>'
const U2028 = 'before after'
await apply({
  ...HEALTH,
  nudges: nudge('ok', { items: [{ severity: 'high', line: XSS }, { severity: 'high', line: U2028 }], returned: 2, ageCeilingS: 3 }),
  activity: act('ok', { items: [{ kind: 'tool', label: '</span><b>x', detail: '<script>alert(3)</script>', status: 'ok', ageS: 5 }], newestAgeS: 5 }),
})
s = await snap()
const injected = await page.evaluate(() => document.querySelectorAll('#nudgeList img, #nudgeList script, #evList script, #evList b').length)
check('XSS: no injected nodes anywhere', injected === 0, `injected=${injected}`)
check('XSS: markup renders as literal text', s.nudgeText.includes('<img src=x onerror='), s.nudgeText.slice(0, 60))
const labelText = await page.evaluate(() => document.querySelector('#evList .id').textContent)
check('XSS: hostile activity label is literal', labelText === '</span><b>x', labelText)
const u2 = await page.evaluate(() => [...document.querySelectorAll('#nudgeList .line')].map(e => e.textContent).find(t => t.includes('before')))
check('U+2028 survives intact as text', u2 === U2028, JSON.stringify(u2))
check('XSS: no dialog fired', dialogs.length === 0, JSON.stringify(dialogs))

// ---- 13. pathological width ------------------------------------------------
await apply({ ...HEALTH, nudges: nudge('ok', { items: [{ severity: 'high', line: 'x'.repeat(400) }], returned: 1, ageCeilingS: 3 }), activity: act('empty') })
const overflow = await page.evaluate(() => ({ body: document.body.scrollWidth, doc: document.documentElement.clientWidth }))
check('400-char unbroken token does not overflow horizontally', overflow.body <= 340, JSON.stringify(overflow))

// ---- 14. caps --------------------------------------------------------------
const many = Array.from({ length: 12 }, (_, i) => ({ severity: 'high', line: `nudge ${i}` }))
const manyEv = Array.from({ length: 12 }, (_, i) => ({ kind: 'tool', label: `t${i}`, detail: 'd', status: 'ok', ageS: i }))
await apply({ ...HEALTH, nudges: nudge('ok', { items: many, returned: 12, ageCeilingS: 3 }), activity: act('ok', { items: manyEv, newestAgeS: 0 }) })
s = await snap()
check('cap: exactly 3 nudge rows from 12', s.nudgeRows === 3, String(s.nudgeRows))
check('cap: exactly 3 activity rows from 12', s.evRows === 3, String(s.evRows))
check('cap: height still under clamp with full lists', s.height <= 760, `height=${s.height}`)

// ---- 15. prototype pollution via `step` ------------------------------------
await apply({
  ...HEALTH,
  nudges: nudge('empty', { ageCeilingS: 1 }),
  activity: act('ok', { items: [
    { kind: 'constructor', label: 'a', detail: '', status: 'ok', ageS: 1 },
    { kind: '__proto__', label: 'b', detail: '', status: 'weird', ageS: -1 },
    { kind: 'toString', label: 'c', detail: '', status: 'error', ageS: 2 },
  ], newestAgeS: 1 }),
})
s = await snap()
check('prototype keys do not throw: 3 rows rendered', s.evRows === 3, String(s.evRows))
const glyphPaths = await page.evaluate(() => [...document.querySelectorAll('#evList .ico')].map(e => e.querySelectorAll('path').length))
check('prototype keys fall back to the unknown glyph', JSON.stringify(glyphPaths) === '[1,1,1]', JSON.stringify(glyphPaths))
const rowCls = await page.evaluate(() => [...document.querySelectorAll('#evList .ev')].map(e => e.className))
check('hostile status stays neutral (allowlist)', JSON.stringify(rowCls) === JSON.stringify(['ev ok', 'ev', 'ev err']), JSON.stringify(rowCls))
const ages = await page.evaluate(() => [...document.querySelectorAll('#evList .age')].map(e => e.textContent))
check('ageS -1 renders as em dash, never "now"', ages[1] === '—', JSON.stringify(ages))

// ---- 16. renderer isolation ------------------------------------------------
await apply({ ...HEALTH, nudges: null, activity: act('ok', { items: EV3, newestAgeS: 30 }) })
s = await snap()
check('null nudges section degrades to Checking, activity still renders', s.nudgeText === 'Checking…' && s.evRows === 3, `${s.nudgeText}|${s.evRows}`)

check('no uncaught page errors across all fixtures', pageErrors.length === 0, JSON.stringify(pageErrors.slice(0, 3)))

await browser.close()

const failed = results.filter(r => !r.pass)
for (const r of results) console.log(`${r.pass ? '  ok  ' : 'FAIL  '} ${r.name}${r.detail && !r.pass ? `  [${r.detail}]` : ''}`)
console.log(`\n${results.length - failed.length}/${results.length} passed`)
process.exit(failed.length ? 1 : 0)

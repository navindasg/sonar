import Foundation

/// The Gotham Noir menu-bar popover, rendered in a WKWebView inside the
/// NSPopover (see StatusItemController). Embedded as a Swift string rather than
/// a bundled resource so it survives the hand-assembled .app (build-app.sh
/// copies only the bare binary — no SwiftPM resource bundle) and needs no
/// network: all SVG is inline, fonts are the native SF stack, no external assets.
///
/// Contract with the Swift side (mirrors the overlay's `window.sonar` bridge):
///   • buttons `post('open-notes' | 'quit')` via the "sonar" message handler;
///   • `post('__h__:<px>')` reports document height so the popover can size to fit;
///   • Swift pushes live data with `window.sonarPopover.apply({...})`.
/// The page renders honest data only: probed liveness of the three localhost
/// services, the harness `/health` doctor line, and the harness's own `/nudges`
/// and `/events` readings. Nothing here is synthesized. Every row comes from a
/// response received in the CURRENT popover session; "nothing reported", "can't
/// reach the harness" and "snapshot expired" are three visibly different
/// renders; and no timestamp, nudge line, or count is ever invented. Swift
/// refuses to serialize rows for a stale or unreachable reading, so this page
/// has nothing to render in those states even if it tried.
///
/// (The activity feed was dropped from the original design because no `/events`
/// source existed. It exists now — that is why the section is here.)
enum PopoverHTML {
    static let width = 340

    static let html = """
<!doctype html><html><head><meta charset="utf-8"><style>
:root{
  color-scheme:dark;
  --bg:#080B10; --bg-deep:#030507; --surface:#0E141C; --elevated:#151D28; --elevated-2:#1B2735; --haze:#0B1017;
  --line:#23313F; --line-hair:rgba(255,255,255,0.06); --line-cut:rgba(0,0,0,0.60);
  --text-high:#E9EEF5; --text-dim:#7F8D9E; --text-faint:#556579;
  --accent:#E9A64A; --accent-ink:#241704; --accent-glow:#FFC061;
  --steel:#69A6CC; --positive:#5CB98E; --danger:#DC4C5A; --s3:#C9A65C;
  --font-display:"SF Pro Display",-apple-system,system-ui,sans-serif;
  --font-instr:"SF Compact Display","SF Compact Text",-apple-system,system-ui,sans-serif;
  --font-body:-apple-system,"SF Pro Text",system-ui,sans-serif;
  --font-mono:ui-monospace,"SF Mono",SFMono-Regular,Menlo,monospace;
  --r-chip:8px; --r-control:12px;
  --sh-contact:0 2px 8px rgba(0,0,0,0.55);
  --sh-toplight:inset 0 1px 0 rgba(255,255,255,0.05); --sh-cut:inset 0 -1px 0 rgba(0,0,0,0.60);
  --glow-amber:0 0 0 1px rgba(233,166,74,0.70),0 0 20px rgba(255,192,97,0.32);
  --glow-amber-text:0 0 12px rgba(255,192,97,0.35);
  --sheen:linear-gradient(180deg,rgba(255,255,255,0.06),transparent 22%);
  --gloss:linear-gradient(180deg,rgba(255,255,255,0.22),transparent 40%);
  --grain-url:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='140' height='140'%3E%3Cfilter id='g'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23g)'/%3E%3C/svg%3E");
  --tick-idle:rgba(127,141,158,0.35);
  --ease-rise:cubic-bezier(0.22,1,0.36,1);
}
*{ margin:0; padding:0; box-sizing:border-box; }
html,body{ background:var(--bg-deep); }
body{
  width:340px; font-family:var(--font-body); color:var(--text-high);
  -webkit-font-smoothing:antialiased; text-rendering:optimizeLegibility;
  background:radial-gradient(120% 90% at 78% -10%, rgba(20,29,40,0.55), transparent 60%), var(--bg-deep);
  position:relative; isolation:isolate; overflow:hidden;
}
#grain{ position:absolute; inset:0; z-index:0; background:var(--grain-url); opacity:0.045; mix-blend-mode:soft-light; pointer-events:none; }
.tick{ position:absolute; width:10px; height:10px; z-index:3; pointer-events:none; }
.tick.tl{ top:7px; left:7px; border-top:1px solid var(--tick-idle); border-left:1px solid var(--tick-idle); }
.tick.tr{ top:7px; right:7px; border-top:1px solid var(--tick-idle); border-right:1px solid var(--tick-idle); }
.tick.bl{ bottom:7px; left:7px; border-bottom:1px solid var(--tick-idle); border-left:1px solid var(--tick-idle); }
.tick.br{ bottom:7px; right:7px; border-bottom:1px solid var(--tick-idle); border-right:1px solid var(--tick-idle); }
#body{ position:relative; z-index:1; padding:15px 16px 13px; }
svg{ fill:none; stroke:currentColor; stroke-width:1.25; stroke-linecap:round; stroke-linejoin:round; }

/* header */
.head{ display:flex; align-items:center; justify-content:space-between; padding-bottom:12px; border-bottom:1px solid var(--line-hair); }
.brand{ display:flex; align-items:center; gap:9px; }
.brand .mark{ width:20px; height:20px; color:var(--text-dim); flex:none; }
.brand .wm{ font-family:var(--font-display); font-weight:700; font-size:15px; letter-spacing:0.14em; color:var(--text-high); line-height:1.05; }
.brand .sub{ font-family:var(--font-mono); font-size:11px; color:var(--text-faint); letter-spacing:0.04em; margin-top:1px; }
.chip{ display:inline-flex; align-items:center; gap:6px; height:22px; padding:0 9px; border-radius:var(--r-chip); background:var(--haze); border:1px solid var(--line-hair); box-shadow:var(--sh-toplight); }
.chip .d{ width:6px; height:6px; border-radius:50%; background:var(--text-dim); }
.chip .t{ font-family:var(--font-instr); font-size:11px; font-weight:600; letter-spacing:0.14em; color:var(--text-dim); text-transform:uppercase; }
.chip.up .d{ background:var(--positive); box-shadow:0 0 8px rgba(92,185,142,0.5); }
.chip.up .t{ color:var(--positive); }
.chip.down .d{ background:var(--danger); }
.chip.down .t{ color:var(--danger); }

/* sections */
.sect{ padding-top:14px; }
.eyebrow{ display:flex; align-items:baseline; justify-content:space-between; margin-bottom:9px; }
.eyebrow .lbl{ font-family:var(--font-instr); font-size:11px; font-weight:600; letter-spacing:0.14em; text-transform:uppercase; color:var(--text-dim); }
.eyebrow .aux{ font-family:var(--font-mono); font-size:11px; letter-spacing:0.06em; color:var(--text-faint); font-variant-numeric:tabular-nums; }

/* service rows */
.svc{ display:flex; align-items:center; gap:10px; padding:6px 0; }
.svc + .svc{ border-top:1px solid rgba(255,255,255,0.03); }
.svc .dot{ width:8px; height:8px; border-radius:50%; flex:none; background:var(--text-faint); box-shadow:inset 0 0 0 1px rgba(255,255,255,0.06); }
.svc .dot.ok{ background:var(--positive); box-shadow:inset 0 0 0 1px rgba(255,255,255,0.14); }
.svc .dot.down{ background:var(--danger); box-shadow:inset 0 0 0 1px rgba(255,255,255,0.10); }
.svc .name{ font-size:13px; font-weight:600; color:var(--text-high); letter-spacing:-0.01em; }
.svc .port{ font-family:var(--font-mono); font-size:12px; color:var(--text-dim); letter-spacing:0.04em; font-variant-numeric:tabular-nums; }
.svc .meta{ margin-left:auto; font-family:var(--font-mono); font-size:12px; letter-spacing:0.05em; font-variant-numeric:tabular-nums; color:var(--text-faint); text-transform:uppercase; }
.svc .meta.g{ color:var(--positive); }
.svc .meta.r{ color:var(--danger); }

/* doctor case-file line */
.doctor{ margin-top:10px; display:flex; align-items:center; gap:9px; padding:9px 11px; border-radius:var(--r-chip); background:var(--haze); border:1px solid var(--line-hair); box-shadow:var(--sh-cut); }
.doctor .pd{ width:6px; height:6px; border-radius:50%; background:var(--text-faint); flex:none; }
.doctor.up .pd{ background:var(--positive); box-shadow:0 0 0 3px rgba(92,185,142,0.10); }
.doctor .txt{ font-family:var(--font-mono); font-size:12px; line-height:1.5; letter-spacing:0.02em; color:var(--text-dim); font-variant-numeric:tabular-nums; }
.doctor .txt b{ color:var(--text-high); font-weight:600; }

/* nudge rows — .svc geometry with a severity dot and a wrapping line */
.nudge{ display:flex; align-items:flex-start; gap:10px; padding:7px 0; }
.nudge + .nudge{ border-top:1px solid rgba(255,255,255,0.03); }
.nudge .sev{ width:8px; height:8px; border-radius:50%; flex:none; margin-top:5px; background:var(--text-faint); box-shadow:inset 0 0 0 1px rgba(255,255,255,0.06); }
.nudge .sev.high{ background:var(--s3); box-shadow:inset 0 0 0 1px rgba(255,255,255,0.12); }
.nudge .sev.medium{ background:var(--text-dim); }
.nudge .line{ min-width:0; font-size:13px; font-weight:600; letter-spacing:-0.01em; color:var(--text-high); line-height:1.38; overflow-wrap:anywhere; display:-webkit-box; -webkit-box-orient:vertical; -webkit-line-clamp:2; overflow:hidden; }

/* activity rows — the F5 bar's #steps log idiom: zebra, no rules, all mono */
.ev{ display:flex; align-items:center; gap:9px; padding:5px 7px; border-radius:7px; font-family:var(--font-mono); font-size:12px; line-height:1.5; }
.ev:nth-child(even){ background:rgba(255,255,255,0.015); }
.ev .ico{ width:15px; height:15px; flex:none; color:var(--text-dim); }
.ev.ok .ico{ color:var(--positive); }
.ev.err .ico, .ev.err .id, .ev.err .det{ color:var(--danger); }
.ev .id{ flex:none; max-width:118px; color:var(--text-high); letter-spacing:0.02em; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.ev .det{ flex:1; min-width:0; color:var(--text-dim); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.ev .age{ margin-left:auto; flex:none; padding-left:6px; color:var(--text-faint); font-variant-numeric:tabular-nums; }

/* shared: the not-a-row renders */
.empty{ font-size:13px; color:var(--text-dim); padding:2px 0 1px; }
.empty.faint{ color:var(--text-faint); }
.note{ margin-top:6px; font-family:var(--font-mono); font-size:11px; letter-spacing:0.05em; color:var(--text-faint); font-variant-numeric:tabular-nums; }

/* Inert safety rails. The JS caps are the real bound; these engage only if a
   cap is ever bypassed, and they scroll rather than clip so nothing is hidden. */
#nudgeList{ max-height:168px; overflow-y:auto; }
#evList{ max-height:96px; overflow-y:auto; }

/* actions */
.btn{ width:100%; height:40px; display:inline-flex; align-items:center; justify-content:center; gap:8px; border-radius:var(--r-control); font-family:var(--font-body); font-weight:600; cursor:pointer; user-select:none; transition:filter .12s ease, box-shadow .12s ease, background .12s ease, border-color .12s ease, transform .12s ease; }
.btn svg{ width:16px; height:16px; flex:none; }
.btn.primary{ background:var(--gloss),var(--accent); color:var(--accent-ink); font-size:14px; border:none; box-shadow:var(--glow-amber),var(--sh-contact); }
.btn.primary svg{ color:var(--accent-ink); }
.btn.primary:hover{ filter:brightness(1.06); }
.btn.primary:active{ transform:translateY(1px); }
.btn.ghost{ height:36px; margin-top:8px; font-size:13px; background:var(--sheen),var(--elevated); color:var(--text-high); border:1px solid var(--line-hair); box-shadow:var(--sh-toplight); }
.btn.ghost svg{ color:var(--text-dim); }
.btn.ghost:hover{ background:var(--sheen),var(--elevated-2); border-color:rgba(255,255,255,0.12); }
.btn.ghost:active{ transform:translateY(1px); }

/* footer */
.foot{ margin-top:13px; padding-top:11px; border-top:1px solid var(--line-hair); display:flex; align-items:center; justify-content:space-between; font-family:var(--font-mono); font-size:11px; letter-spacing:0.06em; color:var(--text-faint); font-variant-numeric:tabular-nums; }
.foot .kbd{ display:inline-flex; align-items:center; gap:6px; color:var(--text-dim); }
.foot kbd{ font-family:var(--font-mono); font-size:10px; color:var(--text-high); background:var(--elevated); border:1px solid var(--line-hair); border-bottom-color:var(--line-cut); border-radius:5px; padding:1px 5px; box-shadow:var(--sh-toplight); }
</style></head><body>
  <span class="tick tl"></span><span class="tick tr"></span><span class="tick bl"></span><span class="tick br"></span>
  <div id="grain"></div>
  <div id="body">

    <div class="head">
      <div class="brand">
        <svg class="mark" viewBox="0 0 16 16" aria-hidden="true">
          <circle cx="4.4" cy="8" r="1.1" fill="currentColor" stroke="none"/>
          <path d="M7.4 5.2a4 4 0 0 1 0 5.6"/>
          <path d="M9.8 3.3a7 7 0 0 1 0 9.4"/>
          <path d="M12.2 1.6a9.7 9.7 0 0 1 0 12.8" opacity="0.55"/>
        </svg>
        <div>
          <div class="wm">SONAR</div>
          <div class="sub">on-device</div>
        </div>
      </div>
      <span class="chip" id="stateChip"><span class="d"></span><span class="t" id="stateText">Checking</span></span>
    </div>

    <div class="sect">
      <div class="eyebrow"><span class="lbl">Needs you</span><span class="aux" id="nudgeAux">—</span></div>
      <div id="nudgeList"></div>
      <div class="note" id="nudgeNote" hidden></div>
    </div>

    <div class="sect">
      <div class="eyebrow"><span class="lbl">Stack status</span><span class="aux">127.0.0.1</span></div>
      <div class="svc">
        <span class="dot" id="dHarness"></span>
        <span class="name">Harness</span><span class="port">:8787</span>
        <span class="meta" id="mHarness">…</span>
      </div>
      <div class="svc">
        <span class="dot" id="dBridge"></span>
        <span class="name">Overlay bridge</span><span class="port">ws:8770</span>
        <span class="meta" id="mBridge">…</span>
      </div>
      <div class="svc">
        <span class="dot" id="dNotes"></span>
        <span class="name">Notes</span><span class="port">:8771</span>
        <span class="meta" id="mNotes">…</span>
      </div>
      <div class="doctor" id="doctor">
        <span class="pd"></span>
        <span class="txt" id="doctorText">Reading harness health…</span>
      </div>
    </div>

    <div class="sect">
      <div class="eyebrow"><span class="lbl">Activity</span><span class="aux" id="evAux">—</span></div>
      <div id="evList"></div>
    </div>

    <div class="sect">
      <div class="eyebrow"><span class="lbl">Quick actions</span></div>
      <button class="btn primary" type="button" id="openNotes">
        <svg viewBox="0 0 16 16" aria-hidden="true">
          <path d="M5.4 2.8H2.8v2.6"/><path d="M10.6 2.8h2.6v2.6"/>
          <path d="M5.4 13.2H2.8v-2.6"/><path d="M10.6 13.2h2.6v-2.6"/>
          <line x1="6" y1="8" x2="10" y2="8"/>
        </svg>
        Open Notes
      </button>
      <button class="btn ghost" type="button" id="quit">
        <svg viewBox="0 0 16 16" aria-hidden="true">
          <path d="M9.5 3H12a1 1 0 0 1 1 1v8a1 1 0 0 1-1 1H9.5"/>
          <line x1="3" y1="8" x2="9.5" y2="8"/><path d="M6.4 5.4 9 8l-2.6 2.6"/>
        </svg>
        Quit Sonar
      </button>
    </div>

    <div class="foot">
      <span class="kbd"><kbd>F5</kbd> command bar</span>
      <span id="footRight">127.0.0.1</span>
    </div>

  </div>
<script>
  function post(m){ try{ window.webkit.messageHandlers.sonar.postMessage(m); }catch(_){} }
  function fit(){ post('__h__:'+Math.ceil(document.body.scrollHeight)); }
  document.getElementById('openNotes').addEventListener('click', function(){ post('open-notes'); });
  document.getElementById('quit').addEventListener('click', function(){ post('quit'); });

  var NUDGE_MAX=3, EV_MAX=3;
  var SVGNS='http://www.w3.org/2000/svg';

  function clear(el){ while(el.firstChild) el.removeChild(el.firstChild); }
  function mk(tag, cls){ var n=document.createElement(tag); if(cls) n.className=cls; return n; }
  function txt(tag, cls, s){ var n=mk(tag,cls); n.textContent = (s==null?'':String(s)); return n; }

  // Monoline 16x16 glyphs, built node-by-node so this file has zero innerHTML
  // sinks (grep-checkable). className is an SVGAnimatedString on SVG elements,
  // so the class MUST go through setAttribute — assigning .className no-ops.
  var GL={
    turn_start:   ['M8 3.2v9.6','M4.4 7.2 8 3.4l3.6 3.8'],
    tool:         ['M6.4 2.8 3.2 8l3.2 5.2','M9.6 2.8 12.8 8l-3.2 5.2'],
    tool_result_summary: ['M3.6 8h5.2','M3.6 4.8h8.8','M3.6 11.2h6.8'],
    model_switch: ['M3.2 6h7.2l-2-2','M12.8 10H5.6l2 2'],
    final:        ['M3.2 8.4l3 3 6.6-6.8'],
    unknown:      ['M4 8h8']
  };
  function glyph(kind){
    // `step` is nullable and NOT a closed enum on the wire — the harness logs an
    // unrecognized step and stores it anyway. A raw server string used as an
    // object key would otherwise reach Object.prototype ('constructor',
    // 'toString', '__proto__') and return a function, which would throw
    // mid-render.
    var paths = Object.prototype.hasOwnProperty.call(GL, kind) ? GL[kind] : GL.unknown;
    var s=document.createElementNS(SVGNS,'svg');
    s.setAttribute('viewBox','0 0 16 16');
    s.setAttribute('aria-hidden','true');
    s.setAttribute('class','ico');
    for(var i=0;i<paths.length;i++){
      var p=document.createElementNS(SVGNS,'path');
      p.setAttribute('d', paths[i]);
      s.appendChild(p);
    }
    return s;
  }

  // Age vocabulary. Event ages floor (a log row reading 22d for 22.5d is the
  // ordinary convention); the nudge ceiling is computed in Swift and already
  // rounded UP, so it can never under-report. -1 means "no honest value" and
  // prints as an em dash, never as "now".
  function fmtAge(n){
    if(n==null || n<0) return '—';
    if(n<60) return n+'s';
    if(n<3600) return Math.floor(n/60)+'m';
    if(n<86400) return Math.floor(n/3600)+'h';
    return Math.floor(n/86400)+'d';
  }

  function renderNudges(n){
    var list=document.getElementById('nudgeList');
    var aux=document.getElementById('nudgeAux');
    var note=document.getElementById('nudgeNote');
    // Every branch clears first: the failure mode of an incremental renderer is
    // exactly a stale row surviving a state transition.
    clear(list); note.hidden=true; note.textContent='';
    aux.textContent='—';

    if(!n){ list.appendChild(txt('div','empty faint','Checking…')); return; }

    if(n.state==='ok' && n.items && n.items.length){
      aux.textContent = '≤'+fmtAge(n.ageCeilingS)+' old';
      var k=Math.min(n.items.length, NUDGE_MAX);
      for(var i=0;i<k;i++){
        var it=n.items[i]||{};
        var row=mk('div','nudge');
        // Severity picks an ALLOWLISTED literal class. The server string is
        // never concatenated into className, an attribute, or an id.
        var sevCls='sev';
        if(it.severity==='high') sevCls='sev high';
        else if(it.severity==='medium') sevCls='sev medium';
        row.appendChild(mk('span',sevCls));
        // Vault-derived free text: textContent only, never HTML parsing.
        row.appendChild(txt('div','line', it.line));
        list.appendChild(row);
      }
      var hidden=(n.returned|0)-k;
      if(hidden>0){
        // OUR truncation, so it can be stated. Never phrased as a total: the
        // harness already capped the list silently and its `count` is post-cap,
        // so the true number is unknowable from here.
        note.textContent = hidden+' more returned, not shown';
        note.hidden=false;
      }
      return;
    }

    if(n.state==='ok' || n.state==='empty'){
      // The age ceiling still prints here: a FRESH nothing is what distinguishes
      // this from the unreachable/stale states, which show an em dash.
      aux.textContent = '≤'+fmtAge(n.ageCeilingS)+' old';
      // Describes the RESPONSE, not the world. The harness answers a wedged
      // engine with a byte-identical empty snapshot, so "nothing needs you"
      // would be a claim this surface cannot back.
      list.appendChild(txt('div','empty','No nudges reported'));
      return;
    }
    if(n.state==='unreachable'){
      list.appendChild(txt('div','empty faint',"Can't reach the harness — nudges unknown"));
      return;
    }
    if(n.state==='malformed'){
      list.appendChild(txt('div','empty faint','Unexpected response from harness'));
      return;
    }
    if(n.state==='stale'){
      list.appendChild(txt('div','empty faint','Snapshot expired — rechecking…'));
      return;
    }
    list.appendChild(txt('div','empty faint','Checking…'));
  }

  function renderActivity(a){
    var list=document.getElementById('evList');
    var aux=document.getElementById('evAux');
    clear(list);
    aux.textContent='—';

    if(!a){ list.appendChild(txt('div','empty faint','Checking…')); return; }

    if(a.state==='ok' && a.items && a.items.length){
      // /events carries no staleness metadata and its retention sweep only runs
      // on store-open and every 500 appends, so the newest row's age is stated
      // up front rather than implied by the word "recent".
      aux.textContent = 'newest '+fmtAge(a.newestAgeS);
      var k=Math.min(a.items.length, EV_MAX);
      for(var i=0;i<k;i++){
        var it=a.items[i]||{};
        // Allowlisted status -> literal class. Anything else stays neutral.
        var rowCls='ev';
        if(it.status==='ok') rowCls='ev ok';
        else if(it.status==='error') rowCls='ev err';
        var row=mk('div',rowCls);
        row.appendChild(glyph(it.kind));
        row.appendChild(txt('span','id', it.label));
        // `detail` is absent on some steps and present-but-empty on others.
        // Omit the span entirely rather than printing filler that would look
        // like content.
        if(it.detail){ row.appendChild(txt('span','det', it.detail)); }
        row.appendChild(txt('span','age', fmtAge(it.ageS)));
        list.appendChild(row);
      }
      return;
    }

    if(a.state==='ok' || a.state==='empty'){
      // Again: describes the response. {"events":[]} also means "the durable
      // read failed and the ring was empty", indistinguishable from here.
      list.appendChild(txt('div','empty','No steps returned'));
      return;
    }
    if(a.state==='unreachable'){
      list.appendChild(txt('div','empty faint',"Can't reach the harness — history unknown"));
      return;
    }
    if(a.state==='malformed'){
      list.appendChild(txt('div','empty faint','Unexpected response from harness'));
      return;
    }
    list.appendChild(txt('div','empty faint','Checking…'));
  }

  function renderStatus(s){
    var chip=document.getElementById('stateChip'), st=document.getElementById('stateText');
    chip.classList.remove('up','down');
    if(s.harnessUp){ chip.classList.add('up'); st.textContent='Ready'; }
    else{ chip.classList.add('down'); st.textContent='Offline'; }

    function svc(dotId, metaId, up, okText){
      var d=document.getElementById(dotId), m=document.getElementById(metaId);
      d.classList.remove('ok','down');
      m.classList.remove('g','r');
      if(up){ d.classList.add('ok'); m.textContent=okText; m.classList.add('g'); }
      else{ d.classList.add('down'); m.textContent='Down'; m.classList.add('r'); }
    }
    svc('dHarness','mHarness', s.harnessUp, 'OK');
    svc('dBridge','mBridge', s.bridgeUp, 'Up');
    svc('dNotes','mNotes', s.notesUp, 'Up');

    var doc=document.getElementById('doctor'), dt=document.getElementById('doctorText');
    doc.classList.remove('up');
    if(s.harnessUp){
      doc.classList.add('up');
      // Build with a text node for the model — it comes from /health, so never
      // route it through innerHTML (a model id with HTML metachars would inject).
      dt.textContent='';
      var b=document.createElement('b'); b.textContent='Harness reachable';
      dt.appendChild(b);
      dt.appendChild(document.createTextNode(' · '+(s.tools|0)+' tools · '+
        (s.chunks|0).toLocaleString()+' chunks · '+(s.model||'?')));
    } else {
      dt.textContent='Harness offline — start the stack (sonar.sh up)';
    }
  }

  // Each section renders in isolation: an exception in one must not leave the
  // others unpainted, and fit() must run either way so the popover never sizes
  // itself to a half-drawn page.
  function apply(s){
    try{ renderStatus(s); }catch(e){ post('__err__:status'); }
    try{ renderNudges(s && s.nudges); }catch(e){ post('__err__:nudges'); }
    try{ renderActivity(s && s.activity); }catch(e){ post('__err__:activity'); }
    fit();
  }
  window.sonarPopover={ apply:apply };
  window.addEventListener('load', fit); setTimeout(fit, 40);
</script></body></html>
"""
}

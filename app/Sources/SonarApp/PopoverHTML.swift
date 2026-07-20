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
/// The page renders honest data only — the three localhost services (probed for
/// liveness) and the harness `/health` doctor line. No fabricated activity feed.
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
  --steel:#69A6CC; --positive:#5CB98E; --danger:#DC4C5A;
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
      <div class="eyebrow"><span class="lbl">Stack status</span><span class="aux">127.0.0.1</span></div>
      <div class="svc">
        <span class="dot" id="dHarness"></span>
        <span class="name">Harness</span><span class="port">:8787</span>
        <span class="meta" id="mHarness">…</span>
      </div>
      <div class="svc">
        <span class="dot" id="dVoice"></span>
        <span class="name">Voice loop</span><span class="port">ws:8770</span>
        <span class="meta" id="mVoice">…</span>
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

  function cls(el, on, off){ el.classList.remove(on, off); }
  function apply(s){
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
    svc('dVoice','mVoice', s.voiceUp, 'Up');
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
    fit();
  }
  window.sonarPopover={ apply:apply };
  window.addEventListener('load', fit); setTimeout(fit, 40);
</script></body></html>
"""
}

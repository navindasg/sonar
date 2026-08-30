import Foundation

/// The Gotham Noir F5 command bar, ported VERBATIM from the page that shipped
/// inside `spike/glow/init.lua` (BAR_HTML) so the native panel renders the same
/// bar the Hammerspoon overlay does — dark-glass panel milled from near-black,
/// HUD corner-ticks, SF Mono telemetry, and ONE light (status dot + caret + mic
/// fill) whose hue tracks state: graphite idle -> ice blue-steel while
/// listening/thinking -> sodium amber while speaking.
///
/// The Lua<->JS bridge is preserved exactly, because the page is unchanged and
/// only the HOST swapped (hs.webview -> WKWebView):
///
///   host -> page   window.sonar.{setHeard,setBusy,clearTurn,appendAnswer,
///                                addStep,setState,setLevel}
///                  window.focusCmd()
///   page -> host   "<typed question>"   run one harness turn
///                  "__esc__"            dismiss the bar entirely
///                  "__h__:<px>"         report document height for sizing
///
/// Both hosts post through a message handler named "sonar", so the page needed
/// no edits at all. Keeping it byte-identical is deliberate: it means the native
/// bar cannot drift from the Hammerspoon one while both exist, and the eventual
/// deletion of the Lua bar is a pure removal.
enum CommandBarHTML {
    /// Matches BAR_W/BAR_H in spike/glow/init.lua:378 — the panel is sized to
    /// the page, then re-sized by the page's own __h__ report.
    static let width = 752
    static let height = 150

    static let html = """
<!doctype html><html><head><meta charset="utf-8"><style>
  :root{
    color-scheme:dark;
    --surface:#0E141C; --elevated:#151D28; --haze:#0B1017;
    --line:#23313F; --line-hair:rgba(255,255,255,0.06);
    --text-high:#E9EEF5; --text-dim:#7F8D9E; --text-faint:#556579;
    --positive:#5CB98E; --danger:#DC4C5A; --rain:#33485B;
    --font-instr:"SF Compact Display","SF Compact Text",-apple-system,system-ui,sans-serif;
    --font-body:-apple-system,"SF Pro Text",system-ui,sans-serif;
    --font-mono:ui-monospace,"SF Mono",SFMono-Regular,Menlo,monospace;
    --sh-toplight:inset 0 1px 0 rgba(255,255,255,0.05);
    --glass-bg:rgba(10,13,18,0.82);
    --sheen:linear-gradient(180deg,rgba(255,255,255,0.06),transparent 22%);
    --grain-url:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='140' height='140'%3E%3Cfilter id='g'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23g)'/%3E%3C/svg%3E");
    /* the ONE light + the corner-ticks; JS repoints these per state */
    --state:#7F8D9E; --state-glow:none; --tick:rgba(127,141,158,0.35);
  }
  *{ margin:0; padding:0; box-sizing:border-box; }
  html,body{ background:transparent; font-family:var(--font-body); -webkit-font-smoothing:antialiased; text-rendering:optimizeLegibility; }
  #wrap{ padding:16px 18px 20px; }
  #bar{
    position:relative;
    background:var(--sheen),var(--glass-bg);
    -webkit-backdrop-filter:blur(24px) saturate(115%) brightness(0.92);
    border:1px solid var(--line-hair);
    border-radius:16px;
    box-shadow:0 24px 70px rgba(0,0,0,0.72), 0 2px 8px rgba(0,0,0,0.55), var(--sh-toplight);
    padding:19px 22px 14px;
    isolation:isolate;
  }
  #grain{ position:absolute; inset:0; border-radius:inherit; background:var(--grain-url); opacity:0.045; mix-blend-mode:soft-light; pointer-events:none; z-index:0; }
  .tick{ position:absolute; width:10px; height:10px; z-index:2; pointer-events:none; transition:border-color .24s ease; }
  .tick.tl{ top:-5px; left:-5px; border-top:1px solid var(--tick); border-left:1px solid var(--tick); }
  .tick.tr{ top:-5px; right:-5px; border-top:1px solid var(--tick); border-right:1px solid var(--tick); }
  .tick.bl{ bottom:-5px; left:-5px; border-bottom:1px solid var(--tick); border-left:1px solid var(--tick); }
  .tick.br{ bottom:-5px; right:-5px; border-bottom:1px solid var(--tick); border-right:1px solid var(--tick); }
  #inner{ position:relative; z-index:1; }
  .eyebrow{ font-family:var(--font-instr); font-size:11px; font-weight:600; letter-spacing:0.14em; text-transform:uppercase; color:var(--text-dim); display:flex; align-items:center; gap:7px; }
  #stateChip{ color:var(--state); text-shadow:var(--state-glow); transition:color .22s ease; }
  #heard{ display:flex; align-items:flex-start; gap:13px; }
  #dot{ flex:0 0 auto; width:9px; height:9px; margin-top:4px; border-radius:50%; background:var(--state); box-shadow:none; transition:background .22s ease, box-shadow .22s ease; }
  #dot.think{ animation:pulse 1.1s ease-in-out infinite; }
  @keyframes pulse{ 0%,100%{opacity:.5} 50%{opacity:1} }
  #heardBody{ min-width:0; flex:1; }
  #heardText{ margin-top:6px; font-size:15px; line-height:1.3; color:var(--text-high); font-weight:500; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  #heardText.empty{ color:var(--text-faint); font-style:italic; font-weight:400; }
  #cmdWrap{ margin-top:14px; display:flex; align-items:center; height:48px; padding:0 15px; background:var(--elevated); border:1px solid var(--line-hair); border-radius:12px; box-shadow:var(--sh-toplight); }
  #cmd{ flex:1; background:transparent; border:0; outline:0; color:var(--text-high); font-family:var(--font-body); font-size:18px; letter-spacing:-0.01em; caret-color:var(--state); }
  #cmd::placeholder{ color:var(--text-faint); font-style:italic; }
  #enter{ flex:0 0 auto; font-family:var(--font-mono); font-size:12px; color:var(--text-dim); border:1px solid var(--line-hair); border-radius:6px; padding:2px 8px; background:var(--surface); box-shadow:var(--sh-toplight); }
  #meter{ margin-top:10px; display:flex; align-items:center; gap:10px; }
  .mlabel{ font-family:var(--font-mono); font-size:11px; letter-spacing:0.08em; color:var(--text-faint); font-variant-numeric:tabular-nums; }
  #mtrack{ flex:1; height:2px; border-radius:2px; background:var(--line); overflow:hidden; position:relative; }
  #mfill{ position:absolute; inset:0 auto 0 0; width:0%; border-radius:2px; background:linear-gradient(90deg,var(--rain),var(--state)); transition:width .12s linear; }
  #answer{ margin-top:15px; padding-top:15px; border-top:1px solid rgba(255,255,255,0.05); font-size:15px; line-height:1.55; color:var(--text-high); white-space:pre-wrap; word-wrap:break-word; display:none; }
  #answer.show{ display:block; }
  #stepsWrap{ margin-top:15px; padding-top:13px; border-top:1px solid rgba(255,255,255,0.05); display:none; }
  #stepsWrap.show{ display:block; }
  #stepsHdr{ display:flex; align-items:center; gap:8px; cursor:pointer; user-select:none; }
  #stepsHdr:hover .elabel{ color:var(--text-dim); }
  #caret{ width:12px; height:12px; color:var(--text-dim); transition:transform .12s ease; display:inline-flex; }
  #stepsWrap.open #caret{ transform:rotate(90deg); }
  .elabel{ font-family:var(--font-instr); font-size:11px; font-weight:600; letter-spacing:0.14em; text-transform:uppercase; color:var(--text-faint); }
  #stepsCount{ font-family:var(--font-mono); font-size:11px; color:var(--text-dim); font-variant-numeric:tabular-nums; border:1px solid var(--line-hair); border-radius:5px; padding:0 5px; margin-left:2px; }
  #steps{ list-style:none; margin-top:11px; display:none; flex-direction:column; gap:1px; }
  #stepsWrap.open #steps{ display:flex; }
  #steps li{ display:flex; align-items:center; gap:11px; padding:6px 7px; border-radius:7px; font-family:var(--font-mono); font-size:13px; line-height:1.5; }
  #steps li:nth-child(even){ background:rgba(255,255,255,0.015); }
  #steps li .sico{ flex:0 0 auto; width:17px; height:17px; color:var(--text-dim); display:inline-flex; }
  #steps li.ok .sico{ color:var(--positive); }
  #steps li.err, #steps li.err .sico, #steps li.err .stool{ color:var(--danger); }
  #steps li .stool{ flex:0 0 auto; color:var(--text-high); }
  #steps li .sdetail{ flex:1; min-width:0; color:var(--text-dim); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  #foot{ margin-top:15px; padding-top:12px; border-top:1px solid rgba(255,255,255,0.05); display:flex; align-items:center; gap:9px; font-family:var(--font-mono); font-size:11px; letter-spacing:0.06em; color:var(--text-faint); font-variant-numeric:tabular-nums; }
  .sep{ width:3px; height:3px; border-radius:50%; background:var(--text-faint); opacity:.5; flex:0 0 auto; }
  #foot .spacer{ flex:1; }
  #foot kbd{ font-family:var(--font-mono); font-size:10px; color:var(--text-dim); background:var(--surface); border:1px solid var(--line-hair); border-radius:5px; padding:1px 6px; }
  svg{ fill:none; stroke:currentColor; stroke-width:1.4; stroke-linecap:round; stroke-linejoin:round; }
</style></head><body><div id="wrap"><div id="bar">
  <span class="tick tl"></span><span class="tick tr"></span><span class="tick bl"></span><span class="tick br"></span>
  <div id="grain"></div>
  <div id="inner">
    <div id="heard">
      <span id="dot"></span>
      <div id="heardBody">
        <div class="eyebrow">Heard <span id="stateChip">&middot; Idle</span></div>
        <div id="heardText" class="empty">Ask, or type…</div>
      </div>
    </div>
    <div id="cmdWrap">
      <input id="cmd" type="text" autocomplete="off" spellcheck="false" placeholder="Type a question, then Enter…"/>
      <span id="enter">&#8629;</span>
    </div>
    <div id="meter"><span class="mlabel">LVL</span><span id="mtrack"><span id="mfill"></span></span><span class="mlabel" id="mval">0.00</span></div>
    <div id="answer"></div>
    <div id="stepsWrap">
      <div id="stepsHdr"><span id="caret"><svg viewBox="0 0 16 16"><path d="M6 4l4 4-4 4"/></svg></span><span class="elabel">Steps</span><span id="stepsCount">0</span></div>
      <ul id="steps"></ul>
    </div>
    <div id="foot"><span id="clock">--:--:--</span><span class="sep"></span><span id="footState">idle</span><span class="spacer"></span><kbd>&#8629;</kbd> send&nbsp;&nbsp;<kbd>esc</kbd> close</div>
  </div>
</div></div>
<script>
  var heard=document.getElementById('heardText'), cmd=document.getElementById('cmd'),
      dot=document.getElementById('dot'), chip=document.getElementById('stateChip'),
      answer=document.getElementById('answer'), stepsWrap=document.getElementById('stepsWrap'),
      steps=document.getElementById('steps'), stepsCount=document.getElementById('stepsCount'),
      mfill=document.getElementById('mfill'), mval=document.getElementById('mval'),
      footState=document.getElementById('footState'), root=document.documentElement;
  function post(m){ try{window.webkit.messageHandlers.sonar.postMessage(m);}catch(_){} }
  function fit(){ post('__h__:'+Math.ceil(document.body.scrollHeight)); }

  // The one-light state machine: cold graphite -> ice steel -> sodium amber.
  var STATES={
    idle:      {c:'#7F8D9E', box:'none', glow:'none', tick:'rgba(127,141,158,0.35)', label:'Idle'},
    listening: {c:'#69A6CC', box:'0 0 0 1px rgba(105,166,204,0.60),0 0 16px rgba(105,166,204,0.32)', glow:'0 0 10px rgba(165,212,236,0.30)', tick:'rgba(105,166,204,0.70)', label:'Listening'},
    thinking:  {c:'#69A6CC', box:'0 0 0 1px rgba(105,166,204,0.60),0 0 16px rgba(105,166,204,0.32)', glow:'0 0 10px rgba(165,212,236,0.30)', tick:'rgba(105,166,204,0.70)', label:'Thinking'},
    speaking:  {c:'#E9A64A', box:'0 0 0 1px rgba(233,166,74,0.70),0 0 20px rgba(255,192,97,0.32)', glow:'0 0 12px rgba(255,192,97,0.35)', tick:'rgba(233,166,74,0.80)', label:'Speaking'},
    error:     {c:'#DC4C5A', box:'0 0 0 1px rgba(220,76,90,0.60),0 0 14px rgba(220,76,90,0.30)', glow:'none', tick:'rgba(220,76,90,0.70)', label:'Error'}
  };
  var state='idle';
  function applyState(s){
    var st=STATES[s]||STATES.idle; state=(STATES[s]?s:'idle');
    root.style.setProperty('--state', st.c);
    root.style.setProperty('--state-glow', st.glow);
    root.style.setProperty('--tick', st.tick);
    dot.style.boxShadow=st.box;
    dot.className=(state==='thinking')?'think':'';
    chip.textContent='· '+st.label;
    footState.textContent=state;
  }
  function setState(s){ applyState(s); }
  function setHeard(t){ if(t&&t.length){ heard.textContent=t; heard.classList.remove('empty'); } else { heard.textContent='Ask, or type…'; heard.classList.add('empty'); } }
  // Turn-level busy maps onto the one-light: thinking while busy, back to idle
  // only if nothing else (a spoken/listening state) has since claimed the light.
  function setBusy(b){ if(b){ applyState('thinking'); } else if(state==='thinking'){ applyState('idle'); } }
  function setLevel(v){ var n=Math.max(0,Math.min(1,Number(v)||0)); mfill.style.width=(6+n*88).toFixed(0)+'%'; mval.textContent=n.toFixed(2); }
  function clearTurn(){ answer.textContent=''; answer.classList.remove('show'); steps.innerHTML=''; stepsWrap.classList.remove('show','open'); stepsCount.textContent='0'; fit(); }
  function appendAnswer(t){ answer.classList.add('show'); answer.textContent+=t; fit(); }

  // Monoline SF-Symbol-style glyphs replace the shipped emoji step-icons.
  var GL={
    'rag.search':'<svg viewBox="0 0 16 16"><circle cx="7" cy="7" r="4.1"/><line x1="10" y1="10" x2="13.6" y2="13.6"/></svg>',
    'note_context':'<svg viewBox="0 0 16 16"><circle cx="4" cy="4" r="1.55"/><circle cx="12.2" cy="7.6" r="1.55"/><circle cx="5.2" cy="12.4" r="1.55"/><path d="M5.4 4.7C8 5 10 6 11 6.6"/><path d="M11 9.2C8.6 9.9 7 10.9 6 11.6"/></svg>',
    'model_switch':'<svg viewBox="0 0 16 16"><path d="M2.6 5.5h8.8l-2.3-2.3"/><path d="M13.4 10.5H4.6l2.3 2.3"/></svg>',
    'final':'<svg viewBox="0 0 16 16"><path d="M3 8.4l3.1 3.1L13 4.9"/></svg>'
  };
  GL['search']=GL['rag.search']; GL['rag.note_context']=GL['note_context'];
  function stepIcon(kind){ return GL[kind]||'<svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="1.5" fill="currentColor" stroke="none"/></svg>'; }
  function addStep(kind,label,status){
    stepsWrap.classList.add('show');
    var li=document.createElement('li');
    li.className=(status==='error')?'err':(kind==='final'?'ok':'');
    var ic=document.createElement('span'); ic.className='sico'; ic.innerHTML=stepIcon(kind);
    var tool=document.createElement('span'); tool.className='stool'; tool.textContent=kind;
    var det=document.createElement('span'); det.className='sdetail'; det.textContent=label||'';
    li.appendChild(ic); li.appendChild(tool); li.appendChild(det); steps.appendChild(li);
    stepsCount.textContent=String(steps.children.length); fit();
  }
  document.getElementById('stepsHdr').addEventListener('click',function(){ stepsWrap.classList.toggle('open'); fit(); });
  cmd.addEventListener('keydown', function(e){
    if(e.key==='Enter'){ var v=cmd.value.trim(); if(v){ post(v); cmd.value=''; setHeard(v); clearTurn(); setBusy(true); } }
    if(e.key==='Escape'){ post('__esc__'); }
  });
  function tick(){ var d=new Date(); function p(n){return (n<10?'0':'')+n;} document.getElementById('clock').textContent=p(d.getHours())+':'+p(d.getMinutes())+':'+p(d.getSeconds()); }
  setInterval(tick,1000); tick();

  window.focusCmd=function(){ cmd.focus(); };
  window.sonar={setHeard:setHeard,setBusy:setBusy,clearTurn:clearTurn,appendAnswer:appendAnswer,addStep:addStep,setState:setState,setLevel:setLevel};
  applyState('idle');
  window.addEventListener('load', fit); setTimeout(fit,60);
</script></body></html>
"""
}

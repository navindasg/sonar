-- Sonar — S1 overlay spike (Hammerspoon)
-- Two throwaway pieces that lock the signature look before the native Swift app:
--   1. GLOW — a dark "cave" vignette: a grainy near-black shadow that hugs the
--             screen edges and fades to a clear centre. No colour, no cycling —
--             only its overall opacity breathes and lifts a touch when active.
--   2. BAR  — a dark command bar showing what you say (live transcript) that can
--             also be typed into when you can't talk.
-- Summon both with F13 (after the F5->F13 remap) or the fallback chord Cmd+Alt+Ctrl+G.

-- ============================================================ config
local WS_HOST = os.getenv("SONAR_GLOW_HOST") or "127.0.0.1"
local WS_PORT = os.getenv("SONAR_GLOW_PORT") or "8770"
local WS_URL  = "ws://" .. WS_HOST .. ":" .. WS_PORT
local FPS     = 30

-- Overall glow opacity per state — NO colour change, just how deep the cave is.
local INTENSITY = { idle = 0.16, listening = 0.92, thinking = 0.96, speaking = 1.0 }

-- Resolve vignette.png next to this file (works through the ~/.hammerspoon symlink).
local function scriptDir()
  local src = debug.getinfo(1, "S").source:match("@(.*)")
  local real = (src and hs.fs.pathToAbsolute(src)) or src
  return (real and real:match("(.*/)")) or "./"
end
local TEXTURE = scriptDir() .. "vignette.png"

-- ============================================================ state
local M = {
  canvases = {}, ws = nil, animTimer = nil, screenWatcher = nil,
  phase = 0.0, visible = false, state = "idle", level = 0.0,
  wsGen = 0, wsOpen = false, texture = nil,
  bar = nil, barUCC = nil, lastTyped = nil, committed = "",
  summoned = false, summonHideTimer = nil,
}
M.texture = hs.image.imageFromPath(TEXTURE)

-- ============================================================ glow (cave vignette)
local function buildCanvas(screen)
  local canvas = hs.canvas.new(screen:fullFrame())
  canvas:level(hs.canvas.windowLevels.screenSaver)
  canvas:behaviorAsLabels({ "canJoinAllSpaces", "stationary", "fullScreenAuxiliary" })
  canvas:canvasMouseEvents(false, false, false, false)  -- fully click-through
  canvas:replaceElements({
    { type = "image", image = M.texture, imageScaling = "scaleToFill", imageAlpha = 1.0 },
  })
  canvas:alpha(0.0)
  return canvas
end

local function currentIntensity()
  local target = INTENSITY[M.state] or INTENSITY.idle
  local breath = 0.5 + 0.5 * math.sin(M.phase)          -- slow, calm
  local lift = 0.90 + 0.10 * breath
  local levelGain = 0.85 + 0.15 * M.level
  return math.max(0.0, math.min(1.0, target * lift * levelGain))
end

local function render()
  local a = currentIntensity()
  for _, canvas in ipairs(M.canvases) do canvas:alpha(a) end
end

local function showAll() for _, c in ipairs(M.canvases) do c:show() end end
local function hideAll() for _, c in ipairs(M.canvases) do c:hide() end end

local function layout()
  for _, c in ipairs(M.canvases) do c:delete() end
  local built = {}
  local ok, screens = pcall(hs.screen.allScreens)
  if ok and screens then
    for _, screen in ipairs(screens) do
      local okc, canvas = pcall(buildCanvas, screen)
      if okc and canvas then built[#built + 1] = canvas
      else print("[sonar] canvas build failed: " .. tostring(canvas)) end
    end
  end
  M.canvases = built
  render()
  if M.visible then showAll() else hideAll() end
end

-- ============================================================ bar (command bar)
-- 752 = a 720px "Gotham Noir" glass panel + ~16px of #wrap padding each side so
-- the four HUD corner-ticks (drawn at -5px) and the ambient glow aren't clipped.
local BAR_W, BAR_H = 752, 150

-- "Gotham Noir" command bar: dark-glass panel milled from near-black, HUD
-- corner-ticks, SF Mono telemetry, and ONE light (the status dot + caret + mic
-- fill) whose HUE tracks state — graphite idle -> ice blue-steel while
-- listening/thinking -> sodium-amber while speaking. The JS bridge is unchanged:
-- Lua drives window.sonar.{setHeard,setBusy,clearTurn,appendAnswer,addStep} +
-- the NEW setState/setLevel, and reads back typed text / __esc__ / __h__:N.
local BAR_HTML = [[<!doctype html><html><head><meta charset="utf-8"><style>
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
</script></body></html>]]

local function barRect(screen)
  -- Top-right corner, just below the menu bar.
  local f = screen:fullFrame()
  local margin = 22
  return hs.geometry.rect(f.x + f.w - BAR_W - margin, f.y + 40, BAR_W, BAR_H)
end

local function buildBar()
  if M.bar then return end
  local ok, err = pcall(function()
    M.barUCC = hs.webview.usercontent.new("sonar")
    M.barUCC:setCallback(function(msg)
      local body = msg and msg.body
      if type(body) ~= "string" then return end
      -- Esc is advertised as "close" in the bar footer, so it must FULLY dismiss
      -- (hide the glow + stop the mic), not merely hide the webview and leave the
      -- vignette up with the STT still listening. M.dismiss is defined once the
      -- overlay teardown (hideOverlay) exists; fall back to hideBar pre-init.
      if body == "__esc__" then (M.dismiss or M.hideBar)(); return end
      local h = body:match("^__h__:(%d+)$")
      if h then M.resizeBar(tonumber(h)); return end
      M.lastTyped = body
      M.sendText(body)   -- run this typed question through the harness
    end)
    local w = hs.webview.new(barRect(hs.screen.primaryScreen()), { developerExtrasEnabled = false }, M.barUCC)
    w:windowStyle({ "borderless" })
    w:transparent(true)
    w:allowTextEntry(true)
    w:shadow(false)
    pcall(function() w:level(hs.canvas.windowLevels.screenSaver) end)
    pcall(function() w:behaviorAsLabels({ "canJoinAllSpaces", "stationary", "fullScreenAuxiliary" }) end)
    w:html(BAR_HTML)
    M.bar = w
  end)
  if not ok then print("[sonar-bar] build failed: " .. tostring(err)); M.bar = nil end
end

function M.showBar()
  buildBar()
  if not M.bar then return end
  M.bar:frame(barRect(hs.screen.primaryScreen()))
  M.bar:show()
  pcall(function() M.bar:bringToFront(true) end)
  hs.timer.doAfter(0.12, function()
    if M.bar then
      pcall(function() M.bar:hswindow():focus() end)
      M.bar:evaluateJavaScript("window.focusCmd && focusCmd()")
    end
  end)
end

function M.hideBar()
  if M.bar then M.bar:hide() end
end

function M.setTranscript(t)
  if M.bar then
    local safe = (t or ""):gsub("\\", "\\\\"):gsub("'", "\\'")
    M.bar:evaluateJavaScript("setHeard('" .. safe .. "')")
  end
end

-- Accumulate the CURRENT utterance: live partials append to whatever has already
-- been finalized this utterance, so pausing between phrases doesn't wipe the box.
-- But once a turn has fully answered (turnDone), the next utterance is a brand-new
-- exchange: wipe the previous answer + steps and start the transcript fresh, so
-- speaking again gives a clean box instead of piling onto the last turn.
function M.onTranscript(text, partial)
  if M.turnDone then
    M.turnDone = false
    M.committed = ""
    M.evalBar("window.sonar && sonar.clearTurn()")
  end
  local joined = (M.committed ~= "" and (M.committed .. " ") or "") .. text
  if not partial then M.committed = joined end
  M.setTranscript(joined)
end

-- ---- typed-turn helpers (Stream C: box <-> harness via overlay/bridge.py) ----
local function jsEsc(s)
  return (tostring(s or "")):gsub("\\", "\\\\"):gsub("'", "\\'"):gsub("\n", "\\n"):gsub("\r", "")
end

function M.evalBar(js)
  if M.bar then pcall(function() M.bar:evaluateJavaScript(js) end) end
end

-- Repoint the bar's ONE light to `s`, but only when it actually changes. The
-- {state} field rides the same ~10 msg/s stream as {level}, so pushing it
-- unconditionally would spam evaluateJavaScript with identical no-op renders;
-- M.barState dedups it (the JS webview persists across show/hide, so the cache
-- stays truthful). All state-change paths (RX, F5 show, summon) route here.
function M.pushState(s)
  if s and s ~= M.barState then
    M.barState = s
    M.evalBar(("window.sonar && sonar.setState('%s')"):format(jsEsc(s)))
  end
end

-- Grow/shrink the command bar to fit its content (JS reports document height).
function M.resizeBar(h)
  if not M.bar then return end
  local target = math.max(120, math.min(560, (tonumber(h) or BAR_H) + 4))
  local f = M.bar:frame()
  M.bar:frame(hs.geometry.rect(f.x, f.y, BAR_W, target))
end

-- A summoned box (proactive push, e.g. the morning brief) auto-hides after a
-- linger UNLESS the user takes it over — opens/closes the overlay, or types into
-- it. Any of those calls this to cancel the pending auto-hide and forget it was
-- a push, so the box behaves like a normal user session from then on.
function M.clearSummon()
  M.summoned = false
  if M.summonHideTimer then M.summonHideTimer:stop(); M.summonHideTimer = nil end
end

-- Send a typed question to the bridge, which runs it through the harness.
function M.sendText(t)
  M.clearSummon()   -- typing means the user has taken the box over
  if M.ws and M.wsOpen then
    pcall(function() M.ws:send(hs.json.encode({ text = t })) end)
  else
    M.evalBar("window.sonar && sonar.appendAnswer('[bridge not connected — start overlay/bridge.py]')")
    M.evalBar("window.sonar && sonar.setBusy(false)")
  end
end

-- Render one harness step-event into the expandable "steps taken" panel. The
-- bar now shows the tool NAME as its own mono chip, so `detail` is just the
-- descriptive tail (no "tool:" prefix) — the JS composes the two.
function M.renderStep(e)
  local kind = e.tool or e.step or "tool"
  local detail
  if e.tool then
    detail = e.detail or ""
  elseif e.step == "model_switch" then
    detail = e.detail or "model switch"
  elseif e.step == "final" then
    detail = e.detail or "done"
  elseif e.step == "turn_start" then
    return   -- redundant with the typed question already shown in the box
  else
    detail = e.detail or ""
  end
  M.evalBar(("window.sonar && sonar.addStep('%s','%s','%s')"):format(
    jsEsc(kind), jsEsc(detail), jsEsc(e.status or "ok")))
end

-- ============================================================ websocket
local connect
-- Reconnect is driven by the heartbeat in start(), which checks the socket's REAL
-- status(). A server killed abruptly (bridge <-> voice swap, KeepAlive respawn)
-- often never delivers a "closed"/"fail" callback, so the old cached-flag logic
-- left M.wsOpen stale-true and never retried. Each connect() bumps M.wsGen so a
-- late event from a superseded socket can't clobber the current one.
connect = function()
  M.wsGen = (M.wsGen or 0) + 1
  local gen = M.wsGen
  -- Close any prior socket first so reconnects don't pile up stale connections
  -- on the server (each would double-drive the mic).
  if M.ws then pcall(function() M.ws:close() end); M.ws = nil; M.wsOpen = false end
  local ok, ws = pcall(hs.websocket.new, WS_URL, function(status, message)
    if gen ~= M.wsGen then return end   -- stale socket: ignore its late events
    if status == "open" then M.wsOpen = true
    elseif status == "received" then
      M.rxCount = (M.rxCount or 0) + 1
      M.lastRx = message
      local okd, data = pcall(hs.json.decode, message)
      if okd and type(data) == "table" then
        if data.state then
          M.state = data.state
          -- Repoint the bar's ONE light (dot + caret + mic fill + tick) to the
          -- new state so the cold->amber ramp renders in the glass bar too
          -- (deduped — see M.pushState).
          M.pushState(data.state)
        end
        if data.level ~= nil then
          M.level = tonumber(data.level) or M.level
          -- Throttle: the level streams at audio rate; only push a fresh mic
          -- reading to the bar when it moves enough to see (~3%).
          local lvl = tonumber(data.level) or 0
          if math.abs(lvl - (M.barLevel or -1)) > 0.03 then
            M.barLevel = lvl
            M.evalBar(("window.sonar && sonar.setLevel(%.3f)"):format(lvl))
          end
        end
        if data.summon then M.summonBox(data.text) end
        if data.transcript ~= nil then
          M.rxTranscript = data.transcript
          M.onTranscript(data.transcript, data.partial == true)
        end
        if data.answer ~= nil and data.answer ~= "" then
          M.evalBar(("window.sonar && sonar.appendAnswer('%s')"):format(jsEsc(data.answer)))
        end
        if type(data.step) == "table" then M.renderStep(data.step) end
        if data.turn == "start" then M.evalBar("window.sonar && sonar.setBusy(true)")
        elseif data.turn == "end" then
          M.evalBar("window.sonar && sonar.setBusy(false)")
          M.turnDone = true   -- next utterance clears the box (see M.onTranscript)
          if M.summoned then M.scheduleSummonHide() end
        end
      else
        print("[sonar-rx] decode failed: " .. tostring(message))
      end
    elseif status == "closed" or status == "fail" then
      M.wsOpen = false   -- connect() owns M.ws; don't null a possibly-newer socket
    end
  end)
  if ok then M.ws = ws else M.ws = nil; M.wsOpen = false end
end

-- ============================================================ summon + wiring
-- Push-to-talk: the overlay is up only while the key is held.
local function sendCmd(cmd)
  if M.ws then pcall(function() M.ws:send(hs.json.encode({ cmd = cmd })) end) end
end
local function showOverlay()
  if M.visible then return end
  M.clearSummon()    -- a real F5 press: this is now a user session, not a push
  M.committed = ""   -- fresh transcript each time the box opens
  M.visible = true; M.state = "listening"; render(); showAll(); M.showBar()
  hs.timer.doAfter(0.16, function()
    M.evalBar("window.sonar && sonar.clearTurn()")
    M.pushState("listening")
  end)
  sendCmd("start")   -- ack to the bridge (glow state)
end
local function hideOverlay()
  if not M.visible then return end
  M.clearSummon()    -- dismissing (F5 or auto-hide): stop tracking it as a push
  M.visible = false; hideAll(); M.hideBar()
  sendCmd("stop")    -- tell the STT bridge to stop listening
end
-- Full dismissal for the bar's own Esc affordance (routed from the JS bridge).
function M.dismiss() hideOverlay() end
local function toggleOverlay()
  if M.visible then hideOverlay() else showOverlay() end
end

-- ---- proactive push (summon) --------------------------------------------------
-- The morning brief speaks on its OWN short-lived connection, so the glow that
-- draws the box is a different client. The voice loop broadcasts {summon, text}
-- to every client; here we reveal the (normally F5-gated) box and show the whole
-- message at once — WITHOUT opening the mic (no sendCmd("start")).
local SUMMON_LINGER_S = tonumber(os.getenv("SONAR_SUMMON_LINGER_S") or "") or 30
function M.summonBox(text)
  if M.summonHideTimer then M.summonHideTimer:stop(); M.summonHideTimer = nil end
  M.summoned = true
  M.committed = ""
  M.visible = true; M.state = "speaking"; render(); showAll(); M.showBar()
  -- One deferred callback (the freshly-(re)built webview needs a beat to load):
  -- clear THEN set the text together, so no stray clearTurn can wipe it — the
  -- clear-race that showOverlay's own delayed clearTurn would otherwise cause.
  local msg = text or ""
  hs.timer.doAfter(0.16, function()
    -- A proactive push (e.g. the morning brief) is Sonar speaking: light the
    -- ONE affordance warm amber, not the steel "thinking" of a normal turn.
    M.evalBar("window.sonar && sonar.clearTurn()")
    M.pushState("speaking")
    if msg ~= "" then
      M.evalBar(("window.sonar && sonar.appendAnswer('%s')"):format(jsEsc(msg)))
    end
  end)
end

-- Once the brief finishes speaking (turn:end), keep the box up for a linger so it
-- can be read, then hide it — but only if the user never took it over.
function M.scheduleSummonHide()
  if M.summonHideTimer then M.summonHideTimer:stop() end
  M.summonHideTimer = hs.timer.doAfter(SUMMON_LINGER_S, function()
    M.summonHideTimer = nil
    if M.summoned then hideOverlay() end
  end)
end

local function start()
  pcall(require, "hs.ipc")   -- enable the `hs -c` CLI port (for headless driving)
  layout()
  M.screenWatcher = hs.screen.watcher.new(layout):start()
  -- Auto-reload when init.lua is edited (handy while iterating on the look).
  M.configWatcher = hs.pathwatcher.new(scriptDir(), function(files)
    for _, f in ipairs(files) do
      if f:sub(-4) == ".lua" then hs.timer.doAfter(0.3, hs.reload); return end
    end
  end):start()
  M.animTimer = hs.timer.doEvery(1 / FPS, function()
    M.phase = (M.phase + (2 * math.pi * 0.12) / FPS) % (2 * math.pi)   -- ~8s breath
    if M.visible then render() end
  end)
  connect()
  -- Heartbeat: reconnect whenever the socket isn't actually open. We query the
  -- socket's REAL status() rather than the cached M.wsOpen flag, because a server
  -- killed abruptly may never fire a close callback — the flag would stay stale-
  -- true and we'd never reconnect. On localhost a dead port fails fast, so this
  -- re-links the glow within ~2s of a bridge<->voice swap or a KeepAlive respawn.
  M.heartbeat = hs.timer.doEvery(2, function()
    local st = (M.ws and M.ws:status()) or "closed"
    if st ~= "open" and st ~= "connecting" then connect() end
  end)
  -- Toggle on F13 (the remapped F5): press to show, press again to hide.
  hs.hotkey.bind({}, "f13", toggleOverlay)
  -- Same toggle on the fallback chord (test without the remap).
  hs.hotkey.bind({ "cmd", "alt", "ctrl" }, "g", toggleOverlay)
  if not M.texture then hs.alert.show("Sonar: vignette.png missing — run gen_vignette.py", 3) end
  hs.alert.show("Sonar overlay loaded — F13 or ⌘⌥⌃G", 1.2)
end

-- ipc test hooks: drive headlessly via `hs -c` (canvas + webview need no Accessibility).
sonarGlow = {
  toggle = toggleOverlay,
  show = showOverlay,
  hide = hideOverlay,
  setState = function(s, lvl) M.state = s or M.state; M.level = tonumber(lvl) or M.level; render() end,
  say = function(t) M.visible = true; render(); showAll(); M.showBar(); M.setTranscript(t) end,
  -- Push raw JS into the command bar for headless visual QA (no harness/TTS):
  --   hs -c 'sonarGlow.eval("sonar.setState(\'speaking\'); sonar.appendAnswer(\'hi\')")'
  eval = function(js) M.evalBar(js) end,
  -- Drive a full typed turn headlessly (opens the overlay, runs it through the
  -- harness):  hs -c 'sonarGlow.ask("what does my note say about X?")'
  ask = function(t)
    showOverlay()
    hs.timer.doAfter(0.35, function()
      M.evalBar("window.sonar && sonar.clearTurn(); window.sonar && sonar.setBusy(true)")
      M.sendText(t)
    end)
  end,
  status = function()
    return hs.inspect({
      visible = M.visible, state = M.state, level = M.level, screens = #M.canvases,
      ws_connected = M.ws ~= nil, ws_open = M.wsOpen, texture = M.texture ~= nil, bar = M.bar ~= nil,
      lastTyped = M.lastTyped, rxCount = M.rxCount or 0, lastRx = M.lastRx, rxTranscript = M.rxTranscript,
    })
  end,
}

start()

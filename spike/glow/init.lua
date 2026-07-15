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
local BAR_W, BAR_H = 720, 132

local BAR_HTML = [[<!doctype html><html><head><meta charset="utf-8"><style>
  :root { color-scheme: dark; }
  * { margin:0; padding:0; box-sizing:border-box; }
  html,body { background:transparent; font-family:-apple-system,"SF Pro Text",system-ui,sans-serif; }
  #wrap { padding:10px; }
  #bar {
    background:rgba(11,12,15,0.86);
    border:1px solid rgba(255,255,255,0.07);
    border-radius:16px;
    box-shadow:0 20px 60px rgba(0,0,0,0.6), inset 0 1px 0 rgba(255,255,255,0.04);
    -webkit-backdrop-filter:blur(24px) saturate(115%);
    padding:14px 18px;
  }
  #heard { display:flex; align-items:center; gap:10px; min-height:20px; margin-bottom:11px; }
  #dot { width:8px; height:8px; border-radius:50%; background:#9aa0ac; box-shadow:0 0 9px rgba(154,160,172,.7); flex:0 0 auto; }
  #dot.busy { background:#5b9dff; box-shadow:0 0 12px rgba(91,157,255,.9); animation:pulse 1.1s ease-in-out infinite; }
  @keyframes pulse { 0%,100%{opacity:.35} 50%{opacity:1} }
  #heardText { color:#aab0bb; font-size:13px; letter-spacing:.2px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  #heardText.empty { color:#5b616c; font-style:italic; }
  #cmd { width:100%; background:transparent; border:0; outline:0; color:#eef1f6; font-size:18px; letter-spacing:.2px; caret-color:#9aa0ac; }
  #cmd::placeholder { color:#5b616c; }
  #answer { color:#e7ebf2; font-size:15px; line-height:1.55; margin-top:12px; white-space:pre-wrap; word-wrap:break-word; display:none; }
  #answer.show { display:block; }
  #stepsWrap { margin-top:12px; border-top:1px solid rgba(255,255,255,0.06); padding-top:8px; display:none; }
  #stepsWrap.show { display:block; }
  #stepsHdr { color:#7d8590; font-size:12px; letter-spacing:.3px; cursor:pointer; user-select:none; display:flex; align-items:center; gap:6px; }
  #stepsHdr:hover { color:#aab0bb; }
  #caret { display:inline-block; transition:transform .12s ease; }
  #stepsWrap.open #caret { transform:rotate(90deg); }
  #steps { list-style:none; margin-top:8px; display:none; }
  #stepsWrap.open #steps { display:block; }
  #steps li { color:#9aa0ac; font-size:12.5px; line-height:1.7; display:flex; gap:8px; align-items:baseline; }
  #steps li.err { color:#e0787f; }
  #steps .ico { flex:0 0 auto; }
</style></head><body><div id="wrap"><div id="bar">
  <div id="heard"><span id="dot"></span><span id="heardText" class="empty">Ask, or type…</span></div>
  <input id="cmd" type="text" autocomplete="off" spellcheck="false" placeholder="Type a question, then Enter…"/>
  <div id="answer"></div>
  <div id="stepsWrap"><div id="stepsHdr"><span id="caret">▸</span><span id="stepsLabel">steps</span></div><ul id="steps"></ul></div>
</div></div>
<script>
  const heard=document.getElementById('heardText'), cmd=document.getElementById('cmd'),
        dot=document.getElementById('dot'), answer=document.getElementById('answer'),
        stepsWrap=document.getElementById('stepsWrap'), steps=document.getElementById('steps'),
        stepsLabel=document.getElementById('stepsLabel');
  function post(m){ try{window.webkit.messageHandlers.sonar.postMessage(m);}catch(_){} }
  function fit(){ post('__h__:'+Math.ceil(document.body.scrollHeight)); }
  function setHeard(t){ if(t&&t.length){ heard.textContent=t; heard.classList.remove('empty'); } else { heard.textContent='Ask, or type…'; heard.classList.add('empty'); } }
  function setBusy(b){ dot.classList.toggle('busy', !!b); }
  function clearTurn(){ answer.textContent=''; answer.classList.remove('show'); steps.innerHTML=''; stepsWrap.classList.remove('show','open'); stepsLabel.textContent='steps'; fit(); }
  function appendAnswer(t){ answer.classList.add('show'); answer.textContent+=t; fit(); }
  const ICON={'rag.search':'🔍','search':'🔍','rag.note_context':'🧵','note_context':'🧵','model_switch':'🧠','final':'✓'};
  function addStep(kind,label,status){
    stepsWrap.classList.add('show');
    const li=document.createElement('li'); if(status==='error') li.className='err';
    const ic=document.createElement('span'); ic.className='ico'; ic.textContent=ICON[kind]||'·';
    const tx=document.createElement('span'); tx.textContent=label;
    li.appendChild(ic); li.appendChild(tx); steps.appendChild(li);
    stepsLabel.textContent='steps ('+steps.children.length+')'; fit();
  }
  document.getElementById('stepsHdr').addEventListener('click',()=>{ stepsWrap.classList.toggle('open'); fit(); });
  cmd.addEventListener('keydown', e => {
    if(e.key==='Enter'){ const v=cmd.value.trim(); if(v){ post(v); cmd.value=''; setHeard(v); clearTurn(); setBusy(true);} }
    if(e.key==='Escape'){ post('__esc__'); }
  });
  window.focusCmd=()=>cmd.focus();
  window.sonar={setHeard,setBusy,clearTurn,appendAnswer,addStep};
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
      if body == "__esc__" then M.hideBar(); return end
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

-- Grow/shrink the command bar to fit its content (JS reports document height).
function M.resizeBar(h)
  if not M.bar then return end
  local target = math.max(120, math.min(560, (tonumber(h) or BAR_H) + 4))
  local f = M.bar:frame()
  M.bar:frame(hs.geometry.rect(f.x, f.y, BAR_W, target))
end

-- Send a typed question to the bridge, which runs it through the harness.
function M.sendText(t)
  if M.ws and M.wsOpen then
    pcall(function() M.ws:send(hs.json.encode({ text = t })) end)
  else
    M.evalBar("window.sonar && sonar.appendAnswer('[bridge not connected — start overlay/bridge.py]')")
    M.evalBar("window.sonar && sonar.setBusy(false)")
  end
end

-- Render one harness step-event into the expandable "steps taken" panel.
function M.renderStep(e)
  local kind = e.tool or e.step or "tool"
  local label
  if e.tool then
    label = e.tool .. (e.detail and (": " .. e.detail) or "")
  elseif e.step == "model_switch" then
    label = e.detail or "model switch"
  elseif e.step == "final" then
    label = "done"
  elseif e.step == "turn_start" then
    return   -- redundant with the typed question already shown in the box
  else
    label = (e.step or "step") .. (e.detail and (": " .. e.detail) or "")
  end
  M.evalBar(("window.sonar && sonar.addStep('%s','%s','%s')"):format(
    jsEsc(kind), jsEsc(label), jsEsc(e.status or "ok")))
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
        if data.state then M.state = data.state end
        M.level = tonumber(data.level) or M.level
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
  M.committed = ""   -- fresh transcript each time the box opens
  M.visible = true; M.state = "listening"; render(); showAll(); M.showBar()
  hs.timer.doAfter(0.16, function() M.evalBar("window.sonar && sonar.clearTurn()") end)
  sendCmd("start")   -- ack to the bridge (glow state)
end
local function hideOverlay()
  if not M.visible then return end
  M.visible = false; hideAll(); M.hideBar()
  sendCmd("stop")    -- tell the STT bridge to stop listening
end
local function toggleOverlay()
  if M.visible then hideOverlay() else showOverlay() end
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

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
local RECONNECT_AFTER = 2.0

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
  reconnecting = false, wsOpen = false, texture = nil,
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
local BAR_W, BAR_H = 720, 112

local BAR_HTML = [[<!doctype html><html><head><meta charset="utf-8"><style>
  :root { color-scheme: dark; }
  * { margin:0; padding:0; box-sizing:border-box; }
  html,body { background:transparent; font-family:-apple-system,"SF Pro Text",system-ui,sans-serif; }
  #wrap { padding:10px; }
  #bar {
    background:rgba(11,12,15,0.84);
    border:1px solid rgba(255,255,255,0.07);
    border-radius:16px;
    box-shadow:0 20px 60px rgba(0,0,0,0.6), inset 0 1px 0 rgba(255,255,255,0.04);
    -webkit-backdrop-filter:blur(24px) saturate(115%);
    padding:14px 18px;
  }
  #heard { display:flex; align-items:center; gap:10px; min-height:20px; margin-bottom:11px; }
  #dot { width:8px; height:8px; border-radius:50%; background:#9aa0ac; box-shadow:0 0 9px rgba(154,160,172,.7); flex:0 0 auto; animation:pulse 1.9s ease-in-out infinite; }
  @keyframes pulse { 0%,100%{opacity:.3} 50%{opacity:.95} }
  #heardText { color:#aab0bb; font-size:13.5px; letter-spacing:.2px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  #heardText.empty { color:#5b616c; font-style:italic; }
  #cmd { width:100%; background:transparent; border:0; outline:0; color:#eef1f6; font-size:18px; letter-spacing:.2px; caret-color:#9aa0ac; }
  #cmd::placeholder { color:#5b616c; }
</style></head><body><div id="wrap"><div id="bar">
  <div id="heard"><span id="dot"></span><span id="heardText" class="empty">Listening…</span></div>
  <input id="cmd" type="text" autocomplete="off" spellcheck="false" placeholder="Speak, or type here…"/>
</div></div>
<script>
  const heard = document.getElementById('heardText'), cmd = document.getElementById('cmd');
  function setHeard(t){ if(t&&t.length){ heard.textContent=t; heard.classList.remove('empty'); } else { heard.textContent='Listening…'; heard.classList.add('empty'); } }
  cmd.addEventListener('keydown', e => {
    if(e.key==='Enter'){ const v=cmd.value.trim(); if(v){ try{window.webkit.messageHandlers.sonar.postMessage(v);}catch(_){}; cmd.value=''; setHeard('you typed: '+v);} }
    if(e.key==='Escape'){ try{window.webkit.messageHandlers.sonar.postMessage('__esc__');}catch(_){} }
  });
  window.focusCmd = () => cmd.focus();
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
      if body == "__esc__" then M.hideBar(); return end
      M.lastTyped = body
      print("[sonar-bar] typed: " .. tostring(body))
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

-- Accumulate turns: live partials of the current turn append to whatever has
-- already been finalized this session, so pausing between phrases doesn't wipe
-- the box. A final turn commits; the next turn's partials append after it.
function M.onTranscript(text, partial)
  local joined = (M.committed ~= "" and (M.committed .. " ") or "") .. text
  if not partial then M.committed = joined end
  M.setTranscript(joined)
end

-- ============================================================ websocket
local connect
local function scheduleReconnect()
  if M.reconnecting then return end
  M.reconnecting = true
  hs.timer.doAfter(RECONNECT_AFTER, function() M.reconnecting = false; connect() end)
end
connect = function()
  -- Close any prior socket first so reloads/heartbeats don't pile up stale
  -- connections on the bridge (each would double-drive the mic).
  if M.ws then pcall(function() M.ws:close() end); M.ws = nil; M.wsOpen = false end
  local ok, ws = pcall(hs.websocket.new, WS_URL, function(status, message)
    if status == "open" then M.reconnecting = false; M.wsOpen = true
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
      else
        print("[sonar-rx] decode failed: " .. tostring(message))
      end
    elseif status == "closed" or status == "fail" then
      M.ws = nil; M.wsOpen = false; scheduleReconnect()
    end
  end)
  if ok then M.ws = ws else scheduleReconnect() end
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
  sendCmd("start")   -- tell the STT bridge to start listening
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
  -- Heartbeat: if the socket isn't open, reconnect. Survives the STT bridge
  -- restarting without needing a manual hs.reload().
  M.heartbeat = hs.timer.doEvery(3, function()
    if not M.wsOpen and not M.reconnecting then connect() end
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
  status = function()
    return hs.inspect({
      visible = M.visible, state = M.state, level = M.level, screens = #M.canvases,
      ws_connected = M.ws ~= nil, ws_open = M.wsOpen, texture = M.texture ~= nil, bar = M.bar ~= nil,
      lastTyped = M.lastTyped, rxCount = M.rxCount or 0, lastRx = M.lastRx, rxTranscript = M.rxTranscript,
    })
  end,
}

start()

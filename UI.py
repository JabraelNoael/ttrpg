"""UI.py — a tiny local web UI for the TTRPG, served from the Python standard library (no extra
dependencies). It wraps repl.dispatch() so EVERY command behaves exactly as in the shell, and
exposes the world as JSON for the page to render.

Run:   python3 UI.py        then open  http://localhost:8765
Stop:  Ctrl-C

Design notes (kept deliberately simple + all in one file so you can redesign freely):
  - The whole frontend is the PAGE string at the bottom (HTML + a little vanilla JS, no frameworks).
  - Backend endpoints:
      GET  /            -> the page
      GET  /state       -> the whole world as JSON
      POST /cmd {line}  -> run a command, return {output, state}
      GET  /complete?line=...  -> autocomplete candidates (reuses repl's context-aware completer)
  - It runs in ONE process sharing repl's WORLD, so commands and the view stay in sync.
  - CAVEAT: a few commands prompt with input() (undo-confirm, execution_check, the function editor).
    With no console those auto-resolve (undo->keep, execution->final kill); the multi-line function
    editor isn't usable from the box yet — edit functions in the shell for now.
"""
import io
import os
import re
import sys
import json
import contextlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

# The whole frontend lives in ui.html (edit it live: save -> refresh the browser, no restart needed).
# The PAGE string at the bottom is just the seed/fallback used if ui.html is ever missing.
HTML_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui.html")
HELP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reference.md")


def page_html():
    """Read ui.html fresh on every request (so edits show on the next browser refresh). Falls back
    to the embedded PAGE if the file is missing."""
    try:
        with open(HTML_FILE, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return PAGE


class _Server(HTTPServer):
    allow_reuse_address = True  # rebind immediately on restart (no "address already in use" TIME_WAIT)

import repl

_DELIMS = " \t{,>"  # mirrors repl's readline completer delimiters


def _newest_saved_id():
    """The id of the most-recently-modified saved session (so a bare relaunch reopens your latest
    world), or None if there are no saves yet."""
    ids = repl._saved_session_ids()
    if not ids:
        return None
    return max(ids, key=lambda sid: os.path.getmtime(repl._session_path(sid)))


def _boot_session(arg):
    """Decide which world to host before the server binds its port:
      arg in (None / "")          -> load the NEWEST saved session (relaunch reopens your world);
                                     if there are no saves, start a fresh empty session.
      arg in ("new"/"fresh"/"-n") -> always start a fresh empty session.
      arg = <id|name>             -> load that specific saved session.
    Returns the port to host on (= the loaded/allocated session id). Loading sets repl.SESSION_ID,
    so the UI hosts on the SAME id the world was saved under (stable URL)."""
    arg = (arg or "").strip()
    if arg.lower() in ("new", "fresh", "-n", "--new"):
        return repl._ensure_session_id()                 # fresh empty world on the lowest free port
    target = arg or _newest_saved_id()
    if target in (None, ""):
        return repl._ensure_session_id()                 # nothing saved yet -> fresh
    result = repl.cmd_session(f"load {target}")          # loads world + sets repl.SESSION_ID to its id
    if not result:
        print(f"  (couldn't load '{target}' — starting a fresh session instead)")
        return repl._ensure_session_id()
    return repl._ensure_session_id()                     # now returns the loaded session's id


def run_command(line):
    """Run one command through repl.dispatch, capturing whatever it prints.
    stdin is swapped to an empty stream so any interactive prompt (parse-warning
    confirm, execution check, structure picker) takes its non-interactive default
    instead of blocking the HTTP thread on a keypress nobody is typing into the
    server's terminal. For parse warnings that default is 'keep' — the warnings
    are still printed (and shown in the console)."""
    buffer = io.StringIO()
    saved_stdin = sys.stdin
    sys.stdin = io.StringIO()  # not a tty, reads as EOF -> every prompt auto-resolves
    try:
        with contextlib.redirect_stdout(buffer):
            try:
                repl.dispatch(line)
            except Exception as error:       # never let the server die on a bad command
                print(f"  error: {error}")
    finally:
        sys.stdin = saved_stdin
    return buffer.getvalue().rstrip("\n")


def get_state():
    """The whole world as JSON-able data the page renders."""
    cells = repl.MAP.get("cells", {})
    weather, entities = {}, []
    with repl._DISPATCH_LOCK:  # _weather_at syncs shared weather state — don't race a running command
        for key in cells:
            try:
                x, y = (int(n) for n in key.split(","))
                weather[key] = repl._weather_at(x, y)[0]
            except (ValueError, KeyError):
                pass
        for name, entity in repl.WORLD.items():
            pos = repl._entity_pos(entity)
            if pos is None:
                continue
            kind = "mob" if repl._is_mob(entity) else ("npc" if repl._is_npc(entity) else "player")
            entities.append({"name": name, "x": pos[0], "y": pos[1], "kind": kind,
                             "symbol": entity.nbt.get("symbol") or ""})
    return {
        "turn": repl.TURN,
        "active": repl.TURN_ACTIVE,
        "players": {name: repl._player_to_dict(p) for name, p in repl.WORLD.items()},
        "map": repl.MAP,
        "biomes": repl.BIOMES,                                   # name -> {label,color,glyph,temperature,...}
        "body_coverage": repl.BODY_COVERAGE,                     # normalized slot -> default % of body covered
        "weather": weather,                                      # "x,y" -> weather name (for the atlas overlay)
        "entities": entities,                                    # positioned players/npcs/mobs for atlas markers
        "structures": [s.as_dict() for s in repl.STRUCTURES],    # each carries pos:[x,y]
        "history": list(repl.HISTORY)[-60:],
        "functions": {k: list(v) for k, v in repl.FUNCTIONS.items()},
        "hooks": {name: {"name": h.name, "on": h.on, "context": h.context,
                         "condition": repr(h.condition) if h.condition else "", "run": repr(h.run) if h.run else "",
                         "description": h.description} for name, h in repl.WORLD_HOOKS.items()},
        "calendar": repl._calendar_year(repl.TURN),
    }


def complete(line):
    """Autocomplete candidates for a command line, reusing repl._complete_line (context-aware)."""
    text = line
    for delim in _DELIMS:
        text = text.rsplit(delim, 1)[-1]
    try:
        return list(repl._complete_line(line, text))[:40]
    except Exception:
        return []


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")  # never serve a stale ui.html / state
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            self._send(200, page_html(), "text/html")
        elif url.path == "/state":
            self._send(200, json.dumps(get_state(), default=str))
        elif url.path == "/complete":
            line = parse_qs(url.query).get("line", [""])[0]
            self._send(200, json.dumps(complete(line)))
        elif url.path == "/help":               # the formatting reference, read fresh (edit reference.md live)
            try:
                with open(HELP_FILE, encoding="utf-8") as fh:
                    body = fh.read()
            except OSError:
                body = "# Reference\n\n(reference.md not found — create it next to ui.py)"
            self._send(200, body, "text/markdown")
        elif re.fullmatch(r"/[\w.-]+\.html", url.path or ""):   # serve sibling .html files (e.g. /hook.html prototype)
            try:
                with open(os.path.join(os.path.dirname(__file__), url.path.lstrip("/")), encoding="utf-8") as fh:
                    self._send(200, fh.read(), "text/html")
            except OSError:
                self._send(404, "not found")
        else:
            self._send(404, "{}")

    def do_POST(self):
        if urlparse(self.path).path == "/cmd":
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or "{}")
            output = run_command(payload.get("line", ""))
            self._send(200, json.dumps({"output": output, "state": get_state()}, default=str))
        else:
            self._send(404, "{}")

    def log_message(self, *args):
        pass  # quiet server


PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>TTRPG</title>
<style>
  * { box-sizing: border-box; }
  :root { --gold:#f5c542; --bg:#14161a; --panel:#1b1e24; --line:#2c313a; --ink:#d7dae0; --footerH:156px; }
  /* BULLETPROOF dock: console is absolutely pinned to the bottom (fixed height); #app fills the
     rest absolutely. No grid/flex track can collapse, so the console never floats. */
  body { margin: 0; font: 15px/1.4 ui-monospace, Menlo, Consolas, monospace;
         background: var(--bg); color: var(--ink); position: relative; height: 100vh; overflow: hidden; }
  /* hotkey letter (Warcraft style): the access key glows gold, rest stays default */
  .hk { color: var(--gold); }
  #app { position: absolute; top: 0; left: 0; right: 0; bottom: var(--footerH);
         display: flex; flex-direction: column; overflow: hidden; }
  #app.menu #topbar { display: none; }
  #topbar { flex: none; display: flex; align-items: center; gap: 14px; padding: 8px 12px;
            background: #1d2026; border-bottom: 1px solid var(--line); }
  #topbar .navbtns { display: flex; gap: 8px; }
  #topbar button { padding: 5px 11px; cursor: pointer; background: #21252c; color: var(--ink);
                   border: 1px solid var(--line); border-radius: 6px; font: inherit; }
  #topbar button:hover { border-color: #4c6286; } #topbar button:disabled { opacity: .4; cursor: default; }
  #topbar kbd { font-size: 11px; color: #9aa3b2; border: 1px solid var(--line); border-radius: 4px; padding: 0 4px; margin-left: 4px; }
  #crumb { color: #9aa3b2; font-size: 13px; margin-left: auto; }
  #screen { flex: 1; min-height: 0; position: relative; overflow: hidden; }

  /* MAIN MENU — two seamless full-bleed buttons, no borders */
  .menu { position: absolute; inset: 0; display: grid; grid-template-rows: 1fr 1fr; }
  .menu .slab { display: flex; align-items: center; justify-content: center; font-size: 64px; cursor: pointer;
                color: #1b1e24; user-select: none; transition: filter .12s; }
  .menu .slab:hover { filter: brightness(1.08); }
  .menu .slab.players { background: #6f9ad6; } .menu .slab.world { background: #7fae6a; }
  .menu .slab .hk { color: #fff; }

  /* PLAYER PICKER */
  .picker { position: absolute; inset: 0; display: flex; flex-direction: column; }
  .picker .p { flex: 1; display: flex; align-items: center; justify-content: center; font-size: 42px;
               cursor: pointer; color: #14161a; }
  .picker .p:hover { filter: brightness(1.08); }

  /* PLAYER WHEEL */
  /* the wheel FILLS the screen: viewBox is 100x100, scaled to COVER (slice) so wedges reach the edges */
  .wheelwrap { position: absolute; inset: 0; overflow: hidden; }
  #wheelsvg { width: 100%; height: 100%; display: block; }
  .slice { cursor: pointer; transition: filter .1s; } .slice:hover { filter: brightness(1.12); }
  .slabel { fill: #14161a; text-anchor: middle; dominant-baseline: middle; pointer-events: none; }
  .slabel .ttl { font: 600 4.4px ui-monospace, Menlo, monospace; } .slabel .hk { fill: var(--gold); }
  .slabel .sub { font: 500 2.9px ui-monospace, Menlo, monospace; fill: #14161add; }
  .clabel { fill: var(--ink); font: 600 5px ui-monospace, Menlo, monospace; text-anchor: middle; dominant-baseline: middle;
            pointer-events: none; } .clabel .hk { fill: var(--gold); }

  /* GENERIC SUB-PAGE / scroll surface */
  .page { position: absolute; inset: 0; overflow: auto; padding: 14px 18px; }
  .card { background: var(--panel); border: 1px solid var(--line); border-radius: 7px; padding: 10px 12px; margin: 0 0 12px; }
  .card h3 { margin: 0 0 8px; font-size: 13px; color: #8ab4f8; }
  .kv { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 2px 12px; }
  .kv span { color: #9aa3b2; } .kv b { color: #e8eaed; font-weight: 500; }
  .pill { display: inline-block; background: #21252c; border: 1px solid var(--line); border-radius: 12px;
          padding: 2px 9px; margin: 2px 3px 2px 0; }
  .muted { color: #6b7280; }
  .grid { display: grid; gap: 3px; }
  .cell { border: 1px solid var(--line); border-radius: 4px; padding: 4px 6px; min-height: 46px; background: var(--panel); cursor: pointer; }
  .cell:hover { border-color: #4c6286; } .cell .b { color: #9aa3b2; font-size: 11px; } .cell .o { font-size: 18px; }

  /* INVENTORY */
  .invgrid { display: grid; grid-template-columns: 360px 1fr; gap: 12px; align-items: start; }
  .zone { border-radius: 8px; padding: 8px 10px; margin-bottom: 10px; max-height: 168px; overflow: auto; }
  .zone h4 { margin: 0 0 6px; font-size: 12px; text-transform: uppercase; letter-spacing: .5px; }
  .zone.armor { background: #3a3d44; } .zone.accessories { background: #4a3a57; } .zone.clothing { background: #57502f; }
  .slotrow { display: flex; align-items: center; gap: 8px; margin: 3px 0; }
  .slotrow label { width: 120px; color: #cfd3da; } .slotrow select { flex: 1; background: var(--bg); color: var(--ink);
            border: 1px solid var(--line); border-radius: 5px; padding: 3px 6px; font: inherit; }
  .radars { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px,1fr)); gap: 8px; }
  .radar { border-radius: 8px; padding: 6px; } .radar .t { font-size: 12px; margin-bottom: 2px; color:#222; }
  .flatbox { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px; }
  .flatbox .kv b { color: var(--gold); }
  .itemgrid { display: grid; grid-template-columns: repeat(auto-fill, minmax(72px,72px)); gap: 8px; }
  .itembox { height: 72px; border: 1px solid var(--line); border-radius: 8px; background: var(--panel);
             display: flex; flex-direction: column; align-items: center; justify-content: center; cursor: pointer; }
  .itembox:hover { border-color: #4c6286; } .itembox .em { font-size: 30px; } .itembox .n { font-size: 10px; color:#9aa3b2; }
  .itembox.add { font-size: 34px; color: var(--gold); }

  /* ITEM EDITOR */
  .editor { max-width: 760px; } .editor .row { display: flex; gap: 8px; align-items: center; margin: 6px 0; }
  .editor label.f { width: 120px; color: #9aa3b2; }
  .editor input, .editor select, .editor textarea { background: var(--bg); color: var(--ink); border: 1px solid var(--line);
            border-radius: 5px; padding: 4px 7px; font: inherit; }
  .editor input[type=text], .editor textarea { flex: 1; } .editor textarea { min-height: 34px; resize: vertical; }
  .sub { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px; margin: 8px 0; }
  .sub h4 { margin: 0 0 6px; font-size: 12px; color: #8ab4f8; }
  .todo { border-left: 3px solid #b5803f; } .todo h4 { color: #d9a441; }
  .hint { color: #6b7280; font-size: 12px; }
  /* clickable chips (replace the tiny checkboxes — whole chip toggles) */
  .chips { display: flex; flex-wrap: wrap; gap: 7px; }
  .chip { display: inline-flex; align-items: center; gap: 7px; padding: 6px 12px; border-radius: 16px; cursor: pointer;
          border: 1px solid var(--line); background: #20242b; user-select: none; }
  .chip .box { width: 15px; height: 15px; border: 1px solid #6b7280; border-radius: 4px; flex: none; }
  .chip:hover { border-color: #4c6286; } .chip.on { border-color: var(--gold); background: #2a2e25; }
  .chip.on .box { background: var(--gold); border-color: var(--gold); }
  .chip.cat-armor { color: #c8ccd2; } .chip.cat-accessories { color: #c9a7e6; } .chip.cat-clothing { color: #e6d27a; }
  /* right-aligned label/value stat grid */
  .statgrid { display: grid; grid-template-columns: repeat(auto-fill, minmax(210px, 1fr)); gap: 6px 14px; }
  .statgrid .sg { display: grid; grid-template-columns: 1fr 92px; align-items: center; gap: 8px; }
  .statgrid .sg label { text-align: right; color: #9aa3b2; } .statgrid .sg input { width: 92px; }
  .warn { color: #e06c6c; font-size: 12px; }

  /* ABILITY CARDS */
  .ability-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 10px; }
  .ability-card { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; }
  .ability-card .ab-name { font-size: 14px; font-weight: 600; color: #e8eaed; }
  .ability-card .ab-lvl { font-size: 11px; color: #9aa3b2; margin-left: 6px; }
  .ability-card .ab-desc { color: #9aa3b2; font-size: 12px; margin: 4px 0 8px; }
  .ability-card .ab-row { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 4px; }
  .ability-tag { font-size: 11px; padding: 2px 7px; border-radius: 10px; border: 1px solid; white-space: nowrap; }
  .ab-cast { border-color: #6a8fd8; color: #8ab4f8; background: #1c2540; }
  .ab-cooldown { border-color: #6b7280; color: #9aa3b2; background: #1e2128; }
  .ab-cost { border-color: #b5803f; color: #d9a441; background: #28200e; }
  .ab-affinity { border-color: #6a4fb5; color: #b9a8d8; background: #1e1630; }
  .ab-damage { border-color: #c0392b; color: #e88; background: #2a1212; }
  .ab-onhit { border-color: #2e7d32; color: #81c784; background: #0d200e; }
  .ab-nbt { border-color: var(--line); color: #9aa3b2; background: var(--bg); }

  /* CONSOLE — absolutely pinned to the bottom, fixed height (--footerH) */
  footer { position: absolute; left: 0; right: 0; bottom: 0; height: var(--footerH); overflow: visible;
           border-top: 1px solid var(--line); background: #1d2026; padding: 8px 12px; }
  footer .lbl { color: #6b7280; font-size: 11px; text-transform: uppercase; letter-spacing: .5px; margin-bottom: 4px; }
  #log { height: 84px; overflow: auto; white-space: pre-wrap; color: #aab; background: var(--bg);
         border: 1px solid var(--line); border-radius: 5px; padding: 6px 8px; margin-bottom: 6px; font-size: 13px; }
  #cmdrow { position: relative; display: flex; gap: 6px; }
  #cmd { flex: 1; padding: 7px 9px; background: var(--bg); color: #e8eaed; border: 1px solid #3a4150; border-radius: 5px; font: inherit; }
  #ac { position: absolute; bottom: 38px; left: 0; right: 60px; background: var(--panel); border: 1px solid #3a4150;
        border-radius: 5px; max-height: 180px; overflow: auto; display: none; z-index: 9999; }
  #ac div { padding: 4px 9px; cursor: pointer; } #ac div:hover, #ac div.sel { background: #314056; }
  button.go { padding: 7px 14px; background: #314056; color: #e8eaed; border: 1px solid #4c6286; border-radius: 5px; cursor: pointer; }
</style></head>
<body>
  <div id="app">
    <div id="topbar">
      <div class="navbtns">
        <button id="backbtn" onclick="back()">‹ back<kbd>esc</kbd></button>
        <button onclick="home()">⌂ menu<kbd>`</kbd></button>
      </div>
      <div id="crumb"></div>
    </div>
    <div id="screen"></div>
  </div>
  <footer>
    <div class="lbl">console</div>
    <div id="log"></div>
    <div id="cmdrow">
      <div id="ac"></div>
      <input id="cmd" autocomplete="off" placeholder="/command …  (Tab to complete, Enter to run · ` returns to menu)">
      <button class="go" onclick="runCmd()">Run</button>
    </div>
  </footer>

<script>
let STATE = null;
// ---- navigation state machine ------------------------------------------------
// NAV.stack is the breadcrumb; NAV.cur is the current screen. ` -> menu, Esc -> back.
const NAV = { stack: [], cur: {kind: "menu"} };
let KEYMAP = {};   // per-screen single-key -> action, rebuilt on every render

function go(screen) { NAV.stack.push(NAV.cur); NAV.cur = screen; render(); }
function replace(screen) { NAV.cur = screen; render(); }
function back() { if (NAV.stack.length) { NAV.cur = NAV.stack.pop(); render(); } }
function home() { NAV.stack = []; NAV.cur = {kind: "menu"}; render(); }

// ---- hotkey-label helpers (gold access letter) -------------------------------
function hkHTML(label, key) {
  const i = label.toLowerCase().indexOf(key.toLowerCase());
  if (i < 0) return label + ` <span class="hk">(${key})</span>`;
  return label.slice(0, i) + `<span class="hk">${label[i]}</span>` + label.slice(i + 1);
}
function hkSVG(label, key) {
  const i = label.toLowerCase().indexOf(key.toLowerCase());
  if (i < 0) return label;
  return label.slice(0, i) + `<tspan class="hk">${label[i]}</tspan>` + label.slice(i + 1);
}

async function refresh() {
  STATE = await (await fetch("/state")).json();
  // if the player we were viewing vanished, fall back to menu
  if (NAV.cur.player && !STATE.players[NAV.cur.player]) home();
  else render();
}

// ---- render dispatcher -------------------------------------------------------
function render() {
  const app = document.getElementById("screen");
  KEYMAP = {};
  document.getElementById("app").classList.toggle("menu", NAV.cur.kind === "menu");
  document.getElementById("backbtn").disabled = NAV.stack.length === 0;
  document.getElementById("crumb").textContent = crumbText();
  const k = NAV.cur.kind;
  if (k === "menu")    app.innerHTML = renderMenu();
  else if (k === "picker") app.innerHTML = renderPicker();
  else if (k === "wheel")  app.innerHTML = renderWheel();
  else if (k === "inv")    app.innerHTML = renderInventory();
  else if (k === "item")   app.innerHTML = renderItemEditor();
  else if (k === "world")  app.innerHTML = renderWorld();
  else app.innerHTML = renderSubpage();
}
function crumbLabel(s) {
  if (s.kind === "menu") return "menu";
  if (s.kind === "picker") return "players";
  if (s.kind === "world") return "world";
  if (s.kind === "wheel") return s.player;
  if (s.kind === "inv") return "inventory";
  if (s.kind === "item") return s.item == null ? "item creation" : "item editor";
  if (s.kind === "sub") return s.slice;
  return s.kind;
}
function crumbText() { return NAV.stack.concat([NAV.cur]).map(crumbLabel).join("  ›  "); }

// ---- MAIN MENU ---------------------------------------------------------------
function renderMenu() {
  KEYMAP["p"] = () => go({kind: "picker"});
  KEYMAP["w"] = () => go({kind: "world"});
  return `<div class="menu">
    <div class="slab players" onclick="KEYMAP['p']()">${hkHTML("Players", "p")}</div>
    <div class="slab world" onclick="KEYMAP['w']()">${hkHTML("World", "w")}</div>
  </div>`;
}

// ---- PIE WHEEL (shared by the player wheel AND the player picker) ------------
// FORMULA: N segments fill the circle, each spanning 360/N degrees; segment 0 is CENTERED at the
// top (12 o'clock) and they go clockwise. The wedges run all the way to the centre; a hub circle is
// painted ON TOP for the centre button. N=1 -> one full disc; N=2 -> two half-circles; etc.
function pieWheel(segs, center) {
  // 100x100 viewBox, scaled to FIT the screen (preserveAspectRatio meet) so the WHOLE wheel shows,
  // zoomed out and centered (a circle can't fill a rectangle without cropping, so we letterbox).
  // rO ~49 keeps the wheel just inside the viewBox. center is optional — omit for no hub.
  const n = segs.length, w = 360 / n, cx = 50, cy = 50, rO = 49, rC = center ? 13 : 0, rL = 28;
  const P = (deg, r) => { const a = deg * Math.PI / 180; return [(cx + r * Math.sin(a)).toFixed(2), (cy - r * Math.cos(a)).toFixed(2)]; };
  let svg = "";
  segs.forEach((s, i) => {
    if (n === 1) {
      svg += `<circle cx="${cx}" cy="${cy}" r="${rO}" fill="${s.fill}" stroke="#14161a" stroke-width=".5" class="slice" onclick="${s.action}"/>`;
    } else {
      const [x1, y1] = P(i * w - w / 2, rO), [x2, y2] = P(i * w + w / 2, rO), big = w > 180 ? 1 : 0;
      const d = `M${cx} ${cy} L${x1} ${y1} A${rO} ${rO} 0 ${big} 1 ${x2} ${y2} Z`;
      svg += `<path d="${d}" fill="${s.fill}" stroke="#14161a" stroke-width=".5" class="slice" onclick="${s.action}"/>`;
    }
    const [lx, ly] = P(i * w, rL);
    const sub = s.sub ? `<tspan x="${lx}" dy="3.6" class="sub">${s.sub}</tspan>` : "";
    svg += `<text x="${lx}" y="${(ly - (s.sub ? 1.6 : 0)).toFixed(2)}" class="slabel"><tspan x="${lx}" class="ttl">${s.labelHTML}</tspan>${sub}</text>`;
  });
  if (center) {
    svg += `<circle cx="${cx}" cy="${cy}" r="${rC}" fill="#1b1e24" stroke="#3a4150" stroke-width=".5" class="slice"
              ${center.action ? `onclick="${center.action}"` : ""}/>`;
    svg += `<text x="${cx}" y="${cy}" class="clabel">${center.labelHTML}</text>`;
  }
  return `<div class="wheelwrap"><svg viewBox="0 0 100 100" preserveAspectRatio="xMidYMid meet" id="wheelsvg">${svg}</svg></div>`;
}

// Assign each name a single-key accelerator: its first UNUSED letter. ONLY names that get no free
// letter fall back to a number — numbered 1,2,…,9,0 (keyboard row order), starting at 1 and assigned
// in order ONLY as needed (so the first letterless name is [1], not its position). After 0 (the 10th
// number) we run out — extra letterless names get no key. Returns [{name,key,isNum}].
const NUM_ROW = ["1","2","3","4","5","6","7","8","9","0"];
function assignKeys(names) {
  const usedLetters = new Set(), out = [];
  let numIdx = 0;
  for (const name of names) {
    let key = null, isNum = false;
    for (const ch of name.toLowerCase()) { if (/[a-z]/.test(ch) && !usedLetters.has(ch)) { key = ch; usedLetters.add(ch); break; } }
    if (!key && numIdx < NUM_ROW.length) { key = NUM_ROW[numIdx++]; isNum = true; }
    out.push({name, key, isNum});
  }
  return out;
}

// ---- PLAYER PICKER (Players -> choose who -> their wheel) ---------------------
// Reuses the pie wheel: each segment = a player OBJECT (title = object/variable name, subtitle =
// the character's {name:""}). No centre button (wedges meet at the centre).
function renderPicker() {
  const names = Object.keys(STATE.players);
  if (!names.length) return `<div class="page"><span class="muted">no players yet — try
     <b>/character new hero</b> in the console.</span></div>`;
  const keyed = assignKeys(names);
  const segs = keyed.map((e, i) => {
    const p = STATE.players[e.name];
    const charName = (p.nbt && p.nbt.name) || "";
    const act = `go({kind:'wheel',player:'${e.name}'})`;
    if (e.key) KEYMAP[e.key] = () => go({kind: "wheel", player: e.name});  // only keys that were assigned
    // letter -> gold letter in the name; number fallback -> "[N] name" with N gold; none -> plain.
    const labelHTML = !e.key ? e.name
                     : e.isNum ? `[<tspan class="hk">${e.key}</tspan>] ${e.name}`
                     : hkSVG(e.name, e.key);
    return {labelHTML, sub: charName, fill: `hsl(${Math.round(i * 360 / names.length)} 40% 60%)`, action: act};
  });
  return pieWheel(segs, null);  // no centre hub
}

// ---- PLAYER WHEEL ------------------------------------------------------------
const WHEEL = [
  {label: "stats", key: "s", slice: "stats"},
  {label: "formulas", key: "f", slice: "formulas"},
  {label: "talents", key: "t", slice: "talents"},
  {label: "abilities", key: "a", slice: "abilities"},
  {label: "storyline", key: "l", slice: "storyline"},
  {label: "effects", key: "e", slice: "effects"},
  {label: "quests", key: "q", slice: "quests"},
  {label: "inventory", key: "i", slice: "inventory"},
];
function renderWheel() {
  const player = NAV.cur.player, n = WHEEL.length;
  const segs = WHEEL.map((it, i) => {
    const open = it.slice === "inventory" ? `go({kind:'inv',player:'${player}'})`
                                          : `go({kind:'sub',player:'${player}',slice:'${it.slice}'})`;
    KEYMAP[it.key] = it.slice === "inventory" ? () => go({kind: "inv", player})
                                              : () => go({kind: "sub", player, slice: it.slice});
    return {labelHTML: hkSVG(it.label, it.key), fill: `hsl(${Math.round(i * 360 / n)} 46% 46%)`, action: open};
  });
  KEYMAP["n"] = () => go({kind: "sub", player, slice: "nbt"});
  return pieWheel(segs, {labelHTML: hkSVG("nbt", "n"), action: `go({kind:'sub',player:'${player}',slice:'nbt'})`});
}

// ---- GENERIC SUB-PAGES (stats/formulas/talents/abilities/effects/quests/nbt/storyline) ----
function card(title, inner) { return `<div class="card"><h3>${title}</h3>${inner}</div>`; }
function kv(obj, skipZero) {
  const rows = Object.entries(obj || {})
    .filter(([k, v]) => v !== null && v !== "" && !(skipZero && v === 0))
    .map(([k, v]) => `<div><span>${k}</span> <b>${typeof v === "object" ? JSON.stringify(v) : v}</b></div>`);
  return rows.length ? `<div class="kv">${rows.join("")}</div>` : `<span class="muted">—</span>`;
}
function pills(arr) { return (arr && arr.length) ? arr.map(s => `<span class="pill">${s}</span>`).join("") : `<span class="muted">none</span>`; }

function stageAbility(player) {
  const g = (id) => (document.getElementById(id) || {}).value.trim();
  const name = g("ab_name"); if (!name) { alert("ability needs a name"); return; }
  const nbt = {};
  const pairs = [
    ["cast_type", g("ab_cast_type")], ["level", g("ab_level")],
    ["description", g("ab_desc")], ["cooldown", g("ab_cooldown")],
    ["cost", g("ab_cost")], ["affinity", g("ab_affinity")],
    ["damage", g("ab_damage")], ["on_hit", g("ab_on_hit")],
  ];
  for (const [k, v] of pairs) { if (v) nbt[k] = v; }
  const nbtStr = Object.keys(nbt).length ? "{" + Object.entries(nbt).map(([k,v]) => `${k}:${v}`).join(", ") + "}" : "";
  send(`/ability new ${player} ${name}${nbtStr}`);
}

function renderAbilities(abilities, player) {
  const creator = `<div class="sub" style="margin-bottom:12px">
    <h4>create</h4>
    <div class="editor">
      <div class="row"><label class="f">name</label><input type="text" id="ab_name" placeholder="polymorph"></div>
      <div class="row"><label class="f">cast_type</label><input type="text" id="ab_cast_type" placeholder="active / passive / reaction">
        <label class="f" style="width:60px">level</label><input type="text" id="ab_level" placeholder="1" style="width:60px"></div>
      <div class="row"><label class="f">description</label><input type="text" id="ab_desc" placeholder="optional flavour text"></div>
      <div class="row"><label class="f">cooldown</label><input type="text" id="ab_cooldown" placeholder="7  or  0/7">
        <label class="f" style="width:60px">affinity</label><input type="text" id="ab_affinity" placeholder="arcana" style="width:100px"></div>
      <div class="row"><label class="f">cost</label><input type="text" id="ab_cost" placeholder="[stats.mana(20)]"></div>
      <div class="row"><label class="f">damage</label><input type="text" id="ab_damage" placeholder="30">
        <label class="f" style="width:auto">on_hit</label><input type="text" id="ab_on_hit" placeholder="[stats.health(-5)]" style="flex:1;margin-left:8px"></div>
      <div class="row"><button class="go" onclick="stageAbility('${player}')">Create → stage command</button>
        <span class="hint">stages <code>/ability new</code> — review in console, then Enter</span></div>
    </div>
  </div>`;
  const tag = (cls, text) => `<span class="ability-tag ${cls}">${text}</span>`;
  const grid = (!abilities || !abilities.length)
    ? `<span class="muted">no abilities yet</span>`
    : `<div class="ability-grid">${abilities.map(ab => {
        const tags = [];
        if (ab.cast_type) tags.push(tag("ab-cast", ab.cast_type));
        if (ab.cooldown) {
          const [cur, tot] = ab.cooldown.split("/");
          tags.push(tag("ab-cooldown", parseInt(cur) <= 0 ? `cd: ${tot}t` : `cd: ${cur}/${tot}`));
        }
        if (ab.cost) tags.push(tag("ab-cost", `cost: ${ab.cost}`));
        if (ab.affinity) tags.push(tag("ab-affinity", ab.affinity));
        if (ab.damage) tags.push(tag("ab-damage", `dmg: ${ab.damage}`));
        if (ab.on_hit) tags.push(tag("ab-onhit", `on_hit: ${ab.on_hit}`));
        const knownKeys = new Set(["name","level","description","cooldown","cost","on_hit","origin","cast_type","affinity","damage"]);
        Object.entries(ab).forEach(([k, v]) => { if (!knownKeys.has(k) && v != null && v !== "") tags.push(tag("ab-nbt", `${k}: ${v}`)); });
        const lvlBadge = ab.level && ab.level !== 1 ? `<span class="ab-lvl">lv${ab.level}</span>` : "";
        const desc = ab.description ? `<div class="ab-desc">${ab.description}</div>` : "";
        return `<div class="ability-card">
          <div><span class="ab-name">${ab.name}</span>${lvlBadge}</div>
          ${desc}
          <div class="ab-row">${tags.join("") || '<span class="muted" style="font-size:11px">no fields set</span>'}</div>
        </div>`;
      }).join("")}</div>`;
  return creator + grid;
}

function renderSubpage() {
  const p = STATE.players[NAV.cur.player], slice = NAV.cur.slice;
  let body;
  if (slice === "stats") body = card("stats (non-zero)", kv(p.stats, true));
  else if (slice === "formulas") body = card("formulas", kv(p.formulas));
  else if (slice === "talents") body = card("talents", pills(p.talents));
  else if (slice === "abilities") body = card("abilities", renderAbilities(p.abilities, NAV.cur.player));
  else if (slice === "effects") body = card("effects", pills((p.effects || []).map(e => e.repr || e)));
  else if (slice === "quests") body = card("quests", pills(p.quests));
  else if (slice === "nbt") body = card(p.name + " — nbt (raw)", kv(p.nbt));
  else if (slice === "storyline") body = card("storyline",
    `<span class="muted">not implemented yet — intended as your line-by-line scribed log of what
     ${p.name} has done (may also host the overview / character-sheet header).</span>`);
  else body = card(slice, `<span class="muted">—</span>`);
  return `<div class="page">${body}</div>`;
}

// ---- WORLD (placeholder; detailed design deferred) ---------------------------
function renderWorld() {
  return `<div class="page">` + card("World",
    `<span class="muted">world screen design is deferred — focusing on the player/inventory flow first.
     The map + history still live in the console (<b>/map view</b>, <b>/history</b>).</span>`) + `</div>`;
}

// ---- INVENTORY ---------------------------------------------------------------
const EMOJI = {sword:"🗡️", blade:"🗡️", dagger:"🔪", hammer:"🔨", axe:"🪓", bow:"🏹", staff:"🪄", wand:"🪄",
  shield:"🛡️", helm:"⛑️", helmet:"⛑️", ring:"💍", amulet:"📿", necklace:"📿", potion:"🧪", scroll:"📜",
  tunic:"👕", shirt:"👕", robe:"🥋", boots:"🥾", gloves:"🧤", gem:"💎", coin:"🪙", key:"🗝️", book:"📖"};
const ARMOR = new Set(["helmet","pauldrons","chestplate","gauntlets","poleyn","graves","rerebrace","vambrace","leggings","sabatons"]);
const CLOTHING = new Set(["tunic","robe","pants","socks"]);
function slotCat(s) { return ARMOR.has(s) ? "armor" : CLOTHING.has(s) ? "clothing" : "accessories"; }
const HANDS = ["main_hand", "off_hand"];
function emojiFor(name) { const k = Object.keys(EMOJI).find(w => name.toLowerCase().includes(w)); return k ? EMOJI[k] : "📦"; }
function typeOf(repr) { return repr.split("{")[0]; }

function renderInventory() {
  const p = STATE.players[NAV.cur.player], player = NAV.cur.player;
  const slotCaps = (p.nbt && p.nbt.equip_slots) || {};         // slot -> capacity (race-seeded)
  const equipped = p.equipment || {};                          // slot -> [reprs]
  const allSlots = Array.from(new Set([...Object.keys(slotCaps), ...Object.keys(equipped)]));
  // group worn slots into the three colour zones (hand slots handled in the weapons block)
  const zones = {armor: [], accessories: [], clothing: []};
  for (const slot of allSlots) {
    if (HANDS.includes(slot)) continue;
    (zones[slotCat(slot)]).push(slot);
  }
  const slotRow = (slot) => {
    const worn = equipped[slot] || [], cap = slotCaps[slot] != null ? slotCaps[slot] : (worn.length || 1);
    const opts = worn.map(r => `<option>${typeOf(r)}</option>`).join("") || `<option>—</option>`;
    return `<div class="slotrow"><label>${slot} (${worn.length}/${cap})</label><select>${opts}</select></div>`;
  };
  const zone = (cat) => `<div class="zone ${cat}"><h4>${cat}</h4>${
    zones[cat].length ? zones[cat].map(slotRow).join("") : `<span class="muted">no ${cat} slots</span>`}</div>`;

  // weapons
  const handRow = (slot) => {
    const worn = (equipped[slot] || []).map(typeOf).join(", ") || "—";
    return `<div class="slotrow"><label>${slot.replace("_hand","")}</label><select><option>${worn}</option></select></div>`;
  };
  const extraHands = allSlots.filter(s => s.includes("hand") && !HANDS.includes(s));

  // radar charts — wired to the player's LIVE stats (these keys already exist in the backend).
  const S = p.stats || {}, v = (k) => Number(S[k] || 0);
  const radars = [
    radar("Physical Damage", ["Bludgeon","Slash","Pierce"], [v("bludgeon"),v("slash"),v("pierce")], null, "#d98a8a", "#c0392b"),
    radar("Physical Penetration", ["Bludgeon","Slash","Pierce"],
          [v("bludgeon_penetration"),v("slash_penetration"),v("pierce_penetration")], null, "#e0b27a", "#b8702a"),
    radar("Magical Damage", ["Arcana","Aether","Ethereal","Ichor"],
          [v("arcana"),v("aether"),v("ethereal"),v("ichor")], null, "#b9a8d8", "#6a4fb5"),
    radar("Physical Resistance", ["Bludgeon","Slash","Pierce"],
          [v("bludgeon_resistance"),v("slash_resistance"),v("pierce_resistance")], null, "#8aa9d9", "#2c6fb5"),
    radar("Magical Ward", ["Ichor","Arcana","Aether","Ethereal"],
          [v("ichor_ward"),v("arcana_ward"),v("aether_ward"),v("ethereal_ward")], null, "#8fc3a8", "#2f8f63"),
  ].join("");

  // loose items
  const items = (p.items || []);
  const itemCells = `<div class="itembox add" title="new item / inspect" onclick="go({kind:'item',player:'${player}',item:null})">+</div>`
    + items.map((r, i) => `<div class="itembox" onclick="go({kind:'item',player:'${player}',item:${i}})">
         <div class="em">${emojiFor(r)}</div><div class="n">${typeOf(r)}</div></div>`).join("");

  return `<div class="page"><div class="invgrid">
    <div>
      <div class="card"><h3>Equipment</h3>${zone("armor")}${zone("accessories")}${zone("clothing")}</div>
      <div class="card"><h3>Weapons</h3>${handRow("main_hand")}${handRow("off_hand")}${
        extraHands.map(handRow).join("")}</div>
    </div>
    <div>
      <div class="card"><h3>Gear Stats</h3>
        <div class="hint">wired to live player stats — per-item gear breakdown awaits item-level stats.</div>
        <div class="radars">${radars}</div>
        <div class="flatbox" style="margin-top:8px"><h3 style="margin:0 0 6px">Flat Defense</h3>
          ${kv({"armor class": v("armor_class"), "defense": v("defense")})}
          <div class="hint">flat (not %) so it lives apart from the resistance radar.</div></div>
      </div>
    </div>
  </div>
  <div class="card"><h3>Items</h3><div class="itemgrid">${itemCells}</div></div>
  </div>`;
}

// radar chart as inline SVG (no libraries). max=null -> auto-scale to the largest value.
function radar(title, labels, values, max, stroke, fill) {
  if (max == null) max = Math.max(1, ...values);
  const n = labels.length, cx = 110, cy = 108, R = 64;
  const pt = (i, r) => { const a = (i / n) * 2 * Math.PI - Math.PI / 2; return [cx + r * Math.cos(a), cy + r * Math.sin(a)]; };
  let g = "";
  for (let ring = 1; ring <= 3; ring++) {
    const pts = labels.map((_, i) => pt(i, R * ring / 3).map(v => v.toFixed(1)).join(",")).join(" ");
    g += `<polygon points="${pts}" fill="none" stroke="#00000022"/>`;
  }
  labels.forEach((lab, i) => {
    const [x, y] = pt(i, R), [lxx, lyy] = pt(i, R + 14);
    g += `<line x1="${cx}" y1="${cy}" x2="${x.toFixed(1)}" y2="${y.toFixed(1)}" stroke="#00000022"/>`;
    g += `<text x="${lxx.toFixed(1)}" y="${lyy.toFixed(1)}" font-size="8" fill="#333" text-anchor="middle">${lab}</text>`;
  });
  const poly = values.map((v, i) => pt(i, R * Math.max(0, Math.min(1, v / max))).map(c => c.toFixed(1)).join(",")).join(" ");
  g += `<polygon points="${poly}" fill="${fill}33" stroke="${stroke}" stroke-width="2"/>`;
  return `<div class="radar" style="background:${stroke}22"><div class="t">${title}</div>
    <svg viewBox="0 0 220 150" width="100%">${g}</svg></div>`;
}

// ---- ITEM EDITOR (attachment 4) ----------------------------------------------
// LIVE backend: equippable[], wield, on_use(+proc+consume), on_equip(+proc), on_unequip(+proc),
// and flat-stat deltas (folded into on_equip/on_unequip as stat.x(±n)).
// NOT yet built (orange .todo): when_equipped (continuous), item attack bludgeon/pierce/slash.
const FLAT_STATS = {
  "Defense (flat)": [["defense","defense"], ["armor_class","armor class"]],
  "Physical Resistance": [["bludgeon_resistance","bludgeon"], ["pierce_resistance","pierce"], ["slash_resistance","slash"]],
  "Magical Ward": [["arcana_ward","arcana"], ["aether_ward","aether"], ["ethereal_ward","ethereal"], ["ichor_ward","ichor"]],
  "Affinity": [["bludgeon_affinity","bludgeon"], ["pierce_affinity","pierce"], ["slash_affinity","slash"],
    ["arcana_affinity","arcana"], ["aether_affinity","aether"], ["ethereal_affinity","ethereal"], ["ichor_affinity","ichor"]],
};
function allUUIDs() { return (JSON.stringify(STATE).match(/[a-z]{2}-[A-Za-z0-9]{4}/g)) || []; }
function nbtVal(repr, key) { const m = repr.match(new RegExp(key + ':("?)([^,"}]*)\\1')); return m ? m[2] : ""; }

function renderItemEditor() {
  const p = STATE.players[NAV.cur.player], player = NAV.cur.player;
  const idx = NAV.cur.item;
  const repr = (idx != null && p.items[idx]) || "";
  const type = repr ? typeOf(repr) : "";
  const uuid = repr ? nbtVal(repr, "uuid") : "";
  const slotOpts = Object.keys((p.nbt && p.nbt.equip_slots) || {}).filter(s => !s.includes("hand"));
  const chip = (s) => `<span class="chip cat-${slotCat(s)}" data-slot="${s}"
       onclick="this.classList.toggle('on')"><span class="box"></span>${s}</span>`;
  const slotChips = slotOpts.length ? slotOpts.map(chip).join("")
    : `<span class="muted">this character has no equip_slots defined</span>`;
  const fnHint = `can use functions e.g. <b>ability:polymorph</b> and deltas e.g. <b>stat.health(1)</b>`;
  // proc + fn pair for a hook
  const hook = (id, procPH, fnPH) => `
      <div class="row"><label class="f">proc</label><input type="text" id="ie_${id}_proc" placeholder="${procPH}"></div>
      <div class="row"><label class="f">fn</label><input type="text" id="ie_${id}_fn" placeholder="${fnPH}"></div>`;
  const flatGroup = (title, rows) => `<div class="sub" style="margin:6px 0 0"><h4>${title}</h4><div class="statgrid">`
    + rows.map(([k, lab]) => `<div class="sg"><label>${lab}</label><input type="text" data-stat="${k}"></div>`).join("")
    + `</div></div>`;

  const uuidField = uuid
    ? `<input type="text" id="ie_uuid" value="${uuid}" oninput="checkUUID('${uuid}')"><span id="ie_uuidwarn" class="warn"></span>`
    : `<input type="text" value="(assigned on create)" disabled>`;

  return `<div class="page"><div class="editor">
    <div class="row"><label class="f">item</label><input type="text" id="ie_type" value="${type}" placeholder="type_id e.g. iron_sword">
      <label class="f" style="width:auto">uuid</label>${uuidField}</div>
    <div class="row"><label class="f">name</label><input type="text" id="ie_name" placeholder="display name">
      <label class="f" style="width:auto">qty</label><input type="text" id="ie_qty" value="1" style="width:60px"></div>

    <div class="sub"><h4>on use (live: <code>/item use</code>)</h4>
      <div class="chips"><span class="chip" id="ie_consume_chip" onclick="this.classList.toggle('on')"><span class="box"></span>
        destroy qty ×</span> <input type="text" id="ie_consume" value="1" style="width:54px"> on use</div>
      ${hook("onuse", "default: item_consumed (comma/space sep)", fnHint.replace(/<\/?b>/g, ""))}
      <div class="hint">${fnHint}</div>
    </div>

    <div class="sub"><h4>equip slots (live: <code>equippable[]</code>)</h4>
      <div class="chips">${slotChips}</div>
      <div class="sub" style="margin:8px 0 0"><h4>on_equip (live)</h4>${hook("onequip", "item_equipped", fnHint.replace(/<\/?b>/g, ""))}</div>
      <div class="sub" style="margin:6px 0 0"><h4>when_equipped (live: <code>/proc enable</code> ↔ <code>disable</code>)</h4>
        <div class="row"><label class="f">proc</label><input type="text" id="ie_whenequipped" placeholder="signals kept ON while worn, e.g. has_helmet"></div>
        <div class="hint">enabled (sticky) on equip, disabled on unequip — continuous while worn.</div></div>
      <div class="sub" style="margin:6px 0 0"><h4>on_unequip (live)</h4>${hook("onunequip", "item_unequipped", fnHint.replace(/<\/?b>/g, ""))}</div>
    </div>

    <div class="sub"><h4>flat gear stats (deltas → folded into on_equip / on_unequip)</h4>
      <div class="hint">each value is injected as <code>stat.x(+n)</code> on equip and <code>stat.x(−n)</code> on unequip.</div>
      ${Object.entries(FLAT_STATS).map(([t, rows]) => flatGroup(t, rows)).join("")}
    </div>

    <div class="sub"><h4>weapon (live: <code>wield</code>)</h4>
      <div class="row"><label class="f">wield</label>
        <select id="ie_wield"><option value="">— not a weapon —</option><option>main</option><option>off</option><option>both</option></select></div>
      <div class="sub todo" style="margin:6px 0 0"><h4>attack — NOT BUILT</h4>
        <div class="statgrid">
          <div class="sg"><label>bludgeon</label><input type="text" disabled></div>
          <div class="sg"><label>pierce</label><input type="text" disabled></div>
          <div class="sg"><label>slash</label><input type="text" disabled></div></div></div>
    </div>

    <div class="row"><button class="go" onclick="saveItem()">Save → stage command</button>
      <span class="hint">composes <code>/item new</code> and stages it in the console (review, then Enter).</span></div>
    <div id="ie_preview" class="hint" style="margin-top:6px"></div>
  </div></div>`;
}
function checkUUID(self) {
  const el = document.getElementById("ie_uuid"), warn = document.getElementById("ie_uuidwarn");
  const v = el.value.trim();
  const clash = v && v !== self && allUUIDs().includes(v);
  warn.textContent = clash ? "⚠ this uuid is already in use" : "";
}
function saveItem() {
  const g = (id) => (document.getElementById(id) || {}).value.trim();
  const on = (id) => { const e = document.getElementById(id); return e && e.classList.contains("on"); };
  const slots = [...document.querySelectorAll(".chip.on[data-slot]")].map(c => c.dataset.slot);
  // flat-stat deltas -> on_equip (+n) / on_unequip (-n)
  const eqDeltas = [], unDeltas = [];
  document.querySelectorAll("[data-stat]").forEach(inp => {
    const n = parseFloat(inp.value);
    if (!isNaN(n) && n !== 0) { eqDeltas.push(`stat.${inp.dataset.stat}(${n})`); unDeltas.push(`stat.${inp.dataset.stat}(${-n})`); }
  });
  const bracket = (fn, deltas) => {
    const parts = [];
    if (fn) parts.push(fn.replace(/^\[|\]$/g, ""));
    parts.push(...deltas);
    return parts.length ? "[" + parts.join(",") + "]" : "";
  };
  const nbt = {};
  if (g("ie_name")) nbt.name = g("ie_name");
  if (slots.length) nbt.equippable = "[" + slots.join(",") + "]";
  if (g("ie_wield")) nbt.wield = g("ie_wield");
  if (on("ie_consume_chip")) nbt.consume = g("ie_consume") || "1";
  const onuse = bracket(g("ie_onuse_fn"), []); if (onuse) nbt.on_use = onuse;
  if (g("ie_onuse_proc")) nbt.on_use_proc = g("ie_onuse_proc");
  const oneq = bracket(g("ie_onequip_fn"), eqDeltas); if (oneq) nbt.on_equip = oneq;
  if (g("ie_onequip_proc")) nbt.on_equip_proc = g("ie_onequip_proc");
  if (g("ie_whenequipped")) nbt.when_equipped = g("ie_whenequipped");
  const onun = bracket(g("ie_onunequip_fn"), unDeltas); if (onun) nbt.on_unequip = onun;
  if (g("ie_onunequip_proc")) nbt.on_unequip_proc = g("ie_onunequip_proc");
  const body = Object.entries(nbt).map(([k, v]) => `${k}:${v}`).join(",");
  const line = `/item new ${NAV.cur.player} ${g("ie_type") || "item"}{${body}} ${g("ie_qty") || "1"}`;
  cmd.value = line; cmd.focus();
  document.getElementById("ie_preview").innerHTML = "staged: <b>" + line.replace(/</g, "&lt;") + "</b>";
}

// ==== COMMAND BAR — autofill + run + history (unchanged core) =================
const cmd = document.getElementById("cmd"), ac = document.getElementById("ac");
let acItems = [], acSel = -1, HIST = [], histIdx = 0;
function splitTok(s) { return s.split(/([ {,>])/); }
function lastTok(s) { const p = splitTok(s); return p[p.length - 1]; }
function setLastTok(v) { const p = splitTok(cmd.value); p[p.length - 1] = v; cmd.value = p.join(""); }
function commonPrefix(a) { let p = a[0] || ""; for (const s of a) while (!s.startsWith(p)) p = p.slice(0, -1); return p; }
function acOpen() { return ac.style.display === "block"; }
function hideAC() { ac.style.display = "none"; acItems = []; acSel = -1; }
async function showAC() {
  const line = cmd.value;
  if (!line.trim()) return hideAC();
  acItems = await (await fetch("/complete?line=" + encodeURIComponent(line))).json();
  if (!acItems.length) return hideAC();
  acSel = -1;
  ac.innerHTML = acItems.map((c, i) => `<div onmousedown="event.preventDefault();acceptAC(${i})">${c}</div>`).join("");
  ac.style.display = "block";
}
function moveSel(d) {
  acSel = (acSel + d + acItems.length) % acItems.length;
  [...ac.children].forEach((el, i) => el.className = i === acSel ? "sel" : "");
}
function acceptAC(i) { if (acItems[i] == null) return; setLastTok(acItems[i] + " "); hideAC(); cmd.focus(); showAC(); }
function tabComplete() {
  if (!acItems.length) return;
  if (acSel >= 0) return acceptAC(acSel);
  if (acItems.length === 1) return acceptAC(0);
  const lcp = commonPrefix(acItems);
  if (lcp.length > lastTok(cmd.value).length) { setLastTok(lcp); showAC(); return; }
  moveSel(1);
}
function recall(d) { if (!HIST.length) return; histIdx = Math.max(0, Math.min(HIST.length, histIdx + d)); cmd.value = HIST[histIdx] || ""; }
function send(line) { cmd.value = line; runCmd(); }
async function runCmd() {
  const line = cmd.value.trim(); if (!line) return;
  HIST.push(line); histIdx = HIST.length;
  const r = await (await fetch("/cmd", {method: "POST", headers: {"Content-Type": "application/json"},
                                        body: JSON.stringify({line})})).json();
  const log = document.getElementById("log");
  log.textContent += "ttrpg> " + line + "\n" + (r.output ? r.output + "\n" : "");
  log.scrollTop = log.scrollHeight;
  cmd.value = ""; hideAC();
  STATE = r.state; render();
}
cmd.addEventListener("input", showAC);
cmd.addEventListener("keydown", (e) => {
  if (e.key === "Tab") { e.preventDefault(); tabComplete(); }
  else if (e.key === "Enter") { e.preventDefault(); runCmd(); }
  else if (e.key === "Escape") { hideAC(); cmd.blur(); }
  else if (e.key === "ArrowDown") { e.preventDefault(); acOpen() ? moveSel(1) : recall(1); }
  else if (e.key === "ArrowUp") { e.preventDefault(); acOpen() ? moveSel(-1) : recall(-1); }
});
document.addEventListener("click", (e) => { if (!ac.contains(e.target) && e.target !== cmd) hideAC(); });

// ==== GLOBAL KEYBOARD NAV =====================================================
// Active only when you're NOT typing in the console (or any input/textarea/select).
document.addEventListener("keydown", (e) => {
  const tag = (document.activeElement && document.activeElement.tagName) || "";
  const typing = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
  if (e.key === "`") { e.preventDefault(); home(); return; }   // ` ALWAYS returns to the menu
  if (typing) return;                                          // otherwise let the field have the key
  if (e.key === "Escape") { e.preventDefault(); back(); return; }
  const fn = KEYMAP[e.key.toLowerCase()];
  if (fn) { e.preventDefault(); fn(); }
});

refresh();
</script>
</body></html>"""


if __name__ == "__main__":
    # Host the UI in a background thread AND run the normal interactive shell on the main thread,
    # both sharing repl.WORLD on the SAME session id. So a command typed here applies in the UI and
    # vice versa (repl.dispatch is lock-serialized, so the two threads can't clobber the world).
    # The live feeds differ (the terminal redraws its own prompt; the UI re-renders on each /cmd) —
    # that's fine; state stays in sync. Readline stays on the main thread so TAB/history still work.
    import threading
    PORT = _boot_session(sys.argv[1] if len(sys.argv) > 1 else None)  # load your world (or 'new'), pick the port
    server = _Server(("localhost", PORT), Handler)
    label = f" '{repl.SESSION_NAME}'" if repl.SESSION_NAME else ""
    print(f"TTRPG UI{label} -> http://localhost:{PORT}   ({len(repl.WORLD)} characters loaded)")
    if not sys.stdin.isatty():
        # Launched in the background (e.g. the `ttrpg` nohup launcher) — no terminal to read from,
        # so just host the UI forever like before. (repl.run() would hit EOF and exit instantly here.)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped.")
    else:
        # Real terminal: host in the background AND run the live shell here, both on the same session.
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print("This terminal is ALSO a live shell on the same session — type commands here or in the UI.\n")
        try:
            repl.run()  # the standard shell loop; shares the world the UI is serving
        finally:
            server.shutdown()
        print("stopped.")

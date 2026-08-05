"""serenedash.serve — the dashboard as a live page, over server-sent events.

## Why SSE and not WebSockets

A dashboard is one-way. The server produces a frame every tick and the browser draws it; nothing
the page does needs a socket back. SSE is that shape exactly: an ordinary HTTP response that never
ends, `text/event-stream`, one `data:` line per message. It costs about thirty lines here and no
dependency, where a WebSocket needs the upgrade handshake, the accept-key hash, and frame masking
and fragmentation on both sides for the same result.

It also reconnects on its own. `EventSource` retries a dropped stream without any code, which
matters more than it sounds: the server this watches is one you restart, and a dashboard that
quietly stops updating is worse than one that says it is disconnected.

The one thing SSE cannot do is client-to-server, and the page needs exactly one of those - which
view to draw. That is in the stream's own URL: `/stream?view=logs`. It used to be a separate GET
that set one variable shared by every browser, and that variable was the bug. The page discards any
frame that is not for the view it is showing, so a tab whose choice the server had forgotten - after
a restart, or on a fresh load of a `?view=` link, or because another tab had just switched - threw
every frame away and sat empty forever. Per subscriber instead of global: a reconnect carries the
view with it, two tabs can watch different panels, and there is no state to get out of step.

## What it serves

The same frame functions the terminal draws, exported through `export.py`. Three consumers of one
renderer now - terminal, `--format html`, and this - and none of them can say something the others
do not.
"""
import http.server
import json
import queue
import re
import socketserver
import threading
import time
import urllib.parse

from . import export
from .fmt import strip

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>serenedash</title><style>
:root{color-scheme:dark light}
body{margin:0;padding:1.2rem;background:#1a1d23;color:#e6eaf2;
 font:14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}
#s{font-size:12px;color:#9aa3af;margin:0 0 .5rem;display:flex;gap:.6rem;align-items:center}
#s b{color:#f5d08a;font-weight:600}
#s.off{color:#ff8b96}
#s.wait b{color:#7cc4ff}
#q{display:none;margin-left:auto;background:#101318;border:1px solid #2b303b;border-radius:4px;
 color:#e6eaf2;font:12px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;padding:.25rem .5rem;
 width:14rem}
#q:focus{outline:none;border-color:#f5d08a}
#s.find #q{display:block}
#f{background:#101318;border:1px solid #2b303b;border-radius:6px;padding:.6rem;overflow-x:auto;
 transition:opacity .12s}
#f.wait{opacity:.35}
/* The SVG carries width/height so a standalone .svg opens at a sane size, but on the page the
   viewBox should do the work: scale to the container rather than sit at 1229px with the rest of a
   wide window empty beside it. Aspect ratio is preserved by the viewBox. */
#f svg{display:block;width:100%;height:auto}
/* The key bar at the foot of every frame IS the navigation. A second row of buttons above it said
   the same thing twice, and the selected one changed weight and reflowed the row under the pointer.
   These are invisible hit areas over the bar the renderer already drew, so the only affordance is
   the bar lighting up under the pointer - nothing moves. */
.hit{fill:#e6eaf2;opacity:0;cursor:pointer;rx:2}
.hit:hover{opacity:.14}
@media(prefers-color-scheme:light){body{background:#fbfbfd;color:#2b303b}}
</style></head><body>
<div id=s><span id=lbl>connecting</span>
<input id=q placeholder="filter logs  (/)" spellcheck=false></div>
<div id=f></div>
<script>
const views = __VIEWS__, keys = __KEYS__;
let view = new URLSearchParams(location.search).get('view') || 'main';
const fr = document.getElementById('f'), st = document.getElementById('s'),
      box = document.getElementById('q'), lbl = document.getElementById('lbl');
let needle = new URLSearchParams(location.search).get('q') || '';
// The search box lives outside the frame, because the frame is an image of a terminal and you
// cannot type into an image. Only the views that filter get one.
const FILTERS = ['logs'];
// Only the label is rewritten. Rebuilding the whole bar would replace the input element every
// tick, which drops focus and the half-typed word with it.
function say(txt, cls){ lbl.innerHTML = txt;
  st.className = (cls || '') + (FILTERS.includes(view) ? ' find' : ''); }
// The stream carries the view in its own URL, so EventSource's automatic reconnect asks for the
// right panel with no code at all - which is the whole point of doing it this way. Restarting the
// dashboard used to leave the tab reconnected to a server that had forgotten which view it wanted,
// and every frame it then sent was discarded by the check below.
let es = null;
function connect(){
  if (es) es.close();
  es = new EventSource('/stream?view=' + encodeURIComponent(view) +
                       '&q=' + encodeURIComponent(needle));
  es.onmessage = e => { const m = JSON.parse(e.data);
      if (m.view !== view || !m.svg) return;                // a frame for the view we just left
      fr.innerHTML = m.svg; fr.classList.remove('wait');
      overlay(m.hits || []);
      say('<b>' + m.view + '</b> ' + m.at + ' &middot; click a key below, or press it'); };
  es.onerror = () => say('disconnected - retrying', 'off');
}
connect();

// Hit areas come from the server, computed from the bar it actually rendered, so they cannot drift
// from the text under them the way hand-placed coordinates would.
function overlay(hits){
  const svg = fr.querySelector('svg'); if (!svg) return;
  const NS = 'http://www.w3.org/2000/svg';
  hits.forEach(h => { const r = document.createElementNS(NS, 'rect');
    r.setAttribute('x', h.x); r.setAttribute('y', h.y);
    r.setAttribute('width', h.w); r.setAttribute('height', h.h);
    r.setAttribute('class', 'hit'); r.setAttribute('rx', '2');
    const t = document.createElementNS(NS, 'title'); t.textContent = 'key: ' + h.key;
    r.appendChild(t);
    r.onclick = () => go(h.view);
    svg.appendChild(r); });
}

function url(){ return '?view=' + view + (needle ? '&q=' + encodeURIComponent(needle) : ''); }

// Typing is not a tick. Reconnecting on every keystroke would open and drop a stream per character,
// so the wait is short enough to feel live and long enough to be one connection per word.
let typing = null;
box.oninput = () => { clearTimeout(typing); typing = setTimeout(() => {
  needle = box.value.trim(); history.replaceState(0,'',url()); connect(); }, 250); };
box.onkeydown = e => { e.stopPropagation();          // the view keys must not fire while typing
  if (e.key === 'Escape'){ box.value = ''; box.blur(); needle = '';
                           history.replaceState(0,'',url()); connect(); } };

function go(v){ if (!views.includes(v) || v === view) return;
  view = v; needle = ''; box.value = '';
  history.replaceState(0,'',url());
  // Say so immediately on switch. Most views re-render from data already in memory and land in
  // milliseconds, but `activity` re-fetches whole statements and `doctor` re-runs its checks, and
  // a page that looks frozen for a second is indistinguishable from one that has broken.
  fr.classList.add('wait'); say('loading <b>' + v + '</b>', 'wait');
  connect(); }      // the new stream's first frame IS the switch - nothing to race it, nothing
                    // for the server to remember, and the URL now describes what this tab shows
box.value = needle;
document.addEventListener('keydown', e => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;      // leave the browser's own shortcuts alone
  if (e.key === '/' && FILTERS.includes(view)){ e.preventDefault(); return box.focus(); }
  if (e.key === 'Escape') return go('main');
  const v = keys[e.key.toLowerCase()];
  if (v) { e.preventDefault(); go(v); }
});
</script></body></html>
"""


def hits(lines, keys, cols=None, pad=8):
    """Clickable boxes over the key bar the renderer already drew: [{key, view, x, y, w, h}].

    Found by reading the rendered text rather than by placing coordinates: the bar wraps and
    re-justifies with the width, so anything positioned by hand would be right at one size and
    wrong at the next. Only the last few lines are scanned, because that is where the bar is and a
    panel elsewhere could otherwise contain something that reads like a binding.
    """
    out = []
    first = max(0, len(lines) - 4)
    for row, line in enumerate(lines[first:], start=first):
        text = strip(line)
        for key, view in keys.items():
            m = re.search(rf"(?<![^ ]){re.escape(key)} {re.escape(view)}(?![^ ])", text)
            if not m:
                continue
            out.append({"key": key, "view": view,
                        "x": round(pad + (m.start() - 0.5) * export.CW, 2),
                        "y": round(pad + row * export.LH, 2),
                        "w": round((len(m.group()) + 1) * export.CW, 2),
                        "h": round(export.LH, 2)})
    return out


class Hub:
    """Fan-out to every connected browser. One bounded queue each, and each one names its own view.

    Bounded on purpose: a browser that stops reading must not grow a queue until the dashboard runs
    out of memory. Its frames are dropped instead, and it catches up on the next tick - which for a
    dashboard is the correct loss, since only the newest frame was ever interesting.

    The view is a property of the subscriber, not of the server. See the module docstring: as
    server state it left reconnecting tabs discarding every frame.
    """

    def __init__(self):
        self.subs, self.lock, self.latest = {}, threading.Lock(), {}

    def views(self):
        """The distinct (view, needle) pairs someone is watching."""
        with self.lock:
            return set(self.subs.values())

    def publish_tick(self, render):
        """Render once per view someone is actually watching, and send each to its watchers.

        Nothing is rendered when nobody is connected, and a view nobody has open costs nothing -
        the old code rendered whatever the single global variable last said, whether or not any
        browser wanted it.
        """
        for key in self.views():
            view, needle = key
            try:
                payload = render(view, needle)
            except Exception:                                    # noqa: BLE001, PERF203
                continue        # one broken panel must not stop the others being published
            self.latest[key] = payload
            with self.lock:
                for q, k in list(self.subs.items()):
                    if k == key:
                        try:
                            q.put_nowait(payload)
                        except queue.Full:
                            pass

    def subscribe(self, view, render=None, needle=""):
        """A queue already carrying a frame, so a new tab draws now rather than after one tick.

        The filter travels with the view for the same reason the view does: a page that types into
        a search box and then reconnects should not come back to an unfiltered panel.
        """
        q = queue.Queue(maxsize=4)
        key = (view, needle)
        with self.lock:
            self.subs[q] = key
        payload = None
        if render:
            try:
                payload = render(view, needle)
                self.latest[key] = payload
            except Exception:                                    # noqa: BLE001
                payload = None
        payload = payload or self.latest.get(key)
        if payload:
            q.put_nowait(payload)
        # Nothing is sent when there is nothing to send. A tab that connects before the first tick
        # used to get an empty frame, which blanked whatever it was showing and left it looking
        # broken rather than looking like it was still connecting.
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.subs.pop(q, None)


def handler_for(hub, views, state, keys):
    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass                          # the dashboard owns stdout; an access log would corrupt it

        def _send(self, code, body, ctype="text/html; charset=utf-8", **extra):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in extra.items():
                self.send_header(k.replace("_", "-"), v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/":
                page = (PAGE.replace("__VIEWS__", json.dumps(views))
                            .replace("__KEYS__", json.dumps(keys)))
                return self._send(200, page.encode())
            if path != "/stream":
                return self._send(404, b"no", "text/plain")
            want = self.path.partition("view=")[2].split("&")[0] or "main"
            if want not in views:
                want = "main"
            needle = urllib.parse.unquote_plus(
                self.path.partition("q=")[2].split("&")[0])[:80]

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            # Rendered on subscribe from the data the loop already has, rather than leaving the
            # browser to wait for the next tick. Switching a view is pure formatting - the terminal
            # does it on a keypress without re-querying anything.
            q = hub.subscribe(want, state.get("render"), needle)
            try:
                while True:
                    try:
                        payload = q.get(timeout=20)
                    except queue.Empty:
                        # A comment frame. Proxies and browsers drop a stream that goes quiet, and
                        # a dashboard on an idle server is quiet by definition.
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        continue
                    self.wfile.write(b"data: " + payload + b"\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass                      # the tab was closed; EventSource will reconnect if it comes back
            finally:
                hub.unsubscribe(q)
    return H


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True                 # a lingering stream must not keep the process alive
    allow_reuse_address = True


def start(host, port, views, state, keys):
    """Serve in a background thread. Returns (hub, server) - the caller publishes frames."""
    hub = Hub()
    srv = Server((host, port), handler_for(hub, views, state, keys))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return hub, srv


def frame_payload(view, lines, cols=None, keys=None):
    """One frame for the wire. `cols` pins the grid so every view scales identically on the page."""
    return json.dumps({"view": view, "svg": export.svg(lines, cols=cols),
                       "hits": hits(lines, keys or {}),
                       "at": time.strftime("%H:%M:%S")}).encode()

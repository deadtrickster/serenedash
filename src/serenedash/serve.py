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
view to draw. That is a GET, which is what a GET is for.

## What it serves

The same frame functions the terminal draws, exported through `export.py`. Three consumers of one
renderer now - terminal, `--format html`, and this - and none of them can say something the others
do not.
"""
import http.server
import json
import re
import queue
import socketserver
import threading
import time

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
<div id=s>connecting</div><div id=f></div>
<script>
const views = __VIEWS__, keys = __KEYS__;
let view = new URLSearchParams(location.search).get('view') || 'main';
const fr = document.getElementById('f'), st = document.getElementById('s');
function say(txt, cls){ st.innerHTML = txt; st.className = cls || ''; }
const es = new EventSource('/stream');
es.onmessage = e => { const m = JSON.parse(e.data);
                      if (m.view !== view) return;          // a frame for the view we just left
                      fr.innerHTML = m.svg; fr.classList.remove('wait');
                      overlay(m.hits || []);
                      say('<b>' + m.view + '</b> ' + m.at + ' &middot; click a key below, or press it'); };
es.onerror = () => say('disconnected - retrying', 'off');

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

function go(v){ if (!views.includes(v) || v === view) return;
  view = v; history.replaceState(0,'','?view='+v);
  // Say so immediately on switch. Most views re-render from data already in memory and land in
  // milliseconds, but `activity` re-fetches whole statements and `doctor` re-runs its checks, and
  // a page that looks frozen for a second is indistinguishable from one that has broken.
  fr.classList.add('wait'); say('loading <b>' + v + '</b>', 'wait');
  fetch('/view?name=' + v); }
document.addEventListener('keydown', e => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;      // leave the browser's own shortcuts alone
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
    """Fan-out to every connected browser. One bounded queue each.

    Bounded on purpose: a browser that stops reading must not grow a queue until the dashboard runs
    out of memory. Its frames are dropped instead, and it catches up on the next tick - which for a
    dashboard is the correct loss, since only the newest frame was ever interesting.
    """

    def __init__(self):
        self.subs, self.lock, self.latest = set(), threading.Lock(), None

    def publish(self, payload):
        self.latest = payload
        with self.lock:
            for q in list(self.subs):
                try:
                    q.put_nowait(payload)
                except queue.Full:
                    pass

    def subscribe(self):
        q = queue.Queue(maxsize=4)
        with self.lock:
            self.subs.add(q)
        if self.latest:
            q.put_nowait(self.latest)     # draw immediately rather than after one whole interval
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.subs.discard(q)


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
            if path == "/view":
                name = self.path.partition("name=")[2].split("&")[0]
                if name in views:
                    state["view"] = name
                    # Render NOW from the data the loop already has, rather than leaving the browser
                    # to wait for the next tick. Switching a view is pure formatting - the terminal
                    # does it on a keypress without re-querying anything - so making the page wait
                    # up to a whole refresh interval was the dashboard being slower than the data.
                    render = state.get("render")
                    if render:
                        try:
                            hub.publish(render(name))
                        except Exception:                    # noqa: BLE001
                            pass
                return self._send(200, b"ok", "text/plain")
            if path != "/stream":
                return self._send(404, b"no", "text/plain")

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q = hub.subscribe()
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

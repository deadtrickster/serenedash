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
import queue
import socketserver
import threading
import time

from . import export

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>serenedash</title><style>
:root{color-scheme:dark light}
body{margin:0;padding:1.2rem;background:#1a1d23;color:#c8ccd4;
 font:14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}
nav{display:flex;gap:.3rem;flex-wrap:wrap;margin-bottom:.8rem;align-items:center}
a{color:#9aa3af;text-decoration:none;padding:.25rem .6rem;border:1px solid #2b303b;
 border-radius:4px;font-size:13px}
a:hover{color:#e6e9ef;border-color:#4b5263}
a.on{color:#1a1d23;background:#e5c07b;border-color:#e5c07b;font-weight:600}
#s{margin-left:auto;font-size:12px;color:#7d8590}
#s.off{color:#e06c75}
#f{background:#101318;border:1px solid #2b303b;border-radius:6px;padding:.6rem;overflow-x:auto}
#f svg{display:block;max-width:none}
@media(prefers-color-scheme:light){body{background:#fbfbfd;color:#2b303b}}
</style></head><body>
<nav id=n></nav><div id=f>connecting…</div>
<script>
const views = __VIEWS__;
let view = new URLSearchParams(location.search).get('view') || 'main';
const nav = document.getElementById('n'), fr = document.getElementById('f'),
      st = document.getElementById('s') || Object.assign(document.createElement('span'),{id:'s'});
views.forEach(v => {
  const a = document.createElement('a');
  a.textContent = v; a.href = '?view=' + v;
  if (v === view) a.className = 'on';
  a.onclick = e => { e.preventDefault(); view = v; history.replaceState(0,'','?view='+v);
                     [...nav.querySelectorAll('a')].forEach(x=>x.className = x.textContent===v?'on':'');
                     fetch('/view?name=' + v); };
  nav.appendChild(a);
});
nav.appendChild(st); st.textContent = 'connecting';
const es = new EventSource('/stream');
es.onmessage = e => { const m = JSON.parse(e.data); if (m.view === view) fr.innerHTML = m.svg;
                      st.textContent = m.at; st.className = ''; };
es.onerror = () => { st.textContent = 'disconnected - retrying'; st.className = 'off'; };
</script></body></html>
"""


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


def handler_for(hub, views, state):
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
                page = PAGE.replace("__VIEWS__", json.dumps(views))
                return self._send(200, page.encode())
            if path == "/view":
                name = self.path.partition("name=")[2].split("&")[0]
                if name in views:
                    state["view"] = name
                return self._send(204 if False else 200, b"ok", "text/plain")
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


def start(host, port, views, state):
    """Serve in a background thread. Returns (hub, server) - the caller publishes frames."""
    hub = Hub()
    srv = Server((host, port), handler_for(hub, views, state))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return hub, srv


def frame_payload(view, lines):
    return json.dumps({"view": view, "svg": export.svg(lines),
                       "at": time.strftime("%H:%M:%S")}).encode()

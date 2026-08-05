"""A dashboard server with canned data, run as its own process.

A process, not a thread, because half of what these tests check is what a browser does when the
dashboard DIES - and a thread cannot die the way a process does. `srv.shutdown()` stops the accept
loop but leaves every open stream held by its daemon thread, so the tab sees no drop and the test
proves nothing about the case it was written for. `SIGTERM` closes the sockets the way quitting the
real dashboard does.

Everything below the renderer is canned: `serve`, `export` and the frame functions are the real
code and the numbers in them are invented, so the job needs no SereneDB, no container and no perf
capture.
"""
import sys
import time

from serenedash import serve
from serenedash.views import DETAIL, logs_frame, mcp_frame, mcp_nav

from ..test_views import render  # noqa: TID252  - the same frames the other tests assert on

COLS = 168
VIEWS = ["main", *sorted(DETAIL)]
KEYS = {k: v for v, k in DETAIL.items()}

# Enough lines that a filter has something to remove, and distinctive enough to assert on.
LOGS = ([(f"08-03 10:00:{i:02d}", "Storage", "INFO", f"checkpoint {i} written") for i in range(20)]
        + [("08-03 10:01:00", "Search", "ERROR", "index build failed out of disk")])


# Two sessions with a handful of calls each, so j/k has somewhere to go and enter has something to
# open. Canned for the same reason as everything else here: the page is what is under test.
CALLS = [{"t": 1785000000 + i * 60, "tool": t, "ms": ms, "ok": True, "pid": pid,
          "client": cl, "args": ar, "bytes": by, "reply": rp, "summary": sm}
         for i, (t, ms, pid, cl, ar, by, rp, sm) in enumerate([
             ("status", 812.0, 4001, "claude continue", "", 18422,
              '{"findings": [{"what": "orphaned temp files"}]}', "1 findings: orphaned temp files"),
             ("search", 120.0, 4001, "claude continue", "", 3011,
              '{"indexes": {"2000801": {"num_segments": 19}}}', "indexes, note"),
             ("query", 41.0, 4002, "claude oracle-mcp.json", "sql=select 1", 88,
              '{"rows": [[1]]}', "1 rows")])]


def render_view(name, needle="", st=None):
    if name == "mcp":
        n = {"scroll": 0, "sel": 0, "open": None, "call": -1, "popup": False, **(st or {})}
        lines = mcp_frame(CALLS, [], True, COLS, n["scroll"], n["sel"], 44,
                          n["open"], n["call"], n["popup"])
        return serve.frame_payload(name, lines, cols=COLS, keys=KEYS,
                                   sid=(st or {}).get("id", ""), nav=True)
    if name == "logs":
        rows = [r for r in LOGS if needle.lower() in r[3].lower()]
        lines = logs_frame(rows, "canned", None, needle, True, COLS, 0, 44, True)
    else:
        lines = render(COLS, 44)
    return serve.frame_payload(name, lines, cols=COLS, keys=KEYS,
                               sid=(st or {}).get("id", ""))


def web_nav(st, key):
    """The same reducer the terminal drives, which is the whole point of extracting it."""
    if st.get("view") != "mcp":
        return st
    out = mcp_nav(st, key, CALLS, [])
    return {**st, **out} if out else {**st, "view": "main"}


def main(port, interval=0.25):
    state = {"render": render_view, "nav": web_nav}
    hub, _srv = serve.start("127.0.0.1", port, VIEWS, state, KEYS)
    print(f"up on {port}", flush=True)          # the fixture waits for this rather than for a port
    while True:
        time.sleep(interval)
        hub.publish_tick(render_view)


if __name__ == "__main__":
    main(int(sys.argv[1]))

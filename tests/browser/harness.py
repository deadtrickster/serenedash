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
from serenedash import tui
from serenedash.tui import _web_nav, _withbar   # the real wrapper and the real key reducer
from serenedash.views import (DETAIL, activity_frame, key_to_view, logs_frame, mcp_frame,
                              mcp_nav)

from ..test_views import render  # noqa: TID252  - the same frames the other tests assert on

COLS = 168
VIEWS = ["main", *sorted(DETAIL)]
KEYS = key_to_view()                      # the map, exactly as the real server sends it

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


FINDINGS = [{"kind": "memory", "what": "process memory paged out", "detail": "70.9G in swap."},
            {"kind": "storage", "what": "orphaned temp files", "detail": "24 files, 72.6G."}]

# Two statements, one of them the shape that started all this: multi-term BM25, running far too
# long. The 46-hour one has to sort FIRST, because the cursor is an index into that order and `e`
# plans whatever index it lands on.
QUERIES = [("idle", "SELECT 1", 8, 2, 900, "5001", "172.20.0.7", "pool"),
           ("active", "SELECT id FROM m_idx WHERE body @@ 'cat red alpha' ORDER BY BM25(...)",
            66, 165862, 257242, "5002", "172.20.0.7", "ragflow")]
SAMPLE = {"states": {"active": 1, "idle": 1}, "queries": QUERIES}

# What the server would answer. Canned for the same reason as everything else here - there is no
# SereneDB behind this - but it goes through the REAL reducer, so the toggle, the clearing and the
# rendering are all under test.
PLAN = {"plan": ["╭─ TOP_N ──────────╮", "│ Top: 10          │",
                 "╭─ IRESEARCH_SCAN ─╮", "│ Index: m_idx     │",
                 "│ Score: bm25(k1=1.2, b=0.75)"], "chars": 66}

tui.full_queries = lambda _cfg: QUERIES
tui.explain = lambda _cfg, sql: {**PLAN, "chars": len(sql)}


def render_view(name, needle="", st=None):
    """One frame, wrapped the way the server wraps it - `_withbar` is the code under test as much
    as the page is, since the key hints and the summary rule both live there now."""
    n = {"scroll": 0, "sel": 0, "open": None, "call": -1, "popup": False, **(st or {})}
    if name == "mcp":
        lines = mcp_frame(CALLS, [], True, COLS, n["scroll"], n["sel"], 44,
                          n["open"], n["call"], n["popup"])
    elif name == "activity":
        lines = activity_frame(SAMPLE, True, COLS, n["scroll"], full=QUERIES, sel=n["sel"],
                               open_=n["open"], height=44, plan=n.get("plan"))
    elif name == "logs":
        rows = [r for r in LOGS if needle.lower() in r[3].lower()]
        lines = logs_frame(rows, "canned", None, needle, True, COLS, 0, 44, True)
    else:
        lines = render(COLS, 44)
    body, _off = _withbar(lines, COLS, FINDINGS, name, n)
    return serve.frame_payload(name, body, cols=COLS, keys=KEYS,
                               sid=(st or {}).get("id", ""), nav=name in ("mcp", "activity"))


def web_nav(st, key):
    """The same reducer the terminal drives, which is the whole point of extracting it."""
    if st.get("view") == "activity":
        # The real one, not a stand-in: `e` is handled inside it, and so is dropping the plan when
        # the cursor moves off the statement it belongs to. Those are the two things worth testing.
        return _web_nav({**st, "_n": len(QUERIES)}, key, "", {})
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

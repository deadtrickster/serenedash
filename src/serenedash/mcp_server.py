#!/usr/bin/env python3
"""serenedash-mcp — the dashboard's collectors, as MCP tools.

    serenedash-mcp                 read-only
    serenedash-mcp --allow-write   also expose set_setting (SET GLOBAL)

Everything here delegates to serenedash.py. The TUI renders those collectors for a human; this
renders them as JSON for an agent, which is the only difference. Keeping one implementation means a
fix to the thread accounting or the orphaned-temp rule lands in both at once — and the panels have
been wrong in exactly those places often enough that two copies would drift within a week.

## Why the numbers are shaped the way they are

Each tool returns what was measured plus the denominator it was measured against, because most of
the mistakes this dashboard has made were unit errors rather than collection errors: a thread
percentage that turned out to be a share of the busiest thread rather than of a core, storage shares
divided by a total that appeared nowhere, an engine split computed over the six symbols that fit on
screen. An agent reading `{"pct": 94.9}` cannot tell which of those it is holding, so every rate
here ships next to its base.

## Sampling

`threads` and `cpu` are deltas. A single /proc read cannot produce them, so those tools take two
samples `window` seconds apart inside the call rather than caching state between calls: an agent's
calls arrive at unpredictable intervals, and a delta over "however long since you last asked" is a
number with no defined meaning.

Requires the `mcp` package (see requirements.txt). serenedash.py itself stays stdlib-only.
"""
import argparse
import time

from mcp.server import MCPServer

from . import config as _config
from . import anomaly, db, history, perf, system
from . import snapshot as snap
from .hazards import HAZARDS

# The same four layers the dashboard resolves, from the same loader — flag, environment, config
# file, default. An MCP server is launched by a client with an environment it did not choose, so a
# config file is often the only layer it has; reading env vars directly (as this did) meant the
# global config was invisible to exactly the caller most likely to depend on it.
CFG, _PROV = _config.load_config()
CONTAINER, PORT, PASSWORD = CFG["container"], CFG["port"], CFG["password"]
DATA, PERF = CFG["data"], CFG["perf_dir"]

server = MCPServer(
    name="serenedash",
    version="1.0.0",
    instructions="Live state of a SereneDB server: storage, memory, sessions, per-thread CPU, and "
                 "a perf-backed profile. Call status() first — it is one round trip and carries "
                 "the findings the individual tools would each have to be asked for.",
)


def _sample(query_head: int = 400):
    """One psql round trip, or an error dict the tools return verbatim.

    `query_head` is pushed down into the SQL rather than applied on arrival. Trimming here would
    still move the whole statement across — 1.84 MB per call on this deployment — to throw away
    99.9% of it; `left()` in the query means it is never sent.
    """
    s = db.sample(CFG, query_head=query_head)
    if s is None:
        return None, {"error": f"cannot reach {CONTAINER}:{PORT}",
                      "hint": "is the container running, and is PGPASSWORD right?"}
    return s, None


def _pid():
    return system.host_pid(CFG)


def _threads(window: float = 1.0):
    """Two /proc reads, `window` apart. See the module docstring on why this is not cached."""
    pid = _pid()
    if not pid:
        return [], 0.0, None
    _, _, prev, t = system.threads(pid, {}, time.time())
    time.sleep(window)
    rows, total, _, _ = system.threads(pid, prev, t)
    return rows, total, pid


@server.tool()
def status(thread_window: float = 1.0) -> dict:
    """Everything the dashboard shows, in one call, with the findings called out.

    Start here. `findings` is the part worth reading first: each entry is a condition that was
    measured rather than inferred, with the numbers behind it, so it can be checked rather than
    trusted. An empty list means nothing tripped, not that nothing was looked at. Findings whose
    `what` starts with `anomaly:` come from the recorded history rather than from this instant —
    they are judged against the series' own past, so they catch drift that no threshold can.

    Args:
        thread_window: seconds between the two /proc samples the CPU figures are derived from.
            Longer is steadier and slower; below ~0.5s quantisation starts to show.
    """
    return snap.collect(CFG, thread_window=thread_window, hist=history.load(PERF))


@server.tool()
def storage() -> dict:
    """Disk: database and WAL, the store's directories, and the live/orphaned spill split.

    `spill_live_bytes` and `spill_orphaned_bytes` are reported separately rather than summed. A
    temp file older than the process cannot belong to a query running in it, and counting the two
    together is what makes a server look like it is spilling heavily when it is only holding the
    wreckage of one that was killed.
    """
    s, err = _sample()
    if err:
        return err
    return snap.storage(s, system.slow(CFG, DATA), system.hostinfo(_pid(), CONTAINER))


@server.tool()
def memory() -> dict:
    """RAM: duckdb_memory() by pool, against RSS, swap and memory_limit.

    The pools are what the store believes it holds; resident is what is actually in RAM. Read them
    against each other — the gap is usually swap, and a store can sit comfortably under
    memory_limit while most of it is paged out.
    """
    s, err = _sample()
    if err:
        return err
    return snap.memory(s, system.hostinfo(_pid(), CONTAINER))


@server.tool()
def activity(max_query_chars: int = 2000) -> dict:
    """Sessions and their current statements, excluding this connection.

    `nothing_running: true` alongside busy threads is a finding rather than an absence: it means
    work with no session behind it.

    Args:
        max_query_chars: statement text is cut at this length. `query_chars` on each row is always
            the full length, so a truncated statement is never mistaken for a short one. Raise it
            deliberately — generated statements here run to ~185 KB each.
    """
    s, err = _sample(query_head=max_query_chars)
    if err:
        return err
    return snap.activity(s, max_query_chars)


@server.tool()
def threads(window: float = 1.0) -> dict:
    """Per-thread CPU as a share of one core, with what each thread was running.

    Threads, not the process: one pinned core out of 24 reads as 4% at process level and as 100%
    here, and that difference is the whole diagnosis. `symbol` comes from the newest perf capture
    matched by tid, so it lags the percentage beside it.

    Args:
        window: seconds between the two samples the deltas come from.
    """
    rows, tcpu, pid = _threads(window)
    if not pid:
        return {"error": f"cannot resolve the host pid for {CONTAINER}"}
    _, _, by_tid = perf.perf_window(PERF)
    return snap.threads(rows, tcpu, by_tid, system.hostinfo(pid, CONTAINER), window)


@server.tool()
def profile() -> dict:
    """Sampled CPU by symbol and engine, from the newest perf captures.

    Empty when nothing has been recorded — the dashboard cannot record for itself (perf_event_paranoid
    blocks attaching to a container process without root), so this reads what perf-snap.sh wrote.
    """
    newest, tops, _ = perf.perf_window(PERF)
    out = snap.profile(newest, tops)
    if not tops:
        out["hint"] = "no captures. run: sudo ./perf-snap.sh --container " + CONTAINER
    return out


@server.tool()
def callgraph(limit: int = 40) -> dict:
    """Caller-oriented call graph from the newest usable capture.

    A flat symbol list says what is hot; only this says what led into it — the distinction that
    separated a spinning COPY feeder from a spinning recv loop when both showed the same leaf.

    Args:
        limit: maximum lines of the graph to return.
    """
    name, lines = perf.callstacks(PERF, limit=limit)
    return {"capture": name, "lines": lines,
            **({} if lines else {"hint": "no capture with call-graph data yet"})}


@server.tool()
def host() -> dict:
    """The machine: cores, load, RAM, swap, uptime, and memory_limit as a share of RAM.

    The context every other number is read against — a per-core thread percentage means nothing
    without the core count, and a memory_limit means nothing without the RAM it was drawn from.
    """
    s, err = _sample()
    if err:
        return err
    return snap.hostinfo(system.hostinfo(_pid(), CONTAINER), s)


@server.tool()
def config(name: str = "") -> dict:
    """Effective settings. Without a name, the ones with measured consequences and their verdicts.

    Args:
        name: a single setting to look up, or empty for the watched set.
    """
    s, err = _sample()
    if err:
        return err
    if name:
        # Bound, not interpolated. `name` arrives from the caller, and this was an f-string.
        rows = db.query(CFG,
                        ["select name, value, coalesce(description,''), input_type, scope "
                         "from duckdb_settings() where name = %s"],
                        params=[(name,)])
        r = (rows[0] or [[]])[0] if rows else []
        if len(r) < 2:
            return {"error": f"no such setting: {name}"}
        out = {"name": r[0], "value": r[1], "description": r[2] if len(r) > 2 else "",
               "scope": r[4] if len(r) > 4 else None}
        if r[0] in HAZARDS:
            why, pred = HAZARDS[r[0]]
            out["why_it_matters"] = why
            out["verdict"] = (pred(r[1], s) if pred else None) or "nothing wrong on this server"
        return out
    out = {}
    for n in sorted(HAZARDS):
        why, pred = HAZARDS[n]
        val = str(s["settings"].get(n, "?"))
        out[n] = {"value": val, "why_it_matters": why,
                  "verdict": (pred(val, s) if pred else None) or "nothing wrong on this server"}
    return out


@server.tool()
def query(sql: str, max_rows: int = 200, max_chars: int = 20000) -> dict:
    """Run one read-only statement against the server and get the rows back.

    The panels answer the questions they were built for. This is for the ones they were not: joining
    duckdb_settings() against what a session is actually doing, counting something in a system view,
    checking whether the thing a finding claims is really there. Without it the honest move on a
    question the panels do not cover is to write the SQL out and ask someone else to run it, which
    is a diagnosis that stops halfway.

    Refused unless the statement's leading keyword is one that cannot write, and the connection is
    opened read-only regardless, so the server would reject a write that got past the check. Results
    are capped by rows and then by characters — this returns into a context window, and one wide
    system view can be megabytes.

    Args:
        sql: a single statement. No semicolon-separated batches.
        max_rows: rows to return at most.
        max_chars: total characters of row data to return at most, applied after max_rows.
    """
    return db.read_query(CFG, sql, max_rows=max_rows, max_chars=max_chars)


@server.tool()
def anomalies() -> dict:
    """What has moved, judged against the recorded history rather than a threshold.

    Reads the series the dashboard writes as it runs. With no dashboard running there is no history
    to judge against and this says so — it does not fall back to a single instant, because a
    baseline of one sample would report every value as normal.
    """
    hist = history.load(PERF)
    if not hist:
        return {"available": False,
                "reason": f"no history at {history.path(PERF)}",
                "fix": "run the dashboard (serenedash) - it records one sample per tick, and the "
                       "file outlives it",
                "note": "the threshold findings in status() do not need this; only the drift ones do"}
    n = max((len(v) for v in hist.values()), default=0)
    if n < anomaly.MIN_SPIKE:
        # An empty list from a window this short would read as "nothing is wrong". It is not the
        # same claim, and the difference is exactly the one this tool exists to keep straight.
        return {"available": False, "samples": n,
                "reason": f"only {n} samples recorded; no rule may speak below "
                          f"{anomaly.MIN_SPIKE}",
                "fix": "leave the dashboard running - one sample per tick, 5s by default",
                "note": "not a report that nothing tripped: there is not enough history to judge"}
    scanned = anomaly.scan(hist)
    return {
        "available": True,
        "samples": n,
        "series": sorted(hist),
        "anomalies": [a.as_finding() for a in scanned],
        "note": "nothing here means nothing tripped over the recorded window, not that nothing was "
                "looked at. Every series is checked for a level shift, a monotonic climb, and a "
                "single-sample excursion, in that order.",
    }


def _install_write_tools():
    """Registered only under --allow-write. SET GLOBAL applies immediately and reverts on restart,
    so the blast radius is one running server — but it is still a write, and a read-only tool
    surface that can quietly mutate the thing it reports on is the wrong default."""

    @server.tool()
    def set_setting(name: str, value: str) -> dict:
        """SET GLOBAL a setting on the live server. Reverts on restart.

        Args:
            name: setting name, validated as a plain identifier.
            value: new value, sent as a quoted literal.
        """
        ok, msg = db.apply_setting(CFG, name, value)
        return {"ok": ok, "message": msg,
                "note": "applies immediately and reverts on restart - put it in serened.conf to keep it"}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--allow-write", action="store_true",
                    help="also expose set_setting, which mutates the running server")
    a = ap.parse_args()
    if a.allow_write:
        _install_write_tools()
    server.run("stdio")


if __name__ == "__main__":
    main()

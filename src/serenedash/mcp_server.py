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
import functools
import hashlib
import inspect
import os
import time

from mcp.server import MCPServer

from . import config as _config
from . import anomaly, db, history, mcplog, perf, system
from . import snapshot as snap
from .hazards import HAZARDS

# The same four layers the dashboard resolves, from the same loader — flag, environment, config
# file, default. An MCP server is launched by a client with an environment it did not choose, so a
# config file is often the only layer it has; reading env vars directly (as this did) meant the
# global config was invisible to exactly the caller most likely to depend on it.
CFG, _PROV = _config.load_config()
CONTAINER, PORT, PASSWORD = CFG["container"], CFG["port"], CFG["password"]
DATA, PERF = CFG["data"], CFG["perf_dir"]


VERSION = "0.1.0"

INSTRUCTIONS_URI = "serenedash://instructions"


def instructions():
    """(text, revision) for instructions.md — what the client puts in front of the model.

    A file rather than a string literal, because it is the one piece of this server read by
    something that cannot ask a follow-up question. It has to be reviewable in a diff like any other
    document, and every rule in it exists because the vocabulary here is misreadable: `orphaned` is
    not spill, a thread percentage is not a share of the machine, and "not enough history to judge"
    is not "nothing is wrong".

    The revision is a hash of the file, stamped into the text itself and returned again by every
    tool. Instructions are injected once, when the client connects, and stay in the model's context
    for the whole session — so upgrading this server mid-session leaves an agent reasoning from
    documentation that no longer describes it, with nothing to notice it by. Comparing the two is
    that missing signal.

    Falls back to one line if the file is missing. An install that lost its package data should give
    an agent less context, not no server.
    """
    try:
        with open(os.path.join(os.path.dirname(__file__), "instructions.md"), "rb") as f:
            raw = f.read()
    except OSError:
        return ("Live state of a SereneDB server. Call status() first. Every rate carries the "
                "denominator it was measured against - do not read one without the other."), "none"
    rev = hashlib.sha256(raw).hexdigest()[:12]
    text = raw.decode("utf-8", "replace")
    return text.replace("{{VERSION}}", VERSION).replace("{{REVISION}}", rev), rev


INSTRUCTIONS, REVISION = instructions()


def _read_brief():
    """The short form, or "" when it is missing - in which case the full text is sent as before.

    A missing brief must degrade to the OLD behaviour rather than to no instructions: a client that
    gets nothing has no way to know it is missing anything.
    """
    try:
        with open(os.path.join(os.path.dirname(__file__), "brief.md")) as f:
            return f.read().replace("{{VERSION}}", VERSION).replace("{{REVISION}}", REVISION)
    except OSError:
        return ""


# Whether this process has already handed over the full guide. One per server process, and a server
# process is one client session, so "first call of the session" and "first call of the process" are
# the same thing.
_first_call = True


def _stamp():
    """What every tool result carries so an agent can tell its context from the running server."""
    return {"version": VERSION, "instructions_revision": REVISION,
            "instructions_uri": INSTRUCTIONS_URI}


# Recording is off until a client is actually on the other end of the pipe. `stamped` is an
# ordinary decorator, so anything that imports this module and calls a wrapped function - a test,
# another tool, an interactive session - would otherwise land in the log as a call that no agent
# made. It did: a test calling `stamped(lambda: [1, 2])` put a `<lambda>` row in the real one.
_SERVING = False


def stamped(fn):
    """Add `server` to every dict a tool returns, and record the call.

    On every tool rather than only on `status`, because an agent that goes straight to `memory()`
    is exactly the one whose context is oldest. `functools.wraps` keeps the signature and docstring
    the SDK builds the tool schema from.

    Recording rides along here because this is the one place every tool already passes through - a
    second decorator would eventually be left off a new tool, and a call log with a hole in it is
    worse than none, since the hole is invisible. See `mcplog`: an MCP server has no window and no
    log of its own, so without this there is no way to see what an agent asked or what it was told.
    """
    # Computed once, not per call. Arguments left at their default are not something the agent
    # asked for, and `max_rows=200, max_chars=20000` on every query() pushed the SQL - the one part
    # worth reading - off the end of the line.
    _defaults = {k: p.default for k, p in inspect.signature(fn).parameters.items()
                 if p.default is not inspect.Parameter.empty}

    @functools.wraps(fn)
    def wrapped(*a, **kw):
        t0 = time.perf_counter()
        named = dict(zip(fn.__code__.co_varnames[:fn.__code__.co_argcount], a, strict=False))
        named.update(kw)
        named = {k: v for k, v in named.items() if k not in _defaults or _defaults[k] != v}
        try:
            out = fn(*a, **kw)
        except Exception as e:                                   # noqa: BLE001
            # Recorded and re-raised. A tool that failed is exactly the call worth seeing, and the
            # agent must still get its error rather than a dashboard's idea of one.
            if _SERVING:
                mcplog.record(PERF, fn.__name__, named, (time.perf_counter() - t0) * 1000,
                              "", ok=False, err=e)
            raise
        if isinstance(out, dict):
            mine = "server" not in out          # a tool that reports its own stamp keeps it whole
            out.setdefault("server", _stamp())
            global _first_call                                   # noqa: PLW0603
            if mine and _first_call and MODE == "lazy":
                # The guide, once, on the call that made it relevant. Under `server` rather than at
                # the top level so it cannot be mistaken for part of the measurement.
                _first_call = False
                out["server"]["instructions"] = INSTRUCTIONS
                out["server"]["instructions_note"] = (
                    "The full guide for reading this server, sent with the first tool result of "
                    "the session rather than at connect - it is 16K tokens and most sessions never "
                    "call these tools. Read it before drawing conclusions; it is also at "
                    + INSTRUCTIONS_URI + " and will not be sent again.")
        if _SERVING:
            mcplog.record(PERF, fn.__name__, named, (time.perf_counter() - t0) * 1000, out)
        return out
    return wrapped


# What the client injects at connect, and what it gets on first use. See the module note on
# `_first_call`: the default is to send the short brief now and the full guide with the first tool
# result, because this server is enabled per project and most sessions never ask it anything.
MODE = os.environ.get("SERENEDASH_INSTRUCTIONS", "lazy").strip().lower()

BRIEF = (_read_brief() or INSTRUCTIONS)

server = MCPServer(
    name="serenedash",
    version=VERSION,
    instructions=INSTRUCTIONS if MODE == "full" else BRIEF,
)


@server.resource(INSTRUCTIONS_URI, name="serenedash instructions", mime_type="text/markdown",
                 description="How to read this server's numbers. The same text injected at "
                             "connect time - re-read it when a tool result reports a revision "
                             "that does not match the copy in your context.")
def _instructions_resource() -> str:
    """Re-readable, so a stale context has a remedy that does not need a reconnect."""
    return INSTRUCTIONS


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
@stamped
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
@stamped
def storage() -> dict:
    """Disk: database and WAL, the store's directories, and the live/orphaned spill split.

    `spill_live_bytes` and `spill_orphaned_bytes` are reported separately rather than summed. A
    temp file older than the process cannot belong to a query running in it, and counting the two
    together is what makes a server look like it is spilling heavily when it is only holding the
    wreckage of one that was killed. `server_temp_files_held` is the same question asked of the
    server instead of the filesystem - 0 files open against a full temp directory is the orphan
    claim proving itself from the inside, which is what to quote before recommending a deletion.
    """
    s, err = _sample()
    if err:
        return err
    return snap.storage(s, system.slow(CFG, DATA), system.hostinfo(_pid(), CONTAINER),
                        db.temp_files_held(CFG))


@server.tool()
@stamped
def memory() -> dict:
    """RAM: duckdb_memory() by pool, against RSS, swap and memory_limit.

    The pools are what the store believes it holds; resident is what is actually in RAM. Read them
    against each other — the gap is usually swap, and a store can sit comfortably under
    memory_limit while most of it is paged out. `spilled_bytes_by_pool` is which pool went to disk:
    the storage panel can only say the temp directory has bytes in it, and that is as often the
    wreckage of a killed run as it is a live operator.
    """
    s, err = _sample()
    if err:
        return err
    return snap.memory(s, system.hostinfo(_pid(), CONTAINER))


@server.tool()
@stamped
def activity(max_query_chars: int = 2000, history: bool = True) -> dict:
    """Sessions and their current statements, excluding this connection, with per-query progress.

    `nothing_running: true` alongside busy threads is a finding rather than an absence: it means
    work with no session behind it.

    `progress` is `sdb_progress` beside the same sessions. `pg_stat_activity` says a statement is
    running; this says how far in and which phase, which is the difference between telling someone
    to wait and telling them to kill it. Unlike the session list it cannot exclude the connection
    that asked, so one row is always this collector.

    `recorded` is the other half, and it is the only one there is: pg_stat_activity is present
    tense, this server has no pg_stat_statements, and `log_query_path` and profiling are both off by
    default — so a statement that has ENDED leaves no trace anywhere on the server. The dashboard
    samples the live view every tick and keeps what it saw, which costs the server nothing and is
    how a 42-hour statement that has since been terminated still has a duration. It is sampled, and
    the payload says so: a statement shorter than one tick was never seen, and every duration is a
    lower bound rather than a measurement.

    Args:
        max_query_chars: statement text is cut at this length. `query_chars` on each row is always
            the full length, so a truncated statement is never mistaken for a short one. Raise it
            deliberately — generated statements here run to ~185 KB each.
        history: include `recorded`, the dashboard's sampled record of what has run. Turn it off
            when you only want what is running right now.
    """
    s, err = _sample(query_head=max_query_chars)
    if err:
        return err
    out = snap.activity(s, max_query_chars, db.progress(CFG))
    if history:
        out["recorded"] = snap.recorded(PERF)
    return out


@server.tool()
@stamped
def search() -> dict:
    """The inverted indexes: documents, segments, size, and whether maintenance is keeping up.

    SereneDB is a search engine and every other tool here measures the process or the store around
    it. This is the engine's own accounting, from `sdb_metrics`: per index, `num_live_docs` against
    `num_docs` (the difference is deleted-but-not-reclaimed), `num_buffered_docs` (written, not yet
    published - why a just-inserted row does not come back), `num_segments`, `index_size_bytes`, the
    `num_failed_*` counters and the three `avg_*_time_ms`. Server-wide: the refresh, compaction and
    cleanup active/pending pairs.

    What is misread without it: a periodic CPU spike on a write-heavy server has two candidate
    causes - a refresh coupled to the autocheckpoint, which is single-threaded with a synchronous
    segment flush, and compaction. They look identical in `threads` and `profile`. `refresh_active`,
    `refresh_pending`, `compaction_active` and `avg_consolidation_time_ms` tell them apart, and
    without them the honest answer is "one of these two". A search index larger than the table it
    indexes reads as a defect until `index_size_bytes` is set beside the feature flags that
    explain it.

    Unavailable is reported as unavailable: no index and no connection are not an empty result.
    """
    return snap.search(db.search(CFG))


@server.tool()
@stamped
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
@stamped
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
@stamped
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
@stamped
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
@stamped
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
@stamped
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
@stamped
def explain(sql: str = "", pid: str = "") -> dict:
    """The plan for a statement — either one you pass, or the one a session is running right now.

    `query` could already run an EXPLAIN by hand, but not on a statement that is already executing:
    that text lives in pg_stat_activity, it is routinely tens of kilobytes, and getting it into a
    tool call means fetching it, quoting it and sending it back. Give this a `pid` from `activity`
    instead and it plans what that session is doing.

    EXPLAIN does not execute — there is no ANALYZE here — so this is safe to point at a statement
    that is hung, which is the case it exists for. On the 68 KB hybrid search that started all this
    it returned in 87 ms.

    Args:
        sql: a statement to plan. Ignored when `pid` is given.
        pid: plan whatever this session is currently running instead.
    """
    if pid:
        rows = [r for r in db.full_queries(CFG) if str(r[5]) == str(pid).strip()]
        if not rows:
            return {"error": f"no session with pid {pid}",
                    "fix": "call activity() for the pids that exist right now"}
        if not (rows[0][1] or "").strip():
            return {"error": f"pid {pid} is not running a statement", "state": rows[0][0]}
        out = db.explain(CFG, rows[0][1])
        out["pid"], out["state"] = str(pid).strip(), rows[0][0]
        return out
    if not sql.strip():
        return {"error": "give me either sql or a pid"}
    return db.explain(CFG, sql)


@server.tool()
@stamped
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
    @stamped
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
    global _SERVING                                              # noqa: PLW0603
    _SERVING = True          # from here, every call has a real client behind it
    server.run("stdio")


if __name__ == "__main__":
    main()

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
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import serenedash as dash                                       # noqa: E402
from mcp.server import MCPServer                                # noqa: E402

CONTAINER = os.environ.get("SERENEDB_CONTAINER", "oracle-serenedb")
PORT = os.environ.get("SERENEDB_PORT", "7890")
PASSWORD = os.environ.get("PGPASSWORD", "oracle-sdb")
DATA = os.environ.get("SERENEDB_DATA", "/var/lib/serenedb")
PERF = os.environ.get("SERENEDASH_PERF_DIR", os.path.expanduser("~/.cache/serenedash/perf"))

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
    s = dash.sample(CONTAINER, PORT, PASSWORD, query_head=query_head)
    if s is None:
        return None, {"error": f"cannot reach {CONTAINER}:{PORT}",
                      "hint": "is the container running, and is PGPASSWORD right?"}
    return s, None


def _pid():
    return dash.host_pid(CONTAINER)


def _threads(window: float = 1.0):
    """Two /proc reads, `window` apart. See the module docstring on why this is not cached."""
    pid = _pid()
    if not pid:
        return [], 0.0, None
    _, _, prev, t = dash.threads(pid, {}, time.time())
    time.sleep(window)
    rows, total, _, _ = dash.threads(pid, prev, t)
    return rows, total, pid


def _temp_split(sz, host):
    """Live spill vs files older than the process. The split, not the sum — see storage()."""
    started = time.time() - host["uptime"] if host.get("uptime") else None
    orph = [f for f in (sz.get("temp_files") or []) if started and f[0] < started]
    orph_bytes = sum(f[1] for f in orph)
    return orph, orph_bytes, max(0, (sz.get("temp") or 0) - orph_bytes)


@server.tool()
def status(thread_window: float = 1.0) -> dict:
    """Everything the dashboard shows, in one call, with the findings called out.

    Start here. `findings` is the part worth reading first: each entry is a condition that was
    measured rather than inferred, with the numbers behind it, so it can be checked rather than
    trusted. An empty list means nothing tripped, not that nothing was looked at.

    Args:
        thread_window: seconds between the two /proc samples the CPU figures are derived from.
            Longer is steadier and slower; below ~0.5s quantisation starts to show.
    """
    s, err = _sample()
    if err:
        return err
    host = dash.hostinfo(_pid(), CONTAINER)
    sz = dash.slow(CONTAINER, DATA)
    rows, tcpu, pid = _threads(thread_window)
    newest, tops, by_tid = dash.perf_window(PERF)
    orph, orph_bytes, live_spill = _temp_split(sz, host)

    findings = []
    if orph_bytes:
        findings.append({
            "what": "orphaned temp files",
            "detail": f"{len(orph)} files, {dash.human(orph_bytes)}, all older than the running "
                      f"serened. DuckDB deletes temp files only in a destructor and never sweeps "
                      f"at startup, so a killed server leaks them and no later run reclaims them.",
            "bytes_reclaimable": orph_bytes,
            "verify": "nothing holds them open: ls -l /proc/1/fd | grep -c tmp, inside the container",
        })
    if (host.get("swap") or 0) > 0:
        findings.append({
            "what": "process memory paged out",
            "detail": f"{dash.human(host['swap'])} of serened is in swap while duckdb_memory() "
                      f"reports {dash.human(s['mem'])} held. Every touch of that memory is a disk "
                      f"read, and memory_limit does not count it.",
            "swap_bytes": host.get("swap"), "rss_bytes": host.get("rss"),
        })
    ratio = s["wal"] / s["size"] if s["size"] else 0
    if ratio > 1:
        findings.append({
            "what": "checkpoints not completing",
            "detail": f"WAL is {ratio:.1f}x the database. Look for write errors, not for tuning.",
            "wal_bytes": s["wal"], "database_bytes": s["size"],
        })
    lim, ram = s["memlimit"] or 0, host.get("ram_total") or 0
    if ram and lim > ram * 0.75:
        findings.append({
            "what": "memory_limit oversubscribed",
            "detail": f"memory_limit is {lim / ram * 100:.0f}% of the machine's {dash.human(ram)} "
                      f"of RAM, which anything else on the box has to fit around.",
            "memory_limit_bytes": lim, "ram_total_bytes": ram,
        })
    for name in sorted(dash.HAZARDS):
        why, pred = dash.HAZARDS[name]
        warn = pred(str(s["settings"].get(name, "?")), s) if pred else None
        if warn:
            findings.append({"what": f"setting: {name}", "detail": warn,
                             "value": s["settings"].get(name)})

    return {
        "findings": findings,
        "storage": _storage(s, sz, host),
        "memory": _memory(s, host),
        "activity": _activity(s),
        "threads": _threads_out(rows, tcpu, by_tid, host, thread_window),
        "profile": _profile(newest, tops),
        "host": _host(host, s),
        "config": {n: s["settings"].get(n) for n in sorted(dash.HAZARDS)},
    }


def _storage(s, sz, host):
    tot = sz.get("total") or 1
    orph, orph_bytes, live_spill = _temp_split(sz, host)
    return {
        "on_disk_bytes": tot,
        "database_bytes": s["size"],
        "note": "database is the store's own logical size; the directory sizes below are du and "
                "sum to on_disk_bytes. The two are different measures and will not match.",
        "wal_bytes": s["wal"],
        "wal_over_database": round(s["wal"] / s["size"], 4) if s["size"] else None,
        "directories": {
            "columnar_bytes": sz.get("duck"), "search_index_bytes": sz.get("index"),
            "temp_bytes": sz.get("temp"),
        },
        "spill_live_bytes": live_spill,
        "spill_orphaned_bytes": orph_bytes,
        "spill_orphaned_files": len(orph),
        "blocks": dict(zip(("total", "used", "free", "size_bytes"), s["blocks"])),
    }


def _memory(s, host):
    rss, swap = host.get("rss") or 0, host.get("swap") or 0
    return {
        "duckdb_memory_bytes": s["mem"],
        "memory_limit_bytes": s["memlimit"],
        "used_fraction_of_limit": round(s["mem"] / s["memlimit"], 4) if s["memlimit"] else None,
        "resident_bytes": rss,
        "swapped_bytes": swap,
        "peak_resident_bytes": host.get("peak"),
        "note": "duckdb_memory() counts what the store believes it holds; resident is what is in "
                "RAM now. A large gap is usually swap, which memory_limit does not account for.",
        "pools": {tag: v for tag, v in s["memtags"]},
    }


def _activity(s, max_query_chars=400):
    """Sessions, with statement text BOUNDED.

    Statements are unbounded server-supplied text and the caller is a context window. A single
    generated INSERT on this deployment is ~185 KB, so twelve sessions returned 1.66 MB in one
    call — enough to blow the tool-result limit on its own, with every other panel in the response
    adding up to under 14 KB. The head of a statement identifies it; the body is bulk. The full
    length is reported so a truncated one is never mistaken for a short one.
    """
    rows = []
    for st, q, full_len in s["queries"]:
        if "pg_stat_activity" in q:
            continue
        row = {"state": st, "query": (q[:max_query_chars] or None), "query_chars": full_len}
        if full_len > len(row["query"] or ""):
            row["query_truncated"] = True
        rows.append(row)
    active = [r for r in rows if r["state"] == "active"]
    return {
        "sessions_total": sum(s["states"].values()),
        "by_state": s["states"],
        "note": "excludes this connection, which is active by construction. Statement text is cut "
                f"at {max_query_chars} chars; query_chars is always the full length.",
        "nothing_running": not active,
        "nothing_running_means": "a pinned core with no active session is work with no session "
                                 "behind it - an orphaned server-side task",
        "sessions": rows,
    }


def _threads_out(rows, tcpu, by_tid, host, window):
    cores = host.get("cores") or 1
    return {
        "process_cpu_percent": round(tcpu, 1),
        "of_percent": cores * 100,
        "cores": cores,
        "note": f"percentages are shares of ONE core, summed over all threads. {cores * 100}% is "
                f"the machine. Sampled over {window}s.",
        "os_threads": host.get("threads"),
        "threads": [
            {"tid": tid, "name": name, "cpu_percent_of_one_core": round(pct, 1),
             "state": st, "blocked_in_io": st == "D",
             "symbol": (by_tid.get(tid) or [None])[0]}
            for pct, name, st, tid in rows
        ],
    }


def _profile(newest, tops):
    fam = {}
    for sym, pct in tops:
        fam[dash.kernel_of(sym)] = fam.get(dash.kernel_of(sym), 0.0) + pct
    tot = sum(fam.values()) or 1
    return {
        "capture": newest,
        "note": "shares of sampled cycles over the newest captures. Engine shares are computed "
                "over the whole profile, not over the symbols listed here.",
        "engines": {k: round(v / tot * 100, 1) for k, v in
                    sorted(fam.items(), key=lambda kv: -kv[1])},
        "symbols": [{"symbol": dash.re.sub(r"^\[[.k]\]\s*", "", sym),
                     "kernel": sym.startswith("[k]"),
                     "engine": dash.kernel_of(sym), "percent": round(pct, 2)}
                    for sym, pct in tops[:25]],
    }


def _host(host, s):
    ram = host.get("ram_total") or 0
    lim = s["memlimit"] or 0
    return {
        "container": host.get("container"), "pid": host.get("pid"),
        "uptime_seconds": int(host.get("uptime") or 0),
        "cores": host.get("cores"), "load": host.get("load"),
        "ram_total_bytes": ram, "ram_available_bytes": host.get("ram_avail"),
        "swap_total_bytes": host.get("swap_total"),
        "swap_used_bytes": (host.get("swap_total") or 0) - (host.get("swap_free") or 0),
        "memory_limit_fraction_of_ram": round(lim / ram, 4) if ram else None,
    }


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
    return _storage(s, dash.slow(CONTAINER, DATA), dash.hostinfo(_pid(), CONTAINER))


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
    return _memory(s, dash.hostinfo(_pid(), CONTAINER))


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
    return _activity(s, max_query_chars)


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
    _, _, by_tid = dash.perf_window(PERF)
    return _threads_out(rows, tcpu, by_tid, dash.hostinfo(pid, CONTAINER), window)


@server.tool()
def profile() -> dict:
    """Sampled CPU by symbol and engine, from the newest perf captures.

    Empty when nothing has been recorded — the dashboard cannot record for itself (perf_event_paranoid
    blocks attaching to a container process without root), so this reads what perf-snap.sh wrote.
    """
    newest, tops, _ = dash.perf_window(PERF)
    out = _profile(newest, tops)
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
    name, lines = dash.callstacks(PERF, limit=limit)
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
    return _host(dash.hostinfo(_pid(), CONTAINER), s)


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
        rows = dash.psql(CONTAINER, PORT, PASSWORD,
                         ["select name, value, coalesce(description,''), input_type, scope "
                          f"from duckdb_settings() where name = '{name}'"])
        r = (rows[0] or [[]])[0] if rows else []
        if len(r) < 2:
            return {"error": f"no such setting: {name}"}
        out = {"name": r[0], "value": r[1], "description": r[2] if len(r) > 2 else "",
               "scope": r[4] if len(r) > 4 else None}
        if r[0] in dash.HAZARDS:
            why, pred = dash.HAZARDS[r[0]]
            out["why_it_matters"] = why
            out["verdict"] = (pred(r[1], s) if pred else None) or "nothing wrong on this server"
        return out
    out = {}
    for n in sorted(dash.HAZARDS):
        why, pred = dash.HAZARDS[n]
        val = str(s["settings"].get(n, "?"))
        out[n] = {"value": val, "why_it_matters": why,
                  "verdict": (pred(val, s) if pred else None) or "nothing wrong on this server"}
    return out


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
        ok, msg = dash.apply_setting(CONTAINER, PORT, PASSWORD, name, value)
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

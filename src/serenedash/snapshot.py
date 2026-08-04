"""serenedash.snapshot — the whole server state as data, for anything that is not a terminal.

This was inside the MCP server, which made `--format json` impossible to write without either a
second implementation or a dependency on `mcp` for a flag that has nothing to do with it. It is the
same shape the TUI renders and the same collectors underneath; only the output differs.

Every rate carries the base it was measured against. Most of the mistakes this dashboard has made
were unit errors rather than collection errors — a thread percentage that turned out to be a share
of the busiest thread rather than of a core, storage shares divided by a total that appeared
nowhere — and a bare number in JSON is exactly as easy to misread as a bare number on screen.
"""
import re
import time

from . import db, perf, system
from .anomaly import scan
from .fmt import human
from .hazards import HAZARDS, kernel_of


def temp_split(sz, host):
    """Live spill vs files older than the process. The split, not the sum.

    A temp file older than the process cannot belong to a query running in it. Summing the two is
    what made a server look like it was spilling 72.6 GB when it was spilling nothing and holding
    the wreckage of a run that was killed.
    """
    started = time.time() - host["uptime"] if host.get("uptime") else None
    orph = [f for f in (sz.get("temp_files") or []) if started and f[0] < started]
    orph_bytes = sum(f[1] for f in orph)
    return orph, orph_bytes, max(0, (sz.get("temp") or 0) - orph_bytes)


def storage(s, sz, host):
    tot = sz.get("total") or 1
    orph, orph_bytes, live_spill = temp_split(sz, host)
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
        "blocks": dict(zip(("total", "used", "free", "size_bytes"), s["blocks"], strict=True)),
    }


def memory(s, host):
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
        "pools": dict(s["memtags"]),
    }


def activity(s, max_query_chars=400):
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


def threads(rows, tcpu, by_tid, host, window):
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


def profile(newest, tops):
    fam = {}
    for sym, pct in tops:
        fam[kernel_of(sym)] = fam.get(kernel_of(sym), 0.0) + pct
    tot = sum(fam.values()) or 1
    return {
        "capture": newest,
        "note": "shares of sampled cycles over the newest captures. Engine shares are computed "
                "over the whole profile, not over the symbols listed here.",
        "engines": {k: round(v / tot * 100, 1) for k, v in
                    sorted(fam.items(), key=lambda kv: -kv[1])},
        "symbols": [{"symbol": re.sub(r"^\[[.k]\]\s*", "", sym),
                     "kernel": sym.startswith("[k]"),
                     "engine": kernel_of(sym), "percent": round(pct, 2)}
                    for sym, pct in tops[:25]],
    }


def hostinfo(host, s):
    ram = host.get("ram_total") or 0
    lim = (s or {}).get("memlimit") or 0
    return {
        "container": host.get("container"), "pid": host.get("pid"),
        "uptime_seconds": int(host.get("uptime") or 0),
        "cores": host.get("cores"), "load": host.get("load"),
        "ram_total_bytes": ram, "ram_available_bytes": host.get("ram_avail"),
        "swap_total_bytes": host.get("swap_total"),
        "swap_used_bytes": (host.get("swap_total") or 0) - (host.get("swap_free") or 0),
        "memory_limit_fraction_of_ram": round(lim / ram, 4) if ram else None,
    }


def findings(s, sz, host, hist=None):
    """Conditions that were MEASURED, each with the numbers behind it and how to check it.

    Not a severity list and not a verdict: an entry is here because a specific comparison came out a
    specific way, and it carries enough for the reader to disagree. An empty list means nothing
    tripped, not that nothing was looked at.
    """
    out = []
    _orph, orph_bytes, _live = temp_split(sz or {}, host)
    if orph_bytes:
        out.append({
            "what": "orphaned temp files",
            "detail": f"{len(_orph)} files, {human(orph_bytes)}, all older than the running "
                      f"serened. DuckDB deletes temp files only in a destructor and never sweeps "
                      f"at startup, so a killed server leaks them and no later run reclaims them.",
            "bytes_reclaimable": orph_bytes,
            "verify": "nothing holds them open: ls -l /proc/1/fd | grep -c tmp, inside the container",
        })
    if (host.get("swap") or 0) > 0:
        out.append({
            "what": "process memory paged out",
            "detail": f"{human(host['swap'])} of serened is in swap"
                      + (f" while duckdb_memory() reports {human(s['mem'])} held" if s else "")
                      + ". Every touch of that memory is a disk read, and memory_limit does not "
                        "count it.",
            "swap_bytes": host.get("swap"), "rss_bytes": host.get("rss"),
        })
    if s:
        ratio = s["wal"] / s["size"] if s["size"] else 0
        if ratio > 1:
            out.append({
                "what": "checkpoints not completing",
                "detail": f"WAL is {ratio:.1f}x the database. Look for write errors, not for tuning.",
                "wal_bytes": s["wal"], "database_bytes": s["size"],
            })
        lim, ram = s["memlimit"] or 0, host.get("ram_total") or 0
        if ram and lim > ram * 0.75:
            out.append({
                "what": "memory_limit oversubscribed",
                "detail": f"memory_limit is {lim / ram * 100:.0f}% of the machine's {human(ram)} "
                          f"of RAM, which anything else on the box has to fit around.",
                "memory_limit_bytes": lim, "ram_total_bytes": ram,
            })
        for name in sorted(HAZARDS):
            why, pred = HAZARDS[name]
            warn = pred(str(s["settings"].get(name, "?")), s) if pred else None
            if warn:
                out.append({"what": f"setting: {name}", "detail": warn,
                            "value": s["settings"].get(name)})
    # Everything above is a comparison against a threshold that someone chose. These are against
    # the series' own recent past, so they catch the shapes no threshold can: a pool that has been
    # climbing all afternoon is not over any limit until it is.
    out.extend(a.as_finding() for a in scan(hist or {}))
    return out


def collect(cfg, thread_window=1.0, query_head=400, hist=None):
    """Everything, in one pass. `error` instead of the SQL panels when the server cannot be reached.

    The /proc, du and perf panels do not need credentials and are collected either way — a server
    that will not let this connect is exactly when the host-side numbers are worth having.
    """
    s = db.sample(cfg, query_head=query_head)
    pid = system.host_pid(cfg)
    host = system.hostinfo(pid, cfg["container"])
    sz = system.slow(cfg, cfg["data"])
    rows, tcpu = [], 0.0
    if pid:
        _, _, prev, t0 = system.threads(pid, {}, time.time())
        time.sleep(thread_window)
        rows, tcpu, _, _ = system.threads(pid, prev, t0)
    newest, tops, by_tid = perf.perf_window(cfg["perf_dir"])

    out = {
        "findings": findings(s, sz, host, hist),
        "threads": threads(rows, tcpu, by_tid, host, thread_window),
        "profile": profile(newest, tops),
        "host": hostinfo(host, s),
    }
    if s is None:
        why = db.sql_status(cfg)
        out["sql"] = {"available": False, "reason": why[0] if why else "unavailable",
                      "fix": why[1] if why else "",
                      "note": "storage, memory, activity and config need a connection; the panels "
                              "above are read from /proc, du and perf captures and do not."}
        return out
    out["sql"] = {"available": True}
    out["storage"] = storage(s, sz, host)
    out["memory"] = memory(s, host)
    out["activity"] = activity(s, query_head)
    out["config"] = {n: s["settings"].get(n) for n in sorted(HAZARDS)}
    return out

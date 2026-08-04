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


# Consolidation slower than the interval it is scheduled on cannot finish between two runs, so the
# backlog can only grow. `compaction_interval` defaults to 1000 ms and is fixed at CREATE INDEX
# time, which is why the finding names the default it compared against rather than claiming to know
# this index's setting. Measured here: avg_consolidation_time_ms read 672 in one sample and 15,368
# an hour later on a 16-segment index, with compaction_active at 1.
CONSOLIDATION_MS = 1000

# A CHOSEN threshold. Neither sdb_metrics nor the documentation says how many deleted documents an
# inverted index should be carrying - consolidation reclaims them on its own schedule and there is
# no published target. 10% is picked, and the finding reports the share and the bytes it works out
# to against index_size so the number can be argued with rather than trusted.
DELETED_SHARE = 0.10


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


def storage(s, sz, host, held=None):
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
        # The split above is inferred from file mtimes against the process start time. This is the
        # server's own answer to the same question - duckdb_temporary_files() lists what it has open
        # right now - so it settles the orphan claim from the inside rather than circumstantially.
        "server_temp_files_held": held[0] if held else None,
        "server_temp_files_held_bytes": held[1] if held else None,
        "server_temp_files_note": "held is duckdb_temporary_files(): the temp files the SERVER has "
                                  "open, not an inference from mtimes. 0 held against a non-empty "
                                  "temp directory means every byte there is orphaned. null means "
                                  "the query did not run - not that nothing is held.",
        "blocks": dict(zip(("total", "used", "free", "size_bytes"), s["blocks"], strict=True)),
    }


def memory(s, host):
    rss, swap = host.get("rss") or 0, host.get("swap") or 0
    spill = dict(s.get("memspill") or {})
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
        # duckdb_memory().temporary_storage_bytes, per tag: WHICH pool spilled. The storage panel
        # can only say that the temp directory has bytes in it, and a temp directory is as often
        # the wreckage of a killed run as it is live spill. This names the operator.
        "spilled_bytes_by_pool": spill,
        "spilled_bytes_total": sum(spill.values()),
        "spill_note": "temporary_storage_bytes per pool, from the same duckdb_memory() row as the "
                      "usage above. Only pools with a non-zero figure appear, so an empty object "
                      "means no pool reports spill right now - it does not mean the temp directory "
                      "is empty (see storage.spill_orphaned_bytes).",
    }


def progress(rows, max_rows=25):
    """`sdb_progress` for the backends the server reports as active, BOUNDED.

    `pg_stat_activity` says a statement is running; this says how far in and which phase, which is
    the difference between waiting for it and killing it.

    An empty list is reported as unavailable rather than as "nothing is running", and that is not a
    guess: the connection asking is itself active, so it appears in its own result - every call that
    reaches the view gets at least one row back. Measured on this deployment: three active rows for
    two real statements plus the asking backend. So zero rows means the view did not answer (an
    older server without `sdb_progress`), which is a different claim from an idle server.
    """
    if not rows:
        return {"available": False,
                "reason": "sdb_progress returned no rows at all",
                "detail": "the connection that asks is active by construction and appears in its "
                          "own result, so an empty result means the view could not be read - not "
                          "that nothing is running. Check with: select * from sdb_progress",
                "note": "activity.sessions above is unaffected; it comes from pg_stat_activity"}
    out = {"available": True, "rows": [dict(r) for r in rows[:max_rows]],
           "rows_reported": len(rows),
           "note": "one of these rows is the connection that collected this - sdb_progress has no "
                   "pg_backend_pid() equivalent to exclude it, unlike sessions above, which does. "
                   "percent is the server's own figure; rows_done/rows_total and "
                   "bytes_done/bytes_total are the counters behind it. A row with no command, no "
                   "phase and zero counters carries no progress information - it is a backend the "
                   "server tracks as active without a tracked phase.",
           "rows_with_a_phase": sum(1 for r in rows if r.get("phase") or r.get("command"))}
    if len(rows) > max_rows:
        out["rows_truncated"] = True
        out["note"] += f" Cut at {max_rows} of {len(rows)} rows."
    return out


def activity(s, max_query_chars=400, prog=None):
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
    out = {
        "sessions_total": sum(s["states"].values()),
        "by_state": s["states"],
        "note": "excludes this connection, which is active by construction. Statement text is cut "
                f"at {max_query_chars} chars; query_chars is always the full length.",
        "nothing_running": not active,
        "nothing_running_means": "a pinned core with no active session is work with no session "
                                 "behind it - an orphaned server-side task",
        "sessions": rows,
    }
    if prog is not None:
        out["progress"] = progress(prog)
    return out


def _num(v):
    """sdb_metrics values arrive as ints already; anything else stays out of the arithmetic."""
    return v if isinstance(v, int) else None


def search(sr):
    """`sdb_metrics`: the inverted indexes, per index, every count beside what it divides.

    This is the engine SereneDB actually is and no payload had a number from it. The two hypotheses
    for the periodic CPU spikes on this deployment - refresh coupled to the autocheckpoint, or
    compaction - are told apart by four figures in here (`refresh_active`, `refresh_pending`,
    `compaction_active`, `avg_consolidation_time_ms`), and neither could be checked without them.

    None in means the table could not be read, and that is reported as unavailable rather than as an
    empty result: "no index reported anything" and "nothing is wrong with the indexes" are different
    claims and only one of them was measured.
    """
    if sr is None:
        return {"available": False,
                "reason": "sdb_metrics could not be read",
                "fix": "needs a connection and a server that has the table: check "
                       "`select 1 from sdb_metrics limit 1` - sql.available says whether the "
                       "connection itself is the problem",
                "note": "not a report that the indexes are healthy - nothing was measured"}
    srv, per = dict(sr.get("server") or {}), dict(sr.get("indexes") or {})
    if not srv and not per:
        return {"available": False,
                "reason": "sdb_metrics returned no rows",
                "note": "the server-wide counters are emitted unconditionally, so an empty table is "
                        "the view not answering rather than an idle engine"}
    out = {
        "available": True,
        "server": {
            "pg_connections": srv.get("pg_connections"),
            "http_connections": srv.get("http_connections"),
            "maintenance": {k: {"active": srv.get(f"{k}_active"), "pending": srv.get(f"{k}_pending")}
                            for k in ("refresh", "compaction", "cleanup")},
            "note": "active is what is running now, pending is what is queued behind it, both as "
                    "job counts rather than document counts. pending above zero while active is "
                    "also non-zero is maintenance not keeping up with the write rate; either one "
                    "alone is ordinary.",
        },
        "indexes": [],
        "note": "per-index rows keyed by relation_id. index_size_bytes is the engine's own "
                "accounting of its segment files; storage.directories.search_index_bytes is du "
                "over the index directory. Two different measures - they will not match. "
                "avg_*_time_ms are the server's own averages and sdb_metrics does not say over "
                "what window, so read them as a level, not as a rate between two calls.",
    }
    for rel in sorted(per):
        m = per[rel]
        docs, live = _num(m.get("num_docs")), _num(m.get("num_live_docs"))
        segs, size = _num(m.get("num_segments")), _num(m.get("index_size"))
        deleted = max(0, docs - live) if docs is not None and live is not None else None
        row = {
            "relation_id": rel,
            "num_docs": docs,
            "num_live_docs": live,
            # num_docs includes deleted-but-not-reclaimed postings, so the share is against
            # num_docs - the thing the deleted documents are still occupying space inside.
            "deleted_docs": deleted,
            "deleted_share_of_docs": round(deleted / docs, 6) if deleted and docs else 0.0,
            "num_buffered_docs": _num(m.get("num_buffered_docs")),
            "num_segments": segs,
            "num_files": _num(m.get("num_files")),
            "index_size_bytes": size,
            "bytes_per_live_doc": round(size / live) if size and live else None,
            "live_docs_per_segment": round(live / segs) if live and segs else None,
        }
        for k in ("num_failed_commits", "num_failed_cleanups", "num_failed_consolidations",
                  "avg_commit_time_ms", "avg_cleanup_time_ms", "avg_consolidation_time_ms"):
            row[k] = _num(m.get(k))
        out["indexes"].append(row)
    out["total_index_size_bytes"] = sum(r["index_size_bytes"] or 0 for r in out["indexes"])
    out["indexes_reported"] = len(out["indexes"])
    if not per:
        out["indexes_note"] = ("no per-index rows: this server has no inverted index, or none has "
                               "reported metrics yet. The server-wide counters above are still live.")
    return out


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


def search_findings(sr):
    """The `sdb_metrics` half of `findings`. Four comparisons, each carrying what it compared.

    Kept separate because the thresholds behind two of them need saying out loud: one is the
    documented `compaction_interval` default and the other is a number someone chose. A finding
    whose threshold cannot be defended is worse than no finding, so both say which they are.
    """
    out = []
    if not sr or not sr.get("available"):
        return out
    srv = sr.get("server") or {}
    for kind in ("refresh", "compaction", "cleanup"):
        m = (srv.get("maintenance") or {}).get(kind) or {}
        act, pend = m.get("active") or 0, m.get("pending") or 0
        # Pending alone is a queue that the next run will drain. Pending WHILE something is already
        # running is work arriving faster than the running job retires it, which is the shape that
        # does not fix itself.
        if pend and act:
            out.append({
                "what": f"search {kind} queue behind a running {kind}",
                "detail": f"{pend} {kind} job(s) pending while {act} is already active. That is the "
                          f"write rate outrunning maintenance rather than a queue waiting for its "
                          f"turn - a single pending count with nothing active drains on the next "
                          f"interval.",
                f"{kind}_pending": pend, f"{kind}_active": act,
                "verify": "select metric, value from sdb_metrics where relation_id is null",
            })
    for ix in sr.get("indexes") or []:
        rel = ix["relation_id"]
        fails = {k: ix[k] for k in ("num_failed_commits", "num_failed_cleanups",
                                    "num_failed_consolidations") if ix.get(k)}
        if fails:
            out.append({
                "what": f"search index {rel}: failed maintenance",
                "detail": ", ".join(f"{k} = {v}" for k, v in sorted(fails.items()))
                          + ". These are cumulative counters with no timestamp beside them, so a "
                            "non-zero value may be old - it says something failed, not that it is "
                            "failing now.",
                "relation_id": rel, **fails,
                "verify": "select timestamp, log_level, message from duckdb_logs() where type in "
                          "('Search','IResearch') order by timestamp desc limit 20  (needs logging "
                          "enabled)",
            })
        docs, deleted = ix.get("num_docs") or 0, ix.get("deleted_docs") or 0
        share = ix.get("deleted_share_of_docs") or 0.0
        size = ix.get("index_size_bytes") or 0
        if docs and share > DELETED_SHARE:
            out.append({
                "what": f"search index {rel}: deleted documents not reclaimed",
                "detail": f"{deleted:,} of {docs:,} documents ({share * 100:.1f}%) are deleted but "
                          f"still in the index, against {human(size)} of segments - about "
                          f"{human(int(size * share))} of postings for documents that can no longer "
                          f"match. {DELETED_SHARE * 100:.0f}% is a CHOSEN threshold, not a "
                          f"documented one: nothing published says what an index should carry. "
                          f"Consolidation is what reclaims them.",
                "relation_id": rel, "deleted_docs": deleted, "num_docs": docs,
                "deleted_share_of_docs": share, "index_size_bytes": size,
                "threshold_share": DELETED_SHARE, "threshold_is_chosen": True,
                "verify": "VACUUM (COMPACT_INDEX <name>), then read num_docs and num_live_docs "
                          "again",
            })
        cons = ix.get("avg_consolidation_time_ms") or 0
        if cons > CONSOLIDATION_MS:
            out.append({
                "what": f"search index {rel}: consolidation slower than its interval",
                "detail": f"avg_consolidation_time_ms is {cons:,} across "
                          f"{ix.get('num_segments')} segments, {cons / CONSOLIDATION_MS:.1f}x the "
                          f"{CONSOLIDATION_MS} ms compaction_interval default. A merge that takes "
                          f"longer than the interval it is scheduled on cannot finish between two "
                          f"runs, so segments accumulate. The interval is fixed at CREATE INDEX "
                          f"time and sdb_metrics does not carry it - this compared against the "
                          f"documented default, so read the index's own setting before acting.",
                "relation_id": rel, "avg_consolidation_time_ms": cons,
                "num_segments": ix.get("num_segments"),
                "threshold_ms": CONSOLIDATION_MS,
                "threshold_source": "documented compaction_interval default (1000 ms)",
                "verify": "compaction_active in sdb_metrics while this is high says it is running "
                          "now; the CREATE INDEX statement says what the interval actually is",
            })
    return out


def findings(s, sz, host, hist=None, sr=None, held=None):
    """Conditions that were MEASURED, each with the numbers behind it and how to check it.

    Not a severity list and not a verdict: an entry is here because a specific comparison came out a
    specific way, and it carries enough for the reader to disagree. An empty list means nothing
    tripped, not that nothing was looked at.
    """
    out = []
    _orph, orph_bytes, _live = temp_split(sz or {}, host)
    if orph_bytes:
        # `held` is the server's own answer to the same question. The mtime split is sound but
        # circumstantial; 0 files open against a full temp directory is the claim proving itself.
        proof = ""
        if held is not None and held[0] == 0:
            proof = (" duckdb_temporary_files() reports the server holding 0 files open, so none of "
                     "this belongs to a query running in it.")
        elif held is not None:
            proof = (f" duckdb_temporary_files() reports the server holding {held[0]} files "
                     f"({human(held[1])}) open, which are NOT these.")
        out.append({
            "what": "orphaned temp files",
            "detail": f"{len(_orph)} files, {human(orph_bytes)}, all older than the running "
                      f"serened. DuckDB deletes temp files only in a destructor and never sweeps "
                      f"at startup, so a killed server leaks them and no later run reclaims them."
                      + proof,
            "bytes_reclaimable": orph_bytes,
            "server_temp_files_held": held[0] if held else None,
            "verify": "select count(*), sum(size) from duckdb_temporary_files() - the server's own "
                      "list of what it has open. Or, inside the container: ls -l /proc/1/fd | "
                      "grep -c tmp",
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
    out.extend(search_findings(sr))
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

    # Three more round trips, and they are not on the tick path - the TUI's 5s loop is db.sample().
    # Skipped entirely when sample() already failed, so an unreachable server costs one connect
    # timeout rather than four. search(None) then reports unavailable, which is the point: an
    # empty search payload would read as "the indexes are fine".
    sr = search(db.search(cfg) if s is not None else None)
    held = db.temp_files_held(cfg) if s is not None else None
    prog = db.progress(cfg) if s is not None else None

    out = {
        "findings": findings(s, sz, host, hist, sr=sr, held=held),
        "threads": threads(rows, tcpu, by_tid, host, thread_window),
        "profile": profile(newest, tops),
        "host": hostinfo(host, s),
        "search": sr,
    }
    if s is None:
        why = db.sql_status(cfg)
        out["sql"] = {"available": False, "reason": why[0] if why else "unavailable",
                      "fix": why[1] if why else "",
                      "note": "storage, memory, activity, search and config need a connection; the "
                              "panels above are read from /proc, du and perf captures and do not."}
        return out
    out["sql"] = {"available": True}
    out["storage"] = storage(s, sz, host, held)
    out["memory"] = memory(s, host)
    out["activity"] = activity(s, query_head, prog)
    out["config"] = {n: s["settings"].get(n) for n in sorted(HAZARDS)}
    return out

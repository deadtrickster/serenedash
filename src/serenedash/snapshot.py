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


def recorded(perf_dir, limit=40, interval=5.0):
    """What the dashboard SAW run, longest first. The half `pg_stat_activity` cannot answer.

    That view is present-tense and this server has no `pg_stat_statements`, `log_query_path` is
    empty and profiling is off - so a statement that has ended leaves no trace anywhere else. The
    dashboard samples the live view every tick anyway, so this costs the server nothing.

    Every caveat that matters is in the payload rather than implied: it is SAMPLED, so anything
    shorter than one interval was never seen, and a duration is the last age the server reported,
    which is a lower bound. Reporting these as exact would be the same class of mistake as calling
    a thread percentage a share of the machine.
    """
    from . import statements                                     # noqa: PLC0415  - avoid a cycle
    rows = statements.recent(perf_dir, limit=limit)
    if not rows:
        return {"available": False,
                "reason": f"nothing recorded yet at {statements.path(perf_dir)}",
                "fix": "run the dashboard (serenedash) - it records one sample per tick, and the "
                       "file outlives it",
                "note": "this is the dashboard's own record, not the server's. SereneDB has no "
                        "pg_stat_statements; log_query_path and profiling are the server-side "
                        "options and both are off by default."}
    live, done = statements.running(rows, interval=interval)
    return {
        "available": True,
        "statements_recorded": len(rows),
        "still_running": len(live),
        "sampling_interval_s": interval,
        "note": "SAMPLED from pg_stat_activity once per tick, by the dashboard, not by the server. "
                "A statement shorter than one interval was never seen. ran_for_s is the last age "
                "the server reported for it, so it is accurate to within one interval and is a "
                "LOWER bound - the statement may have run on after the last tick that saw it.",
        "statements": [{
            "ran_for_s": r.get("ran_for_s"),
            "still_running": r in live,
            "pid": r.get("pid"),
            "statement_chars": r.get("chars"),
            "statement": r.get("statement"),
            "samples": r.get("samples"),
            "first_seen_epoch": r.get("started"),
            "last_seen_epoch": r.get("last_seen"),
            "client_addr": r.get("client_addr") or None,
            "application_name": r.get("application_name") or None,
        } for r in rows],
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
    for st, q, full_len, *rest in s["queries"]:
        age, conn, pid = (list(rest) + [-1, -1, ""])[:3]
        if "pg_stat_activity" in q:
            continue
        row = {"state": st, "query": (q[:max_query_chars] or None), "query_chars": full_len}
        if full_len > len(row["query"] or ""):
            row["query_truncated"] = True
        # Age, because a statement's text says what it is doing and nothing about whether anyone is
        # still waiting for it. -1 is "the server did not say", which is not 0.
        addr, app = (list(rest) + [-1, -1, "", "", ""])[3:5]
        if addr:
            row["client_addr"] = addr
        if app:
            row["application_name"] = app
        if age >= 0:
            row["running_for_s"] = age
        if conn >= 0:
            row["connection_age_s"] = conn
            # conn_age > query_age is a pooled connection that ran other statements first: a pool,
            # not a person, is holding it - the shape an abandoned request leaves behind.
            row["on_a_pooled_connection"] = conn > age + 1
        if pid:
            row["pid"] = pid
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


# When a running statement stops being "busy" and starts being a finding. A CHOSEN threshold, and
# the row says so by carrying the age itself. An hour is past every legitimate analytical query on
# the deployment this was measured on; the incident that produced this rule ran for 42 of them.
ABANDONED_S = 3600


# A checkpoint statement, forced or not. Matched on the text because that is what the server gives
# us - there is no "waiting for a lock" column anywhere in this deployment's catalog.
CHECKPOINT_RE = re.compile(r"^\s*(force\s+)?checkpoint\b", re.I)

# When a checkpoint has clearly stopped making progress rather than merely taking a while. A
# checkpoint on a 110 GB database is minutes of real work, so this is deliberately past that.
CHECKPOINT_STUCK_S = 120


# A share of the MACHINE, not of a core. Sustained above this with a statement nobody is waiting
# for is the shape of the incident this was written from: five of 24 cores, for two days, while
# ingestion sat at zero documents a minute.
BURN_FRACTION = 0.15


def cpu_burn(s, host, tcpu=None):
    """The machine is busy and the oldest thing on it is not finishing.

    Neither half is a finding alone. A busy server is a working server, and a long statement on an
    idle machine is waiting for something rather than burning. Together they are "cores are being
    spent on work nobody is waiting for", which is what nothing on the dashboard said while five
    were pinned for 46 hours.

    Deliberately NOT an anomaly rule. Those compare against the recent past and this is a LEVEL: it
    has held for two days, so there is nothing for a change detector to see. The existing
    `anomaly: process CPU spike` fires on the transition and then goes quiet exactly when the
    problem becomes permanent.
    """
    # `tcpu` is the process CPU as a share of ONE core, summed over threads - the same number the
    # threads panel prints. It is NOT on `host`: hostinfo reads /proc for memory, threads and
    # uptime and has never carried a CPU figure, so the first version of this finding read a key
    # that does not exist and could not fire.
    cores = (host or {}).get("cores") or 0
    used = tcpu
    if not cores or used is None:
        return []
    share = used / (cores * 100.0)
    oldest = max((q for q in (s or {}).get("queries", [])
                  if len(q) > 3 and q[0] == "active" and "pg_stat_activity" not in q[1]),
                 key=lambda q: q[3], default=None)
    if share < BURN_FRACTION or not oldest or oldest[3] < ABANDONED_S:
        return []
    return [{
        "kind": "cpu",
        "what": f"{used / 100:.1f} of {cores} cores busy, oldest statement {human_time(oldest[3])}",
        "detail": f"the process is using {share * 100:.0f}% of the machine and the oldest active "
                  f"statement has been running {human_time(oldest[3])}"
                  + (f" (pid {oldest[5]})" if len(oldest) > 5 and oldest[5] else "")
                  + ". Busy is not a finding and a long statement is not a finding; together they "
                    "mean cores are being spent on work nobody is waiting for. This is a LEVEL, "
                    "not a spike - the anomaly rules compare against the recent past and go quiet "
                    "once a burn has lasted long enough to be the new normal.",
        "cpu_percent_of_one_core": used, "cores": cores,
        "share_of_machine": round(share, 3),
        "oldest_statement_s": oldest[3],
        "pid": (oldest[5] if len(oldest) > 5 else ""),
        "verify": "compare `threads` against `activity`: the per-thread percentages are shares of "
                  "ONE core, and the statement ages say whether anyone is waiting for the result",
        "threshold_share": BURN_FRACTION, "threshold_is_chosen": True,
    }]


def checkpoint_waiting(s):
    """A CHECKPOINT that is itself running long. It is not working, it is waiting.

    Worth its own finding because it looks like ordinary activity - `active 25m` beside six other
    statements - and because the remedy printed in the server's own error message is what put it
    there. FORCE CHECKPOINT waits for the active transactions rather than aborting them, so against
    a statement nobody is waiting for it cannot finish.
    """
    out = []
    for row in (s or {}).get("queries", []):
        if len(row) < 4 or row[0] != "active" or not CHECKPOINT_RE.match(row[1] or ""):
            continue
        age, pid = row[3], (row[5] if len(row) > 5 else "")
        if age < CHECKPOINT_STUCK_S:
            continue
        forced = bool(CHECKPOINT_RE.match(row[1]).group(1))
        # The likely holder: the oldest OTHER active statement. A checkpoint needs every
        # transaction finished, so the oldest one is what it is waiting behind.
        others = [r for r in s["queries"]
                  if len(r) > 3 and r[0] == "active" and r is not row
                  and not CHECKPOINT_RE.match(r[1] or "") and "pg_stat_activity" not in r[1]]
        oldest = max(others, key=lambda r: r[3], default=None)
        behind = ""
        if oldest and oldest[3] > age:
            behind = (f" The oldest statement in front of it has been running "
                      f"{human_time(oldest[3])}"
                      + (f" (pid {oldest[5]})" if len(oldest) > 5 and oldest[5] else "")
                      + ", and a checkpoint needs every transaction finished, reads included.")
        out.append({
            "kind": "activity",
            "what": f"{'FORCE ' if forced else ''}CHECKPOINT waiting for {human_time(age)}",
            "detail": "this checkpoint is not working, it is waiting."
                      + (" FORCE CHECKPOINT retries in a loop with no timeout and no backoff, and "
                         "it waits for the active transactions rather than aborting them - so "
                         "against a statement nobody is waiting for it does not finish. While it "
                         "waits it holds start_transaction_lock, which stops EVERY new transaction "
                         "from starting, reads included - so the pile-up behind it is not only "
                         "writers. And the retry loop is a busy spin with no sleep, so it burns a "
                         "core the whole time (duck_transaction_manager.cpp:295-307). It is "
                         "interruptible, which makes cancelling it the one safe move here."
                         if forced else
                         " A plain CHECKPOINT errors rather than waiting, so one that is still "
                         "active is doing the work; if it stays here it is the checkpoint itself "
                         "that is slow, which on this database is compression of the embeddings.")
                      + behind,
            "waiting_for_s": age, "pid": pid, "forced": forced,
            "blocked_by_pid": (oldest[5] if oldest and len(oldest) > 5 else None),
            "blocked_by_age_s": (oldest[3] if oldest else None),
            "fix": "there is nothing to wait for. Cancel it, deal with the statement in front of "
                   "it, then run a plain CHECKPOINT - which errors immediately rather than "
                   "hanging, so it is safe to use as a test of whether the way is clear.",
            "verify": "select pid, round(extract(epoch from (now()-query_start))) age_s, query "
                      "from pg_stat_activity where state = 'active' order by query_start",
            "threshold_s": CHECKPOINT_STUCK_S, "threshold_is_chosen": True,
        })
    return out


def long_running(s):
    """Statements old enough that nobody is plausibly waiting for them.

    The number the dashboard did not collect. It had what was running and how big the statement was
    and nothing about WHEN it started, so a 42-hour query and a one-second query drew the same row.

    Reads are included on purpose, and are the point: on this engine a checkpoint needs every
    transaction finished before it can rewrite the file, so an ordinary SELECT blocks it outright.
    Anyone reasoning from PostgreSQL - where a long read holds back vacuum and not checkpoints -
    rules SELECT out immediately, which is how two of them ran for 42 hours unnoticed.
    """
    out = []
    for row in (s or {}).get("queries", []):
        if len(row) < 4 or row[0] != "active" or "pg_stat_activity" in row[1]:
            continue
        age, conn, pid, addr, app = (list(row) + [-1, -1, "", "", ""])[3:8]
        if age < ABANDONED_S:
            continue
        pooled = conn > age + 1
        who = " · ".join(x for x in (f"pid {pid}" if pid else "",
                                     f"from {addr}" if addr else "",
                                     f"app {app}" if app else "") if x)
        out.append({
            "kind": "activity",
            "what": f"statement running for {human_time(age)}",
            "detail": f"active for {human_time(age)}"
                      + (f" on a connection {human_time(conn)} old, so a pool rather than a person "
                         f"is holding it - the shape an abandoned request leaves behind" if pooled
                         else "")
                      + ". On this engine a checkpoint needs every transaction finished before it "
                        "can rewrite the file, so this blocks checkpointing whether it reads or "
                        "writes - which is NOT the PostgreSQL model, where a long read holds back "
                        "vacuum and never touches a checkpoint.",
            "holder": who or "the server named no client for it",
            "running_for_s": age, "connection_age_s": conn, "pid": pid,
            "client_addr": addr, "application_name": app,
            "statement_chars": row[2],
            # As much of it as the sample carries. The row shows a head; the opened finding shows
            # this, scrollable, with the true length beside it - a truncated statement must never
            # be mistaken for a short one.
            "statement": row[1],
            "fix": f"if nobody is waiting for it: select pg_terminate_backend({pid or '<pid>'}), "
                   f"then CHECKPOINT to recover the WAL. It is read-only work, so nothing rolls "
                   f"back. Do NOT use FORCE CHECKPOINT - it waits for the transaction rather than "
                   f"aborting it, which is the wrong move against a statement nobody is waiting "
                   f"for. To stop it recurring the only setting that bites is max_execution_time "
                   f"(0 = off here): statement_timeout is accepted and NOT enforced, and "
                   f"pg_statement_timeout_millis is for outbound scan connections to an ATTACHed "
                   f"Postgres, not for anything a client sends here. Note max_execution_time is "
                   f"armed after parsing, so it does not bound the parse itself.",
            "verify": "select pid, state, round(extract(epoch from (now()-query_start))) age_s "
                      "from pg_stat_activity where state = 'active' order by query_start",
            "threshold_s": ABANDONED_S, "threshold_is_chosen": True,
        })
        if pid:
            # The dashboard can do this one itself. Every other `fix` here is a command to run
            # elsewhere; this is the row that already knows the pid.
            out[-1]["action"] = ("terminate", pid)
    return out


def human_time(sec):
    """Seconds as something a person reads. Hours matter here; milliseconds do not."""
    sec = int(sec)
    if sec < 90:
        return f"{sec}s"
    if sec < 5400:
        return f"{sec // 60}m"
    if sec < 172800:
        return f"{sec // 3600}h {(sec % 3600) // 60}m"
    return f"{sec // 86400}d {(sec % 86400) // 3600}h"


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
            # What this row may NOT say: that consolidation is behind. It used to - "consolidation
            # slower than its interval ... so segments accumulate" - and both halves were inference.
            # The interval is fixed at CREATE INDEX and appears in no readable place on this server
            # (35 rows in sdb_settings, 20 metrics in sdb_metrics, none of them it), so the
            # comparison was against a documented default that may not be this index's. And a
            # backlog is directly measured: compaction_pending and compaction_active. On the
            # deployment that produced this rule they were 0 and 0 with num_failed_consolidations
            # 0, which is the opposite of a merge that cannot keep up, and the row asserted it
            # anyway. A reader downstream repeated it as fact, which is what a `what` line stating
            # a conclusion gets you.
            comp = (srv.get("maintenance") or {}).get("compaction") or {}
            pend, act = comp.get("pending") or 0, comp.get("active") or 0
            queue = (f"compaction_pending is {pend} with {act} active, so there IS work queued "
                     f"behind this" if pend else
                     f"compaction_pending is 0 and compaction_active is {act}: nothing is queued "
                     f"right now, so this is the cost of a merge, not an observed backlog")
            out.append({
                "what": f"search index {rel}: each consolidation takes {cons / 1000:.1f}s",
                "detail": f"avg_consolidation_time_ms is {cons:,} across "
                          f"{ix.get('num_segments')} segments. {queue}. Whether that is slow "
                          f"depends on compaction_interval, which is fixed at CREATE INDEX time "
                          f"and is in neither sdb_settings nor sdb_metrics - the documented "
                          f"default is {CONSOLIDATION_MS} ms, and this row does not know whether "
                          f"this index uses it. Read the CREATE INDEX statement before treating "
                          f"the ratio as real.",
                "relation_id": rel, "avg_consolidation_time_ms": cons,
                "num_segments": ix.get("num_segments"),
                "compaction_pending": pend, "compaction_active": act,
                "num_failed_consolidations": ix.get("num_failed_consolidations") or 0,
                "documented_interval_ms": CONSOLIDATION_MS,
                "interval_readable_here": False,
                "verify": "select metric, value from sdb_metrics where relation_id is null - "
                          "compaction_pending and compaction_active are the backlog itself, and "
                          "they beat any comparison against an interval this server will not name",
            })
    return out


def findings(s, sz, host, hist=None, sr=None, held=None, tcpu=None):
    """Conditions that were MEASURED, each with the numbers behind it and how to check it.

    Not a severity list and not a verdict: an entry is here because a specific comparison came out a
    specific way, and it carries enough for the reader to disagree. An empty list means nothing
    tripped, not that nothing was looked at.

    Each carries a `kind` - storage, memory, setting, search, trend - set HERE rather than worked
    out later from the wording of `what`. The wording changes whenever a measurement is corrected,
    and a categoriser reading it would quietly start filing the corrected finding under "other".
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
            "kind": "storage",
            "what": "orphaned temp files",
            "detail": f"{len(_orph)} files, {human(orph_bytes)}, all older than the running "
                      f"serened. DuckDB deletes temp files only in a destructor and never sweeps "
                      f"at startup, so a killed server leaks them and no later run reclaims them."
                      + proof,
            "bytes_reclaimable": orph_bytes,
            "server_temp_files_held": held[0] if held else None,
            # Spelled out because a reader with the "never swept at startup" sentence in front of
            # it still proposed restarting the server to clear them. The distinction the sentence
            # turns on: a CLEAN shutdown runs the destructor and removes what that process was
            # holding, so an orderly restart does not leak. What no restart does, clean or not, is
            # sweep the directory - so files left by an earlier unclean exit survive every one of
            # them, and these are those files.
            "fix": "delete the files. A restart will not reclaim these: a clean shutdown removes "
                   "only what the exiting process is holding, and nothing sweeps the directory at "
                   "startup, so what an earlier kill left behind survives any number of restarts. "
                   "Deleting is safe only while the server holds none of them open, which is the "
                   "server_temp_files_held number beside this: re-read it immediately before "
                   "deleting, because a query that starts spilling in between owns files in that "
                   "directory.",
            "verify": "select count(*), sum(size) from duckdb_temporary_files() - the server's own "
                      "list of what it has open. Or, inside the container: ls -l /proc/1/fd | "
                      "grep -c tmp",
        })
    if (host.get("swap") or 0) > 0:
        out.append({
            "kind": "memory",
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
                "kind": "storage",
                "what": "checkpoints not completing",
                "detail": f"WAL is {ratio:.1f}x the database. Look for write errors, not for tuning.",
                "wal_bytes": s["wal"], "database_bytes": s["size"],
            })
        lim, ram = s["memlimit"] or 0, host.get("ram_total") or 0
        if ram and lim > ram * 0.75:
            out.append({
                "kind": "memory",
                "what": "memory_limit oversubscribed",
                "detail": f"memory_limit is {lim / ram * 100:.0f}% of the machine's {human(ram)} "
                          f"of RAM, which anything else on the box has to fit around.",
                "memory_limit_bytes": lim, "ram_total_bytes": ram,
            })
        for name in sorted(HAZARDS):
            why, pred = HAZARDS[name]
            warn = pred(str(s["settings"].get(name, "?")), s) if pred else None
            if warn:
                out.append({"kind": "setting", "what": f"setting: {name}",
                            "detail": warn, "value": s["settings"].get(name)})
    out.extend(long_running(s))
    out.extend(cpu_burn(s, host, tcpu))
    out.extend(checkpoint_waiting(s))
    out.extend({"kind": "search", **f} for f in search_findings(sr))
    # Everything above is a comparison against a threshold that someone chose. These are against
    # the series' own recent past, so they catch the shapes no threshold can: a pool that has been
    # climbing all afternoon is not over any limit until it is.
    out.extend({"kind": "trend", **a.as_finding()} for a in scan(hist or {}))
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
        "findings": findings(s, sz, host, hist, sr=sr, held=held, tcpu=tcpu),
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
    out["activity"]["recorded"] = recorded(cfg["perf_dir"])
    out["config"] = {n: s["settings"].get(n) for n in sorted(HAZARDS)}
    return out


# Doctor's own status words, mapped onto the one thing a findings row needs to know: did this trip.
# `info` is neither - it is "could not be checked", which AGENTS.md is emphatic about not reporting
# as a pass.
TRIPPED = {"fail": 2, "warn": 1, "info": 1, "ok": 0}


def setup_findings(rows, fix=None):
    """`symbols.doctor` rows as findings, so one screen carries both.

    A doctor row is (status, name, detail, fix) and a finding is what/detail/kind plus its numbers -
    the same claim in a different shape. The status becomes `severity`, which is the field the list
    sorts on, and the runnable remedy travels as `action` so the screen can offer to do it rather
    than only print a command.
    """
    out = []
    for status, name, detail, remedy in rows or []:
        f = {"kind": "setup", "what": name, "detail": detail or "",
             "severity": TRIPPED.get(status, 1), "status": status}
        if remedy:
            f["fix"] = remedy
        # Only the row the fix belongs to gets the action, and `doctor` only ever reports one.
        if fix and status != "ok" and ("symbol" in name or "binary" in name or "perf" in name):
            f["action"] = fix
        out.append(f)
    return out

#!/usr/bin/env python3
"""serenedash — a live dashboard for a SereneDB server, cheap enough to leave open.

    ./serenedash.py                 refresh every 5s
    ./serenedash.py -n 2            faster
    ./serenedash.py --once          one frame, plain text (scripts, logs, cron mail)
    ./serenedash.py --no-color
    ./serenedash.py --container oracle-serenedb --port 7890

Keys: `q` quit · `c` effective config · `s` call graph · `l` legend · `j`/`k` scroll.

Config precedence: flags, then SERENEDB_CONTAINER / SERENEDB_PORT / PGPASSWORD, then the defaults
below. No dependencies — stdlib only.

## What it answers, and why each panel exists

Every panel here is a question that cost real time to answer by hand.

**Is the WAL running away?** A WAL reached 76 GB against a 23 GB database because no checkpoint
completed for two days — writes were failing, so nothing ever flushed. Nothing surfaced that. A
restart at that point would have replayed 76 GB. The ratio is the number that matters, not the size:
a big WAL under a bigger database is fine, a WAL several times the database is a stalled checkpoint.

**What is it actually doing?** `pg_stat_activity` carries live query text. When a core is pinned and
you cannot tell which statement is responsible, this is the answer — and when it shows *nothing*
running while a core is pinned, that is the answer too: orphaned server-side work, which has now
happened twice here (an abandoned COPY feeder, and a sort whose client was killed).

**Where is the memory going?** `duckdb_memory()` breaks it down by tag, so an IN_MEMORY_TABLE that
has quietly grown to gigabytes is visible rather than inferred from RSS.

**Is temp spilling, and to where?** The image ships a RELATIVE `temp_directory` (`.tmp`) resolved
against a root-owned cwd, so on a stock container every spill fails with EACCES — silently, until
the working set first exceeds memory_limit. Showing the setting and the spill volume together makes
that visible before it becomes a five-hour outage.

## Cost

`pragma database_size`, `duckdb_memory()` and `pg_stat_activity` are cheap. Directory sizes are not,
so `du` runs on its own timer (SLOW_EVERY ticks) and is remembered in between. Everything else is one
`docker exec` per tick carrying all queries at once, split client-side on a separator — process spawn
dominates at this scale, not the queries.
"""
import argparse
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import termios
import textwrap
import time
import tty

SEP = "---8<---"
SLOW_EVERY = 12
# Deep enough to fill the tail of a wide terminal; the renderer draws only the tail end that fits,
# so this is a retention limit rather than a width. At -n 5 it is a bit over two hours of history.
HIST = 160
SPARK = "▁▂▃▄▅▆▇█"

C = {"r": "\033[0m", "dim": "\033[2m", "b": "\033[1m", "grn": "\033[32m", "yel": "\033[33m",
     "red": "\033[31m", "cyn": "\033[36m", "mag": "\033[35m", "blu": "\033[34m"}
NOCOLOR = {k: "" for k in C}

# ── settings worth an opinion ───────────────────────────────────────────────────────────────────
#
# `duckdb_settings()` already ships a description for all 297, so the `c` view shows the server's
# own text and does not restate it. What is added here is only what we have MEASURED on a real
# deployment — consequences, not documentation. Each entry is (why it matters, predicate on the
# value returning a warning string or None).
#
# Deliberately short. A hazard list that grows to fifty entries stops being read, and the value of
# this one is that every line in it cost someone a day.
def _rel_temp(v, _s):
    if not str(v).startswith("/"):
        return ("RELATIVE path. serened runs as uid 999 with cwd /, which is root-owned, so this "
                "resolves to /.tmp and CANNOT BE CREATED. Every spill fails with EACCES — silently, "
                "until the working set first exceeds memory_limit.")
    return None


def _ckpt(v, s):
    wal, db = s.get("wal", 0), s.get("size", 0)
    thr = to_bytes(v)
    if thr and wal > max(thr * 50, 2**30):
        return (f"WAL is {human(wal)} against a threshold of {human(thr)} — {wal / thr:.0f}x. "
                "Automatic checkpointing is not completing; look for write errors, not for tuning.")
    return None


def _mem(v, s):
    if s.get("memlimit") and s.get("mem", 0) / s["memlimit"] > 0.9:
        return "over 90% of the limit — spilling is imminent, so temp_directory had better be valid"
    return None


HAZARDS = {
    "temp_directory": ("where spills go; a wrong value fails only under load", _rel_temp),
    "checkpoint_threshold": ("WAL size that should trigger an automatic checkpoint", _ckpt),
    "memory_limit": ("working set ceiling before spilling to temp_directory", _mem),
    "threads": ("parallelism; also multiplies per-thread memory", None),
    "preserve_insertion_order": ("false lets large sorts/inserts avoid materialising in order, "
                                 "which cuts both spill volume and time", None),
}




# ── which engine a hot symbol belongs to ────────────────────────────────────────────────────────
#
# The useful question about a profile here is not user-vs-kernel, it is WHICH ENGINE is burning the
# cycles: SereneDB is DuckDB columnar storage + IResearch text index + FAISS/BLAS vector search
# behind one pg-wire front end, and they fail in completely different ways.
#
# Measured examples that motivated each bucket:
#   vector   sgemm_kernel at 16% — IVF k-means retraining its centroids, single-threaded, while
#            23 cores waited. A matmul dominating means clustering, not search.
#   text     irs::FieldData::add_term, DelimitedTokenizer::next — inverted index insertion.
#   columnar duckdb::RLEState<float>::UpdateFlatValid — column encode.
#   wire     sdb::message::Buffer::ReadableSize at 96% of a fourteen-hour load — the COPY feeder
#            walking its whole chunk list per message to test a five-byte threshold.
KERNELS = (
    ("vector", ("gemm", "sgemm", "faiss", "IndexIVF", "Quantizer", "distance", "l2_", "knn",
                "cblas", "openblas", "simsimd", "hnsw")),
    ("text", ("irs::", "BM25", "Posting", "FieldData", "Tokenizer", "analysis::", "term_",
              "iresearch")),
    ("columnar", ("duckdb::", "RLE", "ColumnData", "RowGroup", "Vector::", "Compress")),
    ("wire", ("sdb::message", "pg_wire", "Buffer::", "CopyEod")),
    ("alloc", ("je_", "malloc", "free", "arena", "tcache")),
)


def kernel_of(sym):
    low = sym.lower()
    for name, pats in KERNELS:
        if any(pt.lower() in low for pt in pats):
            return name
    return "kernel" if sym.startswith("[k]") else "other"


# ── one column grid for every panel ─────────────────────────────────────────────────────────────
#
# Same discipline as ragdash: every row goes through line(), so the glyph column is a single ruler
# down the frame and the number after it lands on the same screen column in every panel. Building
# rows ad hoc is why the storage and memory bars used to start at different offsets.
# 22, not 16: `checkpoint_threshold` is 20 characters and `preserve_insertion_order` is 24, and a
# panel naming the settings that matter must not render them as `checkpoint_thres`.
COL_LABEL, COL_VALUE, COL_BAR = 22, 10, 18


def line(c, label, value="", glyph=None, tail="", lc=None, vc=None):
    # Ellipsis, not a clip: a label cut without a mark reads as the setting's actual name.
    lab = f"{label if len(label) <= COL_LABEL else label[:COL_LABEL - 1] + '…':<{COL_LABEL}}"
    val = value if len(value) <= COL_VALUE else value[:COL_VALUE - 1] + "…"
    g = " " * COL_BAR if glyph is None else glyph
    return (f"{lc or c['dim']}{lab}{c['r']}"
            f"{vc or ''}{val:>{COL_VALUE}}{c['r']}  {g}  {tail}")


def psql(container, port, password, sql, timeout=30):
    """One docker exec, many queries, split on a marker. Returns list-of-rowlists per query."""
    try:
        out = subprocess.run(
            ["docker", "exec", "-e", f"PGPASSWORD={password}", container, "psql",
             "-h", "127.0.0.1", "-p", str(port), "-U", "postgres", "-t", "-A", "-F", "\x01",
             "-c", f"; select '{SEP}'; ".join(sql) + ";"],
            capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    blocks, cur = [], []
    for line in out.stdout.splitlines():
        if line.strip() == SEP:
            blocks.append(cur)
            cur = []
        else:
            cur.append(line.split("\x01"))
    blocks.append(cur)
    return blocks


def to_bytes(s):
    """'22.6 GiB' -> bytes. SereneDB returns pre-formatted sizes, so they must be parsed back to
    compare them — a ratio is the point, and you cannot divide two strings."""
    m = re.match(r"\s*([\d.]+)\s*([KMGT]?i?B|bytes)", str(s), re.I)
    if not m:
        return 0.0
    v, u = float(m.group(1)), m.group(2).upper().rstrip("B").rstrip("I")
    return v * {"": 1, "BYTES": 1, "K": 2**10, "M": 2**20, "G": 2**30, "T": 2**40}.get(u, 1)


def dur(sec):
    """Coarse on purpose: at 3 days the hours matter, at 4 minutes the seconds do not."""
    if not sec:
        return "?"
    sec = int(sec)
    if sec < 120:
        return f"{sec}s"
    if sec >= 86400:
        return f"{sec // 86400}d {sec % 86400 // 3600}h"
    if sec >= 3600:
        return f"{sec // 3600}h {sec % 3600 // 60}m"
    return f"{sec // 60}m"


def human(n):
    for u in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024 or u == "T":
            return f"{n:.0f}{u}" if u == "B" else f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}T"


def sample(c, p, pw):
    b = psql(c, p, pw, [
        "select database_name, database_size, wal_size, memory_usage, memory_limit, "
        "  total_blocks, used_blocks, free_blocks, block_size "
        "from pragma_database_size() where database_name not in ('memory','postgres')",
        "select tag, memory_usage_bytes from duckdb_memory() order by memory_usage_bytes desc",
        # Excluding this very session. It is `active` by construction — it is the one running this
        # query — so counting it made the panel report "1 active" in green directly above "nothing
        # running", which is the same contradiction read two ways. The query list already dropped it;
        # the count did not, and the two disagreeing is worse than either being wrong alone.
        "select coalesce(state,'?'), count(*) from pg_stat_activity "
        "where pid <> pg_backend_pid() group by 1",
        # No `query <> ''` filter: with it the header counted every session and the list showed only
        # the ones carrying statement text, so `6 sessions` sat above four rows with nothing saying
        # where the other two went. A session with no statement is a row that says so.
        "select coalesce(state,'?'), replace(replace(coalesce(query,''),chr(10),' '),chr(13),' ') "
        "from pg_stat_activity where pid <> pg_backend_pid() order by state",
        # Every setting the HAZARDS table has an opinion about, in one go. The panel used to hard-code
        # three of them in the query and show two, so a table built from measured incidents was
        # mostly invisible on the screen that exists to surface those incidents.
        "select name, value from duckdb_settings() where name in ("
        + ", ".join(f"'{n}'" for n in sorted(HAZARDS)) + ")",
    ])
    if b is None or not b[0]:
        return None
    r = b[0][0] if b[0] and len(b[0][0]) >= 9 else [""] * 9
    return {
        "db": r[0], "size": to_bytes(r[1]), "wal": to_bytes(r[2]),
        "mem": to_bytes(r[3]), "memlimit": to_bytes(r[4]),
        "blocks": (int(r[5] or 0), int(r[6] or 0), int(r[7] or 0), int(r[8] or 0)),
        "memtags": [(x[0], int(x[1])) for x in b[1] if len(x) == 2 and x[1].isdigit()],
        "states": {x[0]: int(x[1]) for x in b[2] if len(x) == 2 and x[1].isdigit()},
        "queries": [(x[0], x[1]) for x in b[3] if len(x) == 2],
        "settings": {x[0]: x[1] for x in b[4] if len(x) == 2},
        "t": time.time(),
    }


def hostinfo(pid, container):
    """The facts every other panel quietly assumes, and none of them state.

    A thread at 100% is 100% of ONE core — meaningless without the core count. `threads 24` is the
    store's setting, not how many OS threads exist (107 here, because pools, jemalloc and the wire
    layer all add their own). RSS is the number the OOM killer reads, which is not the number
    `duckdb_memory()` reports. All of it is free: three small reads under /proc.
    """
    d = {"container": container, "pid": pid, "cores": os.cpu_count() or 0, "load": [], "rss": 0,
         "threads": 0, "peak": 0, "swap": 0, "ram_total": 0}
    try:
        with open("/proc/loadavg") as f:
            d["load"] = f.read().split()[:3]
    except OSError:
        pass
    # The machine's own RAM, which is what makes memory_limit readable: 100.2 GiB is DuckDB's
    # default 80% of total, and whether that is generous or reckless depends entirely on what else
    # is running on the box.
    try:
        want = {"MemTotal:": "ram_total", "MemAvailable:": "ram_avail",
                "SwapTotal:": "swap_total", "SwapFree:": "swap_free"}
        with open("/proc/meminfo") as f:
            for ln in f:
                k = ln.split(None, 1)[0] if ln else ""
                if k in want:
                    d[want[k]] = int(ln.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    if pid:
        try:
            d["threads"] = len(os.listdir(f"/proc/{pid}/task"))
        except OSError:
            pass
        # RSS, its high-water mark, and swap. Swap is the one that changes a reading: a store that
        # reports 33.7 GB of buffers while only 7.3 GB is resident has not lost them, it has had
        # them paged out — 29.7 GB of them, measured here — and every touch of that memory is now a
        # disk read that no query plan, cache-hit ratio or memory_limit will show you.
        try:
            want = {"VmRSS:": "rss", "VmHWM:": "peak", "VmSwap:": "swap"}
            with open(f"/proc/{pid}/status") as f:
                for ln in f:
                    k = ln.split(None, 1)[0] if ln else ""
                    if k in want:
                        d[want[k]] = int(ln.split()[1]) * 1024
        except (OSError, ValueError, IndexError):
            pass
        # Field 22 of /proc/<pid>/stat is start time in ticks since boot; /proc/uptime is the other
        # half of the subtraction. Worth having: "the WAL has not checkpointed in two days" reads
        # differently against a process that started an hour ago.
        try:
            with open(f"/proc/{pid}/stat") as f:
                start = int(f.read().rsplit(") ", 1)[1].split()[19])
            with open("/proc/uptime") as f:
                d["uptime"] = max(0.0, float(f.read().split()[0]) - start / os.sysconf("SC_CLK_TCK"))
        except (OSError, ValueError, IndexError):
            pass
    return d


def du(container, path):
    try:
        o = subprocess.run(["docker", "exec", container, "du", "-sm", path],
                           capture_output=True, text=True, timeout=240)
        return int(o.stdout.split()[0]) * 2**20
    except Exception:                                           # noqa: BLE001
        return None


def temp_files(container, temp_dir):
    """(mtime, size) for every file in the temp directory.

    Enough to split the directory into what a live query is using and what a dead one left behind,
    which are two different numbers that must not be added together. DuckDB deletes duckdb_temp_*
    files in TemporaryDirectoryHandle's DESTRUCTOR and nowhere else — there is no sweep at startup —
    so anything a killed server leaves survives every later run, forever. A file older than the
    process cannot belong to it: 73 GB sat in this one, two hours older than the serened holding it.

    Cheap because the directory holds tens of files, not thousands — one stat per file, on the du
    timer, not the refresh timer.
    """
    try:
        o = subprocess.run(
            ["docker", "exec", container, "sh", "-c",
             f"find {temp_dir} -maxdepth 1 -type f -printf '%T@ %s %f\\n' 2>/dev/null"],
            capture_output=True, text=True, timeout=60)
        out = []
        for ln in o.stdout.split("\n"):
            p = ln.split(None, 2)
            if len(p) == 3:
                out.append((float(p[0]), int(p[1]), p[2]))
        return out
    except Exception:                                           # noqa: BLE001
        return []


def slow(container, data_dir, prev=None):
    """Directory sizes, plus how the temp directory MOVED since the last measurement.

    Size is a level; spilling is an activity. A killed query leaves its temp files behind, so a
    directory that has sat at 72 GB since yesterday reads identically to one being written to right
    now — and the level alone was reported as "spilling", which is wrong in exactly the way a
    momentary thread state reported as an interval verdict is wrong. Only the delta can say it.
    """
    cur = {
        "index": du(container, f"{data_dir}/engine_search"),
        "duck": du(container, f"{data_dir}/engine_duckdb"),
        "temp": du(container, f"{data_dir}/tmp"),
        "total": du(container, data_dir),
    }
    cur["temp_files"] = temp_files(container, f"{data_dir}/tmp")
    was = (prev or {}).get("temp")
    cur["temp_d"] = None if was is None or cur["temp"] is None else cur["temp"] - was
    # How long that delta actually covers. "since last check" left the reader to work out that a
    # check is SLOW_EVERY ticks, which is 60s at the default -n 5 and 24s at -n 2.
    cur["t"] = time.time()
    cur["dt"] = cur["t"] - prev["t"] if prev and prev.get("t") else None
    return cur


def perf_window(perf_dir, keep=4, cache={}):
    """Top symbols across the newest `keep` perf.data files, as one weighted view.

    ## Why it reads files instead of recording

    `perf_event_paranoid` is 4 and there is no passwordless sudo here, so a dashboard cannot attach
    to a container process on its own. Rather than demand the whole tool run as root, it consumes
    what `perf-snap.sh` already produces under sudo. The dashboard stays unprivileged; the thing that
    needs privilege stays where it already was.

    ## Sliding window, not one capture

    A single 100-second capture is a keyhole. Averaging the last few gives a view that survives one
    unrepresentative window, and because captures are named with the phase signature that triggered
    them, a phase change shows up as the window's composition shifting rather than as a number
    jumping.

    ## Incremental

    Parsing a capture costs real time — symbol resolution re-reads the 1.7 GB debug binary, so a
    600 KB file takes about a second and a large one far longer. Results are cached by (path, mtime)
    and only new files are parsed, so a tick that finds nothing new costs a directory listing.
    """
    try:
        files = sorted(
            (os.path.join(dp, f) for dp, _, fs in os.walk(perf_dir) for f in fs
             if f.endswith(".data")),
            key=lambda p: os.path.getmtime(p), reverse=True)[:keep]
    except OSError:
        return None, [], {}
    if not files:
        return None, [], {}

    agg, newest, by_tid = {}, None, {}
    for p in files:
        try:
            key = (p, os.path.getmtime(p))
        except OSError:
            continue
        newest = newest or os.path.basename(p)
        if key not in cache:
            # -F overhead,pid,symbol: `--sort symbol` still emits the event's other columns padded
            # per-section with '-' placeholders, so the same symbol arrives as two different strings
            # and splits in two. And a hybrid CPU reports one table PER PMU, so reading only the
            # first gives the E-core view — the minority of the cycles.
            try:
                # `pid` in perf's vocabulary is the tid, which is what /proc's task dir is keyed by,
                # so the same parse that feeds the profile panel also answers "what is THIS thread
                # doing" — the threads panel cannot get that from /proc at any price. Anchoring the
                # symbol group on the [.]/[k] marker keeps a command name with a space in it from
                # eating the symbol and silently dropping the row's cycles from the aggregate.
                out = subprocess.run(
                    ["perf", "report", "-i", p, "--stdio", "-F", "overhead,pid,symbol",
                     "--no-children", "--no-inline", "-g", "none", "--percentage", "absolute"],
                    capture_output=True, text=True, timeout=120)
                rows, per_tid, ec, tot = {}, {}, 0.0, 0.0
                for ln in out.stdout.splitlines():
                    m = re.match(r"^# Event count \(approx\.\): (\d+)", ln)
                    if m:
                        ec = float(m.group(1))
                        tot += ec
                        continue
                    m = re.match(r"^\s+([\d.]+)%\s+(\d+):.*?(\[[.k]\]\s.*?)\s*$", ln)
                    if m and ec:
                        pct, tid, sym = float(m.group(1)), m.group(2), m.group(3)
                        rows[sym] = rows.get(sym, 0.0) + pct * ec
                        if pct > per_tid.get(tid, ("", 0.0))[1]:
                            per_tid[tid] = (sym, pct)
                cache[key] = {"syms": {k: v / tot for k, v in rows.items()} if tot else {},
                              "tids": per_tid}
            except Exception:                                   # noqa: BLE001
                cache[key] = {"syms": {}, "tids": {}}
        for sym, pct in cache[key]["syms"].items():
            agg[sym] = agg.get(sym, 0.0) + pct / len(files)
        # Symbols average over the window; the tid map merges across it, newest capture winning per
        # tid (files are newest-first, so setdefault does that). Taking one capture's map made the
        # labels flap: while perf-snap runs, the newest .data is the one it is writing THIS second,
        # which parses to nothing, so every label blanked for as long as the profiler kept running —
        # exactly when they are wanted. Only tids that are live in /proc are ever looked up, so a
        # recycled id carrying a stale symbol is the bounded cost.
        for t, v in cache[key]["tids"].items():
            by_tid.setdefault(t, v)
    if len(cache) > 32:
        for k in list(cache)[:-32]:
            cache.pop(k, None)
    # Every symbol, not the top few. The engine split is only meaningful over the whole profile —
    # computed over the handful that fit on screen it answers "which of these six is biggest", which
    # is a different and far less useful question. The renderer slices for display.
    return newest, sorted(agg.items(), key=lambda kv: -kv[1]), by_tid



def host_pid(container):
    """The serened pid as the HOST sees it. /proc is the only way to per-thread detail — the server
    exposes sessions, not threads, and a spin lives in a thread that owns no session."""
    try:
        o = subprocess.run(["docker", "inspect", "-f", "{{.State.Pid}}", container],
                           capture_output=True, text=True, timeout=10)
        pid = int(o.stdout.strip())
        return pid if pid > 0 else None
    except Exception:                                           # noqa: BLE001
        return None


def threads(pid, prev, prev_t):
    """Per-thread CPU and state, newest first by CPU.

    Threads, not the process total. 100% of one core out of 24 reads as "4% busy" at process level
    and as a pinned thread here — and that difference is the whole diagnosis when a single thread
    spins.

    `pct` is a delta over the whole refresh interval; `st` is a single instantaneous sample taken
    at the end of it. The two are not comparable, and treating `st` as a verdict on the interval is
    wrong: a thread at 60% duty is off-CPU 40% of the time, so the one read lands on `S` about two
    times in five. Only `D` — blocked in the kernel, which `pct` cannot show — is worth reporting
    from `st`. Everything else the renderer takes from `pct`.
    """
    out, cur = [], {}
    # comm defaults to the process name for any thread that never called pthread_setname_np, and
    # DuckDB's scheduler pool never does — 103 of serened's 107 threads report "serened", which
    # names nothing. jemalloc does name its own, so a comm that differs from the process's is real
    # information and is kept; one that matches is dropped for the tid, which at least identifies
    # the row and is the key perf attributes samples to.
    try:
        with open(f"/proc/{pid}/comm") as f:
            pcomm = f.read().strip()
    except OSError:
        pcomm = ""
    # Read everything first, then stamp the clock. dt used to be measured in the caller, around a
    # loop body that also parses perf captures and shells out to du — work whose duration swings by
    # hundreds of milliseconds between ticks. The ticks counted always spanned read-to-read, so any
    # swing landed straight in the divisor and printed a thread at 110.6% of a core, which is not a
    # thing that can happen. Stamping immediately after the reads makes the divisor the interval the
    # counters actually cover.
    seen = {}
    try:
        for t in os.scandir(f"/proc/{pid}/task"):
            try:
                with open(f"{t.path}/stat") as f:
                    fields = f.read().rsplit(") ", 1)
                comm = fields[0].split("(", 1)[1]
                rest = fields[1].split()
                seen[t.name] = (int(rest[11]) + int(rest[12]), rest[0], comm)
            except (OSError, IndexError, ValueError):
                continue
    except OSError:
        return [], cur, prev_t
    now = time.time()
    dt = now - prev_t if prev_t else 0.0
    total = 0.0
    for tid, (ticks, st, comm) in seen.items():
        cur[tid] = ticks
        if prev and tid in prev and dt > 0:
            # Clamped at one core: the remaining error is sub-tick quantisation, and a bar that
            # overfills its own scale is a worse lie than a rounded 100.0%.
            pct = min(100.0, (ticks - prev[tid]) / os.sysconf("SC_CLK_TCK") / dt * 100)
            # Summed over EVERY thread, before the display filter. `top` says serened is at 300% and
            # the rows say 8%, 6%, 6% — both true, three cores spread over a hundred threads, and
            # with no total on screen the panel looked like it was missing the work.
            total += pct
            if pct > 1:
                name = (comm[:18] if comm != pcomm else
                        "main" if tid == str(pid) else f"tid {tid}")
                out.append((pct, name, st, tid))
    # Every thread that cleared the filter, not the top 32: the main panel slices to what fits, and
    # the `t` view wants the rest. A cap here would have silently limited both.
    return sorted(out, reverse=True), total, cur, now


def callstacks(perf_dir, limit=40, cache={}):
    """Caller-oriented call graph from the newest capture that has one.

    Flat symbol lists say WHAT is hot; they cannot say what led into it. That distinction is what
    separated a spinning COPY feeder from a spinning recv loop when both showed the same leaf.

    ## --no-inline, and why the view used to hang

    Measured on a 577 KB capture against this build: 81.06s with inline expansion, 0.30s without —
    270x, because perf runs addr2line over the 1.8 GB binary for every frame in every chain. The
    inline frames are not worth a view that cannot be opened. This ran unmemoised on every tick, so
    the cost was paid again each refresh, and with a 180s timeout the dashboard simply stopped.

    ## Not simply the newest file

    While perf-snap is recording, the newest .data is the one being written and parses to nothing —
    which is exactly when someone opens this view. Fall through to the next newest that has content.
    """
    try:
        files = sorted((os.path.join(dp, f) for dp, _, fs in os.walk(perf_dir) for f in fs
                        if f.endswith(".data")), key=os.path.getmtime, reverse=True)[:4]
    except OSError:
        return None, []
    for p in files:
        try:
            key = (p, os.path.getmtime(p))
        except OSError:
            continue
        if key not in cache:
            try:
                o = subprocess.run(["perf", "report", "-i", p, "--stdio", "--sort", "symbol",
                                    "--no-inline", "-g", "graph,0.5,caller"],
                                   capture_output=True, text=True, timeout=180)
                cache[key] = [ln.rstrip() for ln in o.stdout.splitlines()
                              if ln.strip() and not ln.startswith("#")]
            except Exception:                                   # noqa: BLE001
                cache[key] = []
            for k in list(cache)[:-8]:
                cache.pop(k, None)
        if cache[key]:
            return os.path.basename(p), cache[key][:limit]
    return (os.path.basename(files[0]) if files else None), []


def spark(v, w=HIST, top=None):
    v = [x for x in v[-w:] if x is not None]
    if len(v) < 2:
        return "·" * max(1, len(v))
    # With `top` given, every trace on the panel is drawn against the same ceiling, so their heights
    # can be read against each other. Self-scaling made a 260 MB pool that never moves render as a
    # full-height line beside a 34 GB one — each series stretched to its own min and max, which says
    # something about that series' variance and nothing about its size.
    if top:
        # Nothing to draw for a series that is flat at zero. The self-scaling branch below renders a
        # flat series as a mid-height bar on every sample, which turned thirteen empty pools into
        # thirteen identical stripes — ink that reads as activity and means the opposite.
        if not any(v):
            return ""
        return "".join(SPARK[min(7, int(max(0.0, x) / top * 7.99))] for x in v)
    lo, hi = min(v), max(v)
    if hi - lo < 1e-9:
        return SPARK[3] * len(v)
    return "".join(SPARK[min(7, int((x - lo) / (hi - lo) * 7.99))] for x in v)


def bar(frac, width, col):
    f = max(0.0, min(1.0, frac))
    k = int(round(f * width))
    return f"{col}{'█' * k}{C['dim'] if col else ''}{'░' * (width - k)}{C['r'] if col else ''}"


def strip(s):
    out, i = [], 0
    while i < len(s):
        if s[i] == "\033":
            while i < len(s) and s[i] != "m":
                i += 1
            i += 1
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def clip(s, n):
    """Truncate to n VISIBLE columns, keeping escapes whole and closing any colour left open.

    A raw `s[:n]` counts the bytes of every colour escape against the width, so a row carrying four
    of them loses about twenty real characters — which is why the engines line lost the capture name
    it was supposed to end with and cut mid-word at "ker…". Worse, the slice can land inside an
    escape, emitting a partial sequence that swallows the box border that follows it.
    """
    out, vis, i = [], 0, 0
    while i < len(s) and vis < n:
        if s[i] == "\033":
            j = i
            while j < len(s) and s[j] != "m":
                j += 1
            out.append(s[i:j + 1])
            i = j + 1
        else:
            out.append(s[i])
            vis += 1
            i += 1
    # Absorb any escapes left at the cut. They carry no width, so a string whose visible length
    # exactly equals n is NOT truncated just because its trailing reset has not been consumed —
    # appending "…" there added a real column, which is why the one memory row whose note filled the
    # field sat one cell right of every other row, and only when colour was on.
    while i < len(s) and s[i] == "\033":
        j = i
        while j < len(s) and s[j] != "m":
            j += 1
        out.append(s[i:j + 1])
        i = j + 1
    # The reset closes whatever colour the cut landed inside, but only if there was colour to close:
    # appending the global palette's escape unconditionally printed a literal "…[0m" under --no-color.
    return "".join(out) + ("…" + (C["r"] if "\033" in s else "") if i < len(s) else "")


# ── what every label and number on the screen means ─────────────────────────────────────────────
#
# A panel that needs an explanation and does not carry one is a panel that gets misread — "spilling"
# was read off a directory that had not grown in a day, and "sleeping" off a thread at 60% of a core.
# Both were the display's fault. This is the reference for the vocabulary that survived, and it is
# kept next to nothing else so it cannot drift from the renderer without being obvious.
LEGEND = (
    ("storage", (
        ("database", "logical size the store reports for its own data, not bytes on disk"),
        ("blocks", "used of total, at the store's block size; free blocks are reusable, not returned"),
        ("wal  N.NNx", "WAL size, and WAL as a multiple of the database. Several times over means "
                       "checkpoints are not completing — look for write errors, not for tuning"),
        ("columnar / search idx / spill", "du of the store's own directories"),
        ("N% of X on disk", "that directory's share of the whole data directory. The three shares "
                            "add to 100; `database` above is a different measure and will not match"),
        ("orphaned", "temp files older than the serened process holding them, so none can belong to "
                     "a live query and none are counted in `spill` above. DuckDB deletes temp files "
                     "only in TemporaryDirectoryHandle's destructor and never sweeps at startup, so "
                     "a server that is killed leaks the lot and no later run reclaims it. The size "
                     "shown is free to delete while the server runs: nothing holds a descriptor "
                     "into that directory. rm /var/lib/serenedb/tmp/duckdb_temp_storage_*.tmp"),
        ("spilling +X/T", "the temp directory GREW by X over the last T — a query is spilling now"),
        ("draining -X/T", "it shrank; spills are being reclaimed"),
        ("flat T", "it did not move over T. Files are sitting there, which is not the same as "
                   "spilling; du cannot tell held-open from abandoned. T is the gap between du "
                   "runs, which happen every SLOW_EVERY ticks and so follow -n: 60s at the default "
                   "5s refresh, 24s at -n 2"),
    )),
    ("memory", (
        ("in use", "duckdb_memory() total against memory_limit; the sparkline is its recent history"),
        ("resident", "the process's RSS, and how far it sits from duckdb_memory(). Over means "
                     "allocator arenas, the wire layer and code on top of the store's own buffers; "
                     "under means the store counts memory that is not resident. RSS is the number "
                     "the OOM killer reads"),
        ("headroom", "memory_limit minus what is in use — what is left before queries spill"),
        ("swapped", "how much of the process the kernel has paged out to swap. Every touch of it is "
                    "a disk read, memory_limit does not count it, and it is the usual reason "
                    "`resident` sits below duckdb_memory()"),
        ("peak", "high-water RSS since serened started — what it has held at its worst, which the "
                 "current figure will not tell you"),
        ("tags", "per-tag breakdown, largest first. Tags under 1% are counted, not listed"),
    )),
    ("activity", (
        ("sessions", "connected sessions by state, EXCLUDING this dashboard's own session"),
        ("▸ / ·", "active / idle. The text is the statement the server reports for that session"),
        ("nothing running", "no session is executing. A pinned core with this showing means work "
                            "with no session behind it — an orphaned server-side task"),
    )),
    ("threads", (
        ("tid N", "kernel thread id. Threads inherit the process name unless they set their own, so "
                  "the id is what actually identifies a row; a real name is shown when there is one"),
        ("N%", "share of ONE core over the last refresh, from utime+stime deltas. 100% is one core "
               "fully held, not the machine — 24 cores means 2400% is available"),
        ("pinned", "held a whole core for the entire interval"),
        ("io wait", "sampled in uninterruptible kernel I/O (D). The only thing the instantaneous "
                    "state adds that the percentage cannot"),
        ("symbol", "what that thread was executing in the perf capture, matched by tid. From the "
                   "capture, so it lags the percentage beside it"),
    )),
    ("profile", (
        ("title", "the capture these numbers came from"),
        ("engines", "share of sampled cycles per subsystem, over the WHOLE profile. Shares sum to "
                    "100; the rows below are individual symbols against the same total, so a flat "
                    "profile shows big engine shares over small per-symbol ones"),
        ("symbols unresolved", "perf could not name the addresses. Register the matching binary: "
                               "perf buildid-cache --add <serened>. Needed again after every rebuild"),
    )),
    ("host", (
        ("cores", "cores the machine has, with 1/5/15-minute load. A thread panel row is a share of "
                  "ONE of these"),
        ("os threads", "threads the process actually has, which is not the `threads` setting — pools, "
                       "jemalloc and the wire layer each add their own"),
        ("uptime", "how long serened has been up. A WAL that has not checkpointed for two days means "
                   "nothing if the process started an hour ago"),
        ("serened", "host-side pid, and the container it is in. This is the pid perf attaches to"),
    )),
    ("config", (
        ("temp_directory", "where spills go. A RELATIVE path cannot be created from the server's "
                           "root-owned cwd, so every spill fails with EACCES — silently, until the "
                           "working set first exceeds memory_limit"),
        ("threads", "the store's parallelism setting; it also multiplies per-thread memory"),
        ("red row", "the setting's predicate fired against this server — that text is a finding, "
                    "not a description. Otherwise the row carries why the setting is watched"),
    )),
)

# One key per panel, named after the panel it opens, plus g for the call graph and l for the
# legend. Every view is a toggle: the same key returns to the main frame, as does Esc.
DETAIL = {"storage": "s", "memory": "m", "activity": "a", "threads": "t", "profile": "p",
          "host": "h", "legend": "l"}
# No j/k here: nothing on the main frame scrolls, so it is carried by the views that do scroll and
# the bar gets its width back. Eleven labelled keys need ~100 columns and wrapped onto a second line
# on a 96-column terminal.
KEYS = (("q", "quit"), ("s", "storage"), ("m", "memory"), ("a", "activity"), ("t", "threads"),
        ("p", "profile"), ("g", "graph"), ("c", "config"), ("h", "host"), ("l", "legend"))


def status(c, width, extra=""):
    """The key bar, as however many lines it needs. The bindings used to hide in the config panel.

    Wrapped rather than clipped: with a view behind every panel there are ten of them, which do not
    fit one line of a 100-column terminal, and a binding you cannot see is a binding you do not have.
    Returns a list so the caller can count the rows it costs — clipping silently cost the last few
    keys, and the single line was also one column too wide for its own frame.
    """
    items = ([f"{c['b']}{k}{c['r']} {c['dim']}{v}{c['r']}" for k, v in KEYS]
             + ([extra] if extra else []))
    W = max(20, width)
    rows, cur, used = [], [], 0
    for it in items:
        n = len(strip(it))
        # Packing gap of 1: this only decides how many keys fit on a line. The justification below
        # gives the space back, so a wide terminal still gets an airy bar and a narrow one keeps all
        # the keys on one line instead of wrapping a single item onto its own row.
        if cur and used + len(cur) + n > W - 2:
            rows.append(cur)
            cur, used = [], 0
        cur.append(it)
        used += n
    if cur:
        rows.append(cur)
    # Justified rather than packed left: the bar spans the frame it sits under, and the keys land on
    # stable columns instead of bunching at one end with a ragged hole after them.
    out = []
    for row in rows:
        vis = sum(len(strip(i)) for i in row)
        gaps = len(row) - 1
        if gaps <= 0:
            out.append(" " + row[0])
            continue
        base, spare = divmod(max(gaps, W - 2 - vis), gaps)
        out.append(" " + "".join(i + (" " * (base + (1 if n < spare else 0)) if n < gaps else "")
                                 for n, i in enumerate(row)))
    return out


def legend_frame(col, width, scroll):
    c = C if col else NOCOLOR
    W = max(70, width)
    out = [f"{c['b']}legend{c['r']}  {c['dim']}what every label and number on the main screen "
           f"means{c['r']}", ""]
    for section, items in LEGEND:
        out.append(f"{c['cyn']}{c['b']}{section}{c['r']}")
        for term, meaning in items:
            # 24, because `columnar / search idx` and `flat since last check` are 21 — a legend that
            # truncates the very labels it exists to explain is not one.
            t = term if len(term) <= 24 else term[:23] + "…"
            wrapped = textwrap.wrap(meaning, max(30, W - 30)) or [""]
            out.append(f"  {c['yel']}{t:<24}{c['r']}  {c['dim']}{wrapped[0]}{c['r']}")
            for cont in wrapped[1:]:
                out.append(f"  {' ' * 24}  {c['dim']}{cont}{c['r']}")
        out.append("")
    return out[scroll:]


def storage_frame(s, sz, host, col, width, scroll):
    """The `s` view: where the bytes on disk actually are, file by file for the temp directory.

    The main panel has room for a size and a verdict. This has room for the evidence behind the
    verdict — every temp file with its age, so "orphaned" is something you can check rather than
    something the dashboard asserts.
    """
    c = C if col else NOCOLOR
    W = max(70, width)
    tot = (sz or {}).get("total") or 1
    started = time.time() - host["uptime"] if host.get("uptime") else None
    files = sorted((sz or {}).get("temp_files") or [], key=lambda f: -f[1])
    orph = [f for f in files if started and f[0] < started]
    out = [f"{c['b']}storage{c['r']}  {c['dim']}{human(tot)} on disk · "
           f"{human(s['size'])} database · {human(s['wal'])} WAL{c['r']}", ""]
    tb, ub, fb, bs = s["blocks"]
    if tb:
        out.append(line(c, "blocks", f"{ub:,}", bar(ub / tb, COL_BAR, c["blu"] if col else ""),
                        f"{c['dim']}of {tb:,} used, {fb:,} free, {human(bs)} each{c['r']}"))
    for k, label, what in (("duck", "columnar", "engine_duckdb — column store"),
                           ("index", "search idx", "engine_search — inverted and vector indexes"),
                           ("temp", "tmp", "temp_directory — spill files")):
        v = (sz or {}).get(k)
        if v is not None:
            out.append(line(c, label, human(v), bar(v / tot, COL_BAR, c["blu"] if col else ""),
                            f"{c['dim']}{v / tot * 100:5.1f}%  {what}{c['r']}"))
    out += ["", f"{c['cyn']}{c['b']}temp files{c['r']}  "
            + (f"{c['dim']}{len(files)} files, {len(orph)} older than this serened "
               f"({human(sum(f[1] for f in orph))} reclaimable){c['r']}" if files
               else f"{c['dim']}none — nothing has spilled{c['r']}")]
    for mtime, size, name in files[:64]:
        old = started and mtime < started
        age = dur(time.time() - mtime)
        out.append(line(c, "", human(size), " " * COL_BAR,
                        f"{c['yel'] if old else c['grn']}{'orphan' if old else 'live  '}{c['r']}  "
                        f"{c['dim']}{age:>8} old  {name[:52]}{c['r']}"))
    return out[scroll:]


def activity_frame(s, col, width, scroll):
    """The `a` view: every session and its whole statement, not the first 90 characters."""
    c = C if col else NOCOLOR
    W = max(70, width)
    st = s["states"]
    rows = [(stt, q) for stt, q in s["queries"] if "pg_stat_activity" not in q]
    out = [f"{c['b']}activity{c['r']}  {c['dim']}"
           + "  ".join(f"{k} {v}" for k, v in sorted(st.items())) + f"{c['r']}", ""]
    if not rows:
        out.append(f"{c['dim']}no sessions{c['r']}")
    for stt, q in rows:
        run = stt == "active"
        out.append(f"{(c['grn'] + '▸') if run else (c['dim'] + '·')} {stt}{c['r']}")
        # Wrapped, not truncated: the interesting part of a statement is rarely in its first line,
        # and the main panel already shows the head of it.
        for chunk in (textwrap.wrap(q, max(30, W - 4)) or ["(no statement)"]):
            out.append(f"    {'' if run else c['dim']}{chunk}{c['r']}")
        out.append("")
    return out[scroll:]


def threads_frame(thr, tcpu, by_tid, host, col, width, scroll):
    """The `t` view: every thread over the noise floor, not just the ones that fit."""
    c = C if col else NOCOLOR
    W = max(70, width)
    cores = host.get("cores") or 1
    out = [f"{c['b']}threads{c['r']}  {c['dim']}{tcpu:.0f}% of {cores * 100}% "
           f"({tcpu / 100:.1f} of {cores} cores) · {host.get('threads', '?')} threads exist · "
           f"{len(thr)} over 1% of a core{c['r']}", ""]
    for pct, name, stt, tid in thr:
        sym = by_tid.get(tid)
        # Same two words as the main panel. The raw state letter is a single instantaneous sample
        # and says nothing about the interval the percentage covers; printing it here would only
        # relitigate that in a view with more room.
        tag = (f"{c['yel']}io wait{c['r']}" if stt == "D" else
               f"{c['grn']}pinned{c['r']}" if pct >= 90 else "")
        nm = re.sub(r"^\[[.k]\]\s*", "", sym[0]) if sym else ""
        out.append(line(c, name, f"{pct:.1f}%",
                        bar(pct / 100, COL_BAR,
                            (c["grn"] if pct >= 90 else c["blu"]) if col else ""),
                        f"{tag:<18}{c['dim']}{nm[:max(20, W - 70)]}{c['r']}",
                        vc=c["b"] if pct >= 90 else None))
    if not thr:
        out.append(f"{c['dim']}nothing over 1% of a core{c['r']}")
    return out[scroll:]


def profile_frame(perf, col, width, scroll):
    """The `p` view: the whole symbol table behind the profile panel, grouped by engine."""
    c = C if col else NOCOLOR
    W = max(70, width)
    newest, tops, _ = perf
    fam = {}
    for sym, pct in tops:
        fam.setdefault(kernel_of(sym), []).append((sym, pct))
    order = sorted(fam.items(), key=lambda kv: -sum(p for _, p in kv[1]))
    tot = sum(p for _, ps in fam.items() for _, p in ps) or 1
    fcol = {"vector": c["mag"], "text": c["yel"], "columnar": c["cyn"],
            "wire": c["red"], "alloc": c["dim"], "kernel": c["blu"]}
    out = [f"{c['b']}profile{c['r']}  {c['dim']}{newest or 'no captures'} · {len(tops)} symbols · "
           f"{len(fam)} engines{c['r']}", ""]
    if not tops:
        out.append(f"{c['dim']}no captures — sudo ./perf-snap.sh --name serened{c['r']}")
    top1 = tops[0][1] if tops else 1
    for k, syms in order:
        share = sum(p for _, p in syms) / tot * 100
        out += ["", f"{fcol.get(k, '')}{c['b']}{k}{c['r']}  "
                f"{c['dim']}{share:.0f}% of sampled cycles, {len(syms)} symbols{c['r']}"]
        for sym, pct in syms[:20]:
            out.append(line(c, k, f"{pct:.2f}%",
                            bar(pct / (top1 or 1), COL_BAR, fcol.get(k, "") if col else ""),
                            f"{c['dim']}{re.sub(r'^\[[.k]\]\s*', '', sym)[:max(20, W - 56)]}{c['r']}",
                            lc=fcol.get(k)))
    return out[scroll:]


def host_frame(host, s, col, width, scroll):
    """The `h` view: the machine, which is the context every other number is read against."""
    c = C if col else NOCOLOR
    ram, avail = host.get("ram_total") or 0, host.get("ram_avail") or 0
    swt, swf = host.get("swap_total") or 0, host.get("swap_free") or 0
    lim = s["memlimit"] or 0
    out = [f"{c['b']}host{c['r']}  {c['dim']}{host.get('container', '?')} · pid "
           f"{host.get('pid', '?')} · up {dur(host.get('uptime'))}{c['r']}", ""]
    rows = [("cores", str(host.get("cores") or "?"), "load " + " ".join(host.get("load") or ["?"])
             + "  1/5/15m"),
            ("os threads", str(host.get("threads") or "?"), "threads the process holds"),
            ("RAM", human(ram), f"{human(avail)} available to the machine"),
            ("swap", human(swt), f"{human(swt - swf)} of it in use system-wide"),
            ("memory_limit", human(lim),
             f"{lim / ram * 100:.0f}% of RAM (DuckDB's default heuristic)" if ram else ""),
            ("resident", human(host.get("rss") or 0), "serened's share of RAM right now"),
            ("swapped", human(host.get("swap") or 0), "serened's share of the swap in use")]
    for label, val, note in rows:
        out.append(line(c, label, val, " " * COL_BAR, f"{c['dim']}{note}{c['r']}"))
    # The one comparison worth making on this screen: a limit the machine cannot honour is how a
    # store ends up with two thirds of itself on disk while still believing it is under its ceiling.
    if ram and lim > ram * 0.75:
        out += ["", f"{c['yel']}memory_limit is {lim / ram * 100:.0f}% of a machine that is also "
                f"running everything else{c['r']}"]
    return out[scroll:]


def memory_frame(s, hist, host, col, width, scroll):
    """The `m` view: every pool duckdb_memory() reports, with its history.

    The main panel shows the pools that are holding something and counts the rest, which is the
    right trade for six rows — but "3 more" with no way to reach them is a dead end. All sixteen
    live here, zeros included and dimmed, because a pool sitting at zero is information: HASH_TABLE
    and ORDER_BY at zero mean nothing is joining or sorting, and those are exactly the pools whose
    climb precedes a spill.
    """
    c = C if col else NOCOLOR
    W = max(70, width)
    tags = sorted(s["memtags"], key=lambda kv: -kv[1])
    tot = sum(v for _, v in tags) or 1
    top = max((v for _, v in tags), default=1) or 1
    room = max(8, W - (COL_LABEL + COL_VALUE + COL_BAR + 6) - 14)
    ramt = host.get("ram_total") or 0
    # Each trace shares its own row's bar denominator — memory_limit for the process rows, the
    # largest pool for the pools. A panel-wide ceiling put a full-height trace next to a bar at a
    # third, which is two different answers to the same question on one line.
    ptop = max(s["memlimit"] or 1, 1)
    out = [f"{c['b']}memory{c['r']}  {c['dim']}{len(tags)} pools · "
           f"{human(s['mem'])} in use of {human(s['memlimit'])} memory_limit"
           + (f" · {human(ramt)} RAM on the host" if ramt else "") + f"{c['r']}", ""]
    for name, val, note, bcol in (
            ("in use", s["mem"], "of memory_limit", c["mag"]),
            ("resident", host.get("rss") or 0, "RSS, in RAM now", c["grn"]),
            ("swapped", host.get("swap") or 0, "paged out to disk", c["red"]),
            ("peak", host.get("peak") or 0, "high-water RSS", c["dim"])):
        key = {"in use": "mem", "resident": "rss", "swapped": "swap"}.get(name)
        h = (hist.get(key) or [])[-room:] if key else []
        out.append(line(c, name, human(val),
                        bar(val / max(s["memlimit"] or 1, 1), COL_BAR, bcol if col else ""),
                        f"{c['dim']}{note:<20}{c['r']}{bcol}"
                        f"{spark(h, top=ptop) if h else ''}{c['r']}",
                        vc=c["b"]))
    out += ["", f"{c['cyn']}{c['b']}pools{c['r']}  {c['dim']}duckdb_memory(), largest first{c['r']}"]
    for tag, v in tags:
        h = (hist.get(f"t:{tag}") or [])[-room:]
        lc = None if v else c["dim"]
        out.append(line(c, tag, human(v), bar(v / top, COL_BAR, c["cyn"] if col and v else ""),
                        f"{c['dim']}{f'{v / tot * 100:.1f}% of tagged':<20}{c['r']}"
                        f"{c['cyn'] if v else c['dim']}"
                        f"{spark(h, top=top) if h else ''}{c['r']}",
                        lc=lc, vc=lc))
    return out[scroll:]


def frame(s, prev, sz, hist, perf, thr, tcpu, host, col, width, height=40):
    c = C if col else NOCOLOR
    newest, tops, by_tid = perf
    # The whole terminal, both ways. The old 120-column cap threw away every column past it while
    # the panels below truncated SQL at 92 and symbols at 52 — three different limits, none of them
    # the screen's, which is why sections cut at visibly different places on the same line width.
    # One inner width, and every truncation derived from it: TAIL for rows that carry the bar grid,
    # WIDE for rows that use the full line.
    W = max(70, width)
    inner = W - 2
    TAIL = max(24, inner - (COL_LABEL + COL_VALUE + COL_BAR + 6))
    WIDE = max(24, inner - 3)
    # Decided here rather than at layout time because the paired panels build their rows against
    # their own half-width, not the frame's: a sparkline sized for the full line would be clipped
    # to a stump inside a box half that wide.
    half = (W - 1) // 2 - 2
    pair = half >= COL_LABEL + COL_VALUE + COL_BAR + 20
    PTAIL = max(16, (half if pair else inner) - (COL_LABEL + COL_VALUE + COL_BAR + 6))
    L = []

    def mkbox(title, rows, accent="cyn", iw=None):
        """One box as a list of lines, at an arbitrary inner width — so boxes can go side by side."""
        iw = inner if iw is None else iw
        t = f"{c[accent]}{c['b']}{title}{c['r']}" if col else title
        out = [f"{c['dim']}┌─{c['r']}{t}{c['dim']} " + "─" * max(0, iw - len(title) - 2) + f"┐{c['r']}"]
        for r in rows:
            vis = len(strip(r))
            if vis > iw - 1:
                r, vis = clip(r, iw - 2), iw - 1
            out.append(f"{c['dim']}│{c['r']} {r}" + " " * max(0, iw - vis - 1) + f"{c['dim']}│{c['r']}")
        out.append(f"{c['dim']}└" + "─" * iw + f"┘{c['r']}")
        return out

    def beside(lt, lr, la, rt, rr, ra, lw):
        """Two boxes side by side, bottom borders on the same line, right edge flush with the frame.

        The rows are padded to equal count BEFORE drawing, not the finished lines afterwards — pad
        the lines and the shorter box closes early with empty space hanging under it, which reads as
        two panels of different heights rather than one row of two.

        The halves are not equal. Two boxes of the same inner width plus the gap come to W - 1 on an
        even terminal, leaving the paired rows a column short of the full-width panels above and
        below them. The right box absorbs the remainder so every right border lands on one column.
        """
        rw = W - lw - 5
        n = max(len(lr), len(rr))
        left = mkbox(lt, lr + [""] * (n - len(lr)), la, lw)
        right = mkbox(rt, rr + [""] * (n - len(rr)), ra, rw)
        return [a + " " + b for a, b in zip(left, right)]

    def box(title, rows, accent="cyn"):
        L.extend(mkbox(title, rows, accent))

    # ── storage ─────────────────────────────────────────────────────────────────────────────────
    ratio = s["wal"] / s["size"] if s["size"] else 0
    wcol = c["red"] if ratio > 1 else (c["yel"] if ratio > 0.3 else c["grn"])
    rows = [line(c, "database", human(s["size"]), " " * COL_BAR,
                 f"{c['dim']}wal{c['r']} {wcol}{human(s['wal'])}{c['r']}  "
                 f"{wcol}{ratio:.2f}x{c['r']} "
                 + (f"{c['red']}checkpoint not completing{c['r']}" if ratio > 1
                    else f"{c['dim']}wal/db{c['r']}"), vc=c["b"])]
    tb, ub, fb, bs = s["blocks"]
    if tb:
        rows.append(line(c, "blocks", f"{ub:,}", bar(ub / tb, COL_BAR, c["blu"] if col else ""),
                         f"{c['dim']}of {tb:,} @ {human(bs)}, {fb:,} free{c['r']}"))
    # Only the store's own directories, and only if measured. A bar per directory that shares one
    # filesystem repeats the same shape four times and says nothing.
    # Split the temp directory before anything is drawn. Files older than the process cannot belong
    # to a live query, so counting them as `spill` overstated active spilling by the entire contents
    # of an abandoned one — 72.6 GB of it here, against 0 actually in use.
    started = time.time() - host["uptime"] if host.get("uptime") else None
    orph_b = orph_n = 0
    for mtime, size, _ in (sz or {}).get("temp_files") or []:
        if started and mtime < started:
            orph_b, orph_n = orph_b + size, orph_n + 1
    for k, label in (("duck", "columnar"), ("index", "search idx"), ("temp", "spill")):
        v = (sz or {}).get(k)
        if v is None:
            continue
        if k == "temp":
            v = max(0, v - orph_b)
        tot = (sz or {}).get("total") or 1
        # 64 MiB over a SLOW_EVERY window is the noise floor — below it, the temp directory is
        # holding files rather than receiving them, and a stale 72 GB is its own kind of finding.
        d = (sz or {}).get("temp_d") if k == "temp" else None
        hot = d is not None and d > 64 * 2**20
        # One sentence form for all three, so there is no vocabulary to learn and nothing is claimed
        # that du did not measure. In particular a big flat temp directory is NOT called orphaned:
        # files a running query is holding and files a killed one abandoned look identical from here.
        # The size goes yellow instead, which points at it without naming a cause.
        # Every bar here is a share of the on-disk total, and that total appeared nowhere — so the
        # panel read as three components of "database 80.8G" that separately came to 194G. Each row
        # now carries its own denominator, which is what makes the arithmetic checkable: the shares
        # add to 100, and the first row's `database` is the store's logical size, a different thing.
        # Terse, because this row also carries the share and the denominator: at 100 columns the
        # long form ran past the box and the measurement window — the part that makes the delta mean
        # anything — was what got truncated away.
        span = dur((sz or {}).get("dt")) if k == "temp" else "?"
        tail = f"{c['dim']}{v / tot * 100:.0f}% of {human(tot)} on disk{c['r']}"
        # Orphaned beats flat: if the NEWEST temp file is older than the process, every file in
        # there is, so none of them can belong to a live query — and DuckDB only deletes them in a
        # destructor, so a killed server leaks the lot and no later startup ever sweeps. That is a
        # reclaimable number, which is worth more than another word for "not moving".
        orphaned = k == "temp" and orph_b
        if hot:
            tail += f"  {c['red']}spilling +{human(d)}/{span}{c['r']}"
        elif d is not None and d < -64 * 2**20:
            tail += f"  {c['dim']}draining -{human(-d)}/{span}{c['r']}"
        elif d is not None:
            tail += f"  {c['dim']}flat {span}{c['r']}"
        rows.append(line(c, label, human(v),
                         bar(v / tot, COL_BAR,
                             (c["red"] if hot else c["yel"] if orphaned else c["blu"]) if col else ""),
                         tail, vc=c["yel"] if k == "temp" and v > 2**30 and not hot else None))
        if orphaned:
            # Its own labelled row, directly under spill: a number with an action attached, not an
            # annotation on someone else's number. In the tail it was the first thing truncated.
            rows.append(line(c, "orphaned", human(orph_b),
                             bar(orph_b / tot, COL_BAR, c["yel"] if col else ""),
                             f"{c['yel']}{orph_n} old temp files{c['r']}  "
                             f"{c['dim']}{orph_b / tot * 100:.0f}% reclaimable{c['r']}",
                             vc=c["yel"]))
    srows = rows

    # ── memory ──────────────────────────────────────────────────────────────────────────────────
    mrows = []

    # The note is padded to a fixed width so every sparkline in the panel starts on the same column.
    # Left ragged, each row's history began wherever its text happened to end, and traces that are
    # meant to be read against each other could not be.
    NOTE_W = 21

    def trace(key, note, scol, top):
        """Row tail: the note in a fixed-width field, then a sparkline filling what is left.

        The sparkline takes the SAME denominator as the bar on its own row, because it is that bar
        over time. Scaled against a panel-wide ceiling instead, `in use` drew a full-height trace
        beside a bar at 34% of memory_limit, and `swapped` a full one beside a bar at 80% — two
        different answers to "how full is this" on one line.
        """
        h = hist.get(key) or []
        # Clipped to the field, then padded to exactly one column past it. The old form padded with
        # max(1, NOTE_W - len) and then added a space, so a note that filled the field got 1 + 1
        # while a short one got n + 1 — one column of drift, which is precisely the row whose note
        # happens to be longest.
        txt = clip(note, NOTE_W)
        room = max(0, PTAIL - NOTE_W - 2)
        pad = " " * max(1, NOTE_W + 1 - len(strip(txt)))
        return f"{txt}{pad}{scol}{spark(h[-room:], top=top) if room and h else ''}{c['r']}"

    if s["memlimit"]:
        f = s["mem"] / s["memlimit"]
        mcol = c["red"] if f > 0.9 else (c["yel"] if f > 0.7 else c["grn"])
        mrows.append(line(c, "in use", human(s["mem"]), bar(f, COL_BAR, mcol),
                          # "of 100.2G" named no quantity — it is memory_limit, and which quantity
                          # it is decides whether 34% is comfortable or a machine oversubscribed.
                          trace("mem", f"{f * 100:.1f}% {c['dim']}of memory_limit{c['r']}",
                                c["mag"], s["memlimit"]), vc=c["b"]))
    # RSS against what the store thinks it holds. The gap is allocator arenas, the wire layer, code
    # and everything else that is not a DuckDB buffer — and it is the number the OOM killer reads,
    # so a memory_limit set from duckdb_memory() alone is set against the wrong quantity.
    rss, swap, peak = host.get("rss") or 0, host.get("swap") or 0, host.get("peak") or 0
    if rss:
        # One number per row. This carried its own value plus the gap to duckdb_memory() plus how
        # much of that gap was swap — three figures, and the two it did not own were the loud ones,
        # so a 6.4G row read as a claim about 29.7G. The gap is visible by reading the rows against
        # each other now that `in use` and `swapped` are both here.
        # Peak rides along with RSS rather than taking a row: it is the same quantity at a different
        # moment, and the panel has better uses for a line than saying that twice.
        mrows.append(line(c, "resident", human(rss), bar(rss / max(rss + swap, 1), COL_BAR,
                                                         c["grn"] if col else ""),
                          trace("rss", f"{c['dim']}RSS, peak {human(peak)}{c['r']}", c["grn"],
                                max(rss + swap, 1)),
                          vc=c["yel"] if swap else None))
        # Both unconditional once /proc has answered at all. Emitting `swapped` only when non-zero
        # made the panel one row taller the moment the kernel first paged something out, which is
        # both a jump and the worst moment to move the rows under someone.
        mrows.append(line(c, "swapped", human(swap), bar(swap / max(rss + swap, 1), COL_BAR,
                                                         (c["red"] if swap else c["dim"]) if col
                                                         else ""),
                          trace("swap", f"{c['red'] if swap else c['dim']}paged out{c['r']}",
                                c["red"], max(rss + swap, 1)),
                          vc=c["red"] if swap else None))
    # No `headroom` row: it was memory_limit minus in-use, which is the `in use` row's own
    # "33.9% of 100.2G" with one subtraction done for you, on a panel that has better uses for a line.
    # Every pool holding anything, not only those over 1% of the total. The 1% rule hid ART_INDEX at
    # 197 MB and IN_MEMORY_TABLE at 8 MB behind a BASE_TABLE of 34 GB — small against that, but they
    # are the pools that move, and with a history beside each one a small pool that is climbing is
    # exactly what this panel should be showing. Only the genuinely empty ones are counted away.
    #
    # Held separately from the rows above and fitted to the panel's height at layout time, exactly
    # as the threads and query lists are. Appending them directly made the panel's height a function
    # of how many pools happened to hold something this second, so it grew and shrank under the eye
    # — and in wide mode it dragged the box paired beside it along with it.
    tot_tag = sum(v for _, v in s["memtags"]) or 1
    top = max((v for _, v in s["memtags"]), default=1) or 1
    mtags = [line(c, tag, human(v), bar(v / top, COL_BAR, c["cyn"] if col else ""),
                  trace(f"t:{tag}", f"{c['dim']}{v / tot_tag * 100:.0f}% of tagged{c['r']}",
                        c["cyn"], top))
             for tag, v in s["memtags"] if v > 0]
    # ── activity ────────────────────────────────────────────────────────────────────────────────
    st = s["states"]
    act, idle = st.get("active", 0), st.get("idle", 0)
    ahead = [line(c, "sessions", str(act + idle), " " * COL_BAR,
                  f"{c['grn']}{act} active{c['r']}  {c['dim']}{idle} idle{c['r']}", vc=c["b"])]
    live = [q for stt, q in s["queries"] if stt == "active" and "pg_stat_activity" not in q]
    if not live and act == 0:
        ahead.append(f"{c['dim']}{' ' * COL_LABEL}nothing running — a pinned core now means "
                     f"orphaned server-side work{c['r']}")
    # Active first, then anything with a statement, then the rest. The server's `order by state` put
    # a NULL state ahead of 'active' — '?' sorts below 'a' — so sessions doing nothing took the top
    # slots off the one session doing something, which is the whole point of the panel.
    ordered = sorted((qq for qq in s["queries"] if "pg_stat_activity" not in qq[1]),
                     key=lambda qq: (qq[0] != "active", not qq[1], qq[0]))
    abody = [f"{(c['grn'] + '▸') if stt == 'active' else (c['dim'] + '·')}{c['r']} "
             f"{'' if stt == 'active' else c['dim']}"
             f"{clip(q, WIDE) if q else f'({stt}, no statement)'}{c['r']}"
             for stt, q in ordered]

    # ── threads ─────────────────────────────────────────────────────────────────────────────────
    if thr:
        # Scaled against one full core, not against the busiest thread. Normalizing to the top row
        # makes it full whether it sits at 8% or 99%, which is exactly the reading this panel exists
        # to give — a half-full bar has to mean half a core.
        trows = []
        for pct, comm, stt, tid in thr:
            # Two words only, each earning its place. `io wait` is the one thing the sampled state
            # knows that the percentage cannot — blocked in the kernel reads the same as descheduled
            # between slices. `pinned` marks a whole core held for the entire interval, the case the
            # panel exists to catch. Nothing else gets a word: a tier name for "54%" restates the
            # number and the bar, and needs a legend to mean anything.
            if stt == "D":
                tag, bcol = f"{c['yel']}io wait{c['r']}", c["yel"]
            elif pct >= 90:
                tag, bcol = f"{c['grn']}pinned{c['r']}", c["grn"]
            else:
                tag, bcol = "", c["blu"]
            # The label can only identify the thread; what it is DOING comes from the capture, keyed
            # by tid. Stale by up to one capture, so it is the tail rather than the headline.
            sym = by_tid.get(tid)
            if sym:
                nm = re.sub(r'^\[[.k]\]\s*', '', sym[0])
                tag += f"{'  ' if tag else ''}{c['dim']}{clip(nm, max(12, TAIL - len(strip(tag))))}{c['r']}"
            trows.append(line(c, comm, f"{pct:.1f}%", bar(pct / 100, COL_BAR, bcol if col else ""),
                              tag, vc=c["b"] if pct >= 90 or stt == "D" else None))
        if not by_tid:
            # Otherwise the empty column reads as broken rather than as unmeasured — and the two
            # reasons for it need different actions, so they do not get the same sentence.
            trows.append(f"{c['dim']}" + ("no captures — sudo ./perf-snap.sh --name serened "
                                          "for what each thread is running" if newest is None else
                                          "newest capture has no samples for these threads yet")
                         + f"{c['r']}")
    else:
        trows = []

    # ── profile ─────────────────────────────────────────────────────────────────────────────────
    if tops:
        # Group by engine first. A flat top-6 tells you a symbol is hot; the grouping tells you
        # which subsystem is busy, which is the thing you act on.
        fam = {}
        for sym, pct in tops:
            fam.setdefault(kernel_of(sym), []).append((sym, pct))
        order = sorted(fam.items(), key=lambda kv: -sum(p for _, p in kv[1]))
        # One denominator for the whole panel: every sampled cycle in the window. The engine line
        # used to divide by the six symbols it had room to print, so it read "columnar 50%" over
        # rows of 2% — two different denominators one line apart. Now the engine shares sum to 100
        # and each row is that symbol's share of the same total, so the two can be read against
        # each other, and a profile too flat for any single symbol to matter says so.
        tot = sum(p for _, ps in fam.items() for _, p in ps) or 1
        fcol = {"vector": c["mag"], "text": c["yel"], "columnar": c["cyn"],
                "wire": c["red"], "alloc": c["dim"], "kernel": c["blu"]}
        prows = [line(c, "engines", f"{len(fam)}", " " * COL_BAR,
                      "  ".join(f"{fcol.get(k, '')}{k} {sum(p for _, p in v) / tot * 100:.0f}%{c['r']}"
                                for k, v in order[:5]), vc=c["b"])]
        top1 = tops[0][1] or 1
        # Straight down by cost, hottest first. Grouping the rows by engine was how the panel used to
        # convey the subsystem split, but the engines line above now does that over the whole profile
        # — leaving the rows free to answer the other question, "what is actually expensive", in the
        # one order that can be scanned. Round-robin by engine put wire at 0.0% above nothing.
        psyms = [line(c, kernel_of(sym), f"{pct:.1f}%",
                      bar(pct / top1, COL_BAR, fcol.get(kernel_of(sym), "") if col else ""),
                      f"{c['dim']}{clip(re.sub(r'^\[[.k]\]\s*', '', sym), TAIL)}{c['r']}",
                      lc=fcol.get(kernel_of(sym)))
                 for sym, pct in tops[:32]]
        # A wall of hex is not a profile. perf resolves a container binary only for a reader that can
        # reach it through /proc/<pid>/root, which is root — perf-snap runs as root and gets names,
        # this dashboard does not and gets addresses off the SAME capture. The fix is one command,
        # keyed by build-id so it cannot resolve against the wrong build, and it has to be repeated
        # after every rebuild because a new build is a new id.
        # Judged on the top 20, not the whole profile: a long tail of unresolved kernel addresses is
        # normal for an unprivileged reader (kptr_restrict) and must not trip the hint on its own.
        head = tops[:20]
        if sum(bool(re.match(r"^\[[.k]\]\s*0x[0-9a-f]+$", sym)) for sym, _ in head) > len(head) / 2:
            prows.append(f"{c['yel']}symbols unresolved{c['r']}  {c['dim']}perf buildid-cache --add "
                         f"<the serened binary matching this build>{c['r']}")
        phead = prows
        # The capture name lives in the title. On the engines row it competed with the engine list
        # for the same line and lost — it was the part that got truncated away.
        ptitle = f"profile  {re.sub(r'[.]data$', '', newest or '')}"
    else:
        phead, psyms, ptitle = ([] if newest is not None else
                                [f"{c['dim']}no captures — sudo ./perf-snap.sh --name serened{c['r']}"],
                                [], "profile")

    # ── config ──────────────────────────────────────────────────────────────────────────────────
    # The five settings HAZARDS has measured consequences for, each with its predicate run against
    # the live sample. A firing predicate is the finding and gets the colour; otherwise the row
    # carries the one-line reason the setting is on this list at all, so the panel teaches what it
    # is watching for instead of listing values that mean nothing on their own.
    cfg = s["settings"]
    crows, bad = [], False
    for name in sorted(HAZARDS, key=lambda n: (HAZARDS[n][1] is None, n)):
        why, pred = HAZARDS[name]
        val = str(cfg.get(name, "?"))
        warn = pred(val, s) if pred else None
        bad = bad or bool(warn)
        # A value column ten wide suits `24` and `16.0 MiB` and mangles `/var/lib/serenedb/tmp` into
        # `/var/lib/…` — and the whole point of showing temp_directory is WHICH directory it is. Too
        # long for the column means it goes in the tail, where the width actually exists, and the
        # explanation gives way to it rather than the other way round.
        note = warn or why
        if len(val) <= COL_VALUE:
            tail = f"{c['red'] if warn else c['dim']}{clip(note, TAIL)}{c['r']}"
        else:
            head = f"{c['red'] if warn else c['grn']}{val}{c['r']}"
            tail = head + f"  {c['dim']}{clip(note, max(0, TAIL - len(val) - 2))}{c['r']}"
            val = ""
        crows.append(line(c, name, val, " " * COL_BAR, tail, vc=c["red"] if warn else None))

    # ── fit to the terminal ─────────────────────────────────────────────────────────────────────
    #
    # Everything above built every row it has. What actually fits is decided here, once, and the
    # slack goes to the two panels that are lists rather than fixed readouts — threads and running
    # queries. Hardcoding six and five wasted half a tall terminal and truncated a short one.
    # ── host ────────────────────────────────────────────────────────────────────────────────────
    hrows = [
        line(c, "cores", str(host.get("cores") or "?"), " " * COL_BAR,
             f"{c['dim']}load{c['r']} {' '.join(host.get('load') or ['?'])}  "
             f"{c['dim']}1/5/15m{c['r']}", vc=c["b"]),
        line(c, "os threads", str(host.get("threads") or "?"), " " * COL_BAR,
             f"{c['dim']}pools and runtimes on top of the store's own {cfg.get('threads', '?')}"
             f"{c['r']}"),
        line(c, "uptime", dur(host.get("uptime")), " " * COL_BAR,
             f"{c['dim']}since serened started{c['r']}"),
        line(c, "serened", str(host.get("pid") or "?"), " " * COL_BAR,
             f"{c['dim']}host pid in {host.get('container', '?')}{c['r']}"),
    ]

    # Two pairs of short fixed readouts, each pair costing the height of its taller half. The rows
    # that frees go to the two list panels below. Only when a half still holds the bar grid plus a
    # readable tail; narrower than that, stacked full width reads better.
    # The paired panels are pinned to a constant row count for the same reason the flex ones are:
    # `memory` gains and loses a tag row as allocations cross 1%, and without this the whole frame
    # below it moved every time that happened. Storage is five rows by construction; memory keeps
    # its head and its trailing "N hidden" line if it has to give up a middle one.
    def fixed_rows(rows, n):
        """Pad to n. Never truncate — these panels have no 'N more' to fall back on.

        This used to drop the overflow, and a short terminal silently lost `columnar` and
        `search idx` from storage: three of the five rows were the ones the panel exists for, and
        they vanished with nothing saying so. Padding is a layout choice; dropping is data loss.
        """
        return rows + [""] * max(0, n - len(rows))

    def fit(items, n):
        """Exactly n rows: padded when short, and when long the last row says how many are missing.

        At n == 1 the count is dropped rather than shown — spending the only row on "6 more" left a
        threads panel with a notice and no threads in it.
        """
        if len(items) <= n:
            return items + [""] * (n - len(items))
        if n <= 1:
            return items[:n]
        return items[:n - 1] + [f"{c['dim']}{' ' * COL_LABEL}{len(items) - n + 1} more{c['r']}"]

    # The total first, then the threads carrying it. Without the header a `top` reading of 300% had
    # no counterpart here — the rows are per-core shares, and three cores spread thin across a
    # hundred threads is a panel full of 3% rows and no way to add them up. An empty list under a
    # non-zero total is then a fact about the spread, not a broken panel.
    cores = host.get("cores") or 1
    cpu_note = f"{c['dim']}of {cores * 100}% across {host.get('threads', '?')} threads{c['r']}"
    cpu_room = max(0, TAIL - len(strip(cpu_note)) - 2)
    cpu_hist = hist.get("cpu") or []
    thead = [line(c, "cpu", f"{tcpu:.0f}%", bar(tcpu / (cores * 100), COL_BAR,
                                                (c["grn"] if tcpu > 100 else c["blu"]) if col else ""),
                  # Against every core the machine has, the same ceiling as the bar beside it —
                  # self-scaled, a process idling between 2% and 3% drew a full-height trace.
                  f"{cpu_note}  {c['grn']}"
                  f"{spark(cpu_hist[-cpu_room:], top=cores * 100) if cpu_room and cpu_hist else ''}"
                  f"{c['r']}", vc=c["b"]),
             ""]                 # the total is a different quantity from the rows; give it its own space

    # Built before the budget so its rows are COUNTED by it. thead was added to the threads box
    # after overhead was computed, so every frame came out one line taller than the terminal and the
    # bottom border scrolled off the screen.
    # The status bar is no longer one line — with a view behind every panel it wraps on a narrow
    # terminal, and a budget that assumes one line pushes the last row off the bottom again.
    overhead = 6 + len(status(c, W)) + len(ahead) + len(phead) + len(thead)
    # How tall the pinned pairs are allowed to be. Narrow means they stack instead of pairing, which
    # doubles their cost: on a resize from wide to narrow the four of them took ~28 rows of a short
    # terminal and the three list panels were left with one row each. They give ground first — nine
    # rows, three apiece, is the floor below which the lists stop being lists.
    # The pinned height can only pad, so a panel's real row count is its floor. Costing the search
    # with `pin` alone under-counted and the frame ran off the bottom of the terminal.
    # Memory's tag rows are not counted here: they fill whatever the pair's height turns out to be,
    # so the panel is sized by storage and its own fixed head, never by how many pools are live.
    floor_top = max(len(srows), len(mrows) + 3)
    floor_bot = max(len(crows), len(hrows))

    def cost(pin, keep_cfg, keep_host, keep_cyc):
        botn = keep_cfg + keep_host
        th, bh = max(pin, floor_top) + 2, max(pin, floor_bot) + 2
        top = th if pair else 2 * th
        bot = bh if (pair and botn == 2) else botn * bh
        return top + bot + (0 if keep_cyc else -(2 + len(phead)))

    # Search down for the tallest arrangement that still leaves the three list panels three rows
    # each: shrink the pinned rows first, and only then drop `host` and `config`. Those two are
    # context — slow-moving, and `c` shows all of it in full — while the panels above them are the
    # reason the dashboard is open. On an 80x24 terminal seven box frames alone are 14 of the rows,
    # so on the smallest screens something has to go, and it should not be live data.
    # Order of sacrifice: pinned padding, then `host`, then `config`, then `profile` last. Profile
    # goes only at the very end because it is live data — but it is the one live panel that depends
    # on an external capture and has a whole view of its own behind `s`, so on a screen too small
    # for everything it is the one that costs least to lose.
    plans = ([(p, 1, 1, 1) for p in (5, 4, 3, 2)] + [(p, 1, 0, 1) for p in (4, 3, 2)]
             + [(2, 0, 0, 1), (2, 0, 0, 0)])
    # Two passes: first insisting on three rows per list, then accepting one. The fixed panels have
    # a hard floor now that they never truncate — storage alone is six rows, and stacked on a narrow
    # 24-line terminal the pair is 16 before anything else is drawn — so without the second pass the
    # frame gave up and ran off the bottom, which loses the top of the screen rather than the least
    # important row of a list.
    pin, keep_cfg, keep_host, keep_cyc = next(
        (pl for need in (9, 3) for pl in plans if height - overhead - cost(*pl) >= need),
        plans[-1])
    if not keep_cyc:
        overhead -= 2 + len(phead)
        phead, psyms = [], []

    top_n, bot_n = max(pin, floor_top), max(pin, floor_bot)
    # Tags fill the height storage set, and overflow is counted on the last row — the same contract
    # the threads and query lists have, applied inside a pinned panel.
    mrows = mrows + fit(mtags, max(1, top_n - len(mrows)))
    srows, mrows = fixed_rows(srows, top_n), fixed_rows(mrows, top_n)
    crows, hrows = fixed_rows(crows, bot_n), fixed_rows(hrows, bot_n)
    cacc = "yel" if bad else "dim"
    if pair:
        top_lines = beside("storage", srows, "blu", "memory", mrows, "cyn", half)
        bot_lines = (beside("config", crows, cacc, "host", hrows, "dim", half)
                     if keep_cfg and keep_host else
                     mkbox("config", crows, cacc) if keep_cfg else [])
    else:
        top_lines = mkbox("storage", srows, "blu") + mkbox("memory", mrows, "cyn")
        bot_lines = ((mkbox("config", crows, cacc) if keep_cfg else [])
                     + (mkbox("host", hrows, "dim") if keep_host else []))

    # Budgets, not content, decide every panel's height — so a panel does not resize when a query
    # ends or a thread goes quiet, and nothing below it jumps. Whatever a panel cannot fill it pads;
    # whatever it cannot fit it counts on its last row. A dashboard whose rows move under the eye is
    # one you have to re-read from the top every refresh.
    slack = max(2 + keep_cyc, height - len(top_lines) - len(bot_lines) - overhead)
    n_act, n_thr, n_cyc = 0, 0, 0
    for i in range(slack):                       # round-robin, so the split is even and stable
        turn = i % (2 + keep_cyc)                # profile takes no share when it is not drawn
        n_thr, n_cyc, n_act = ((n_thr + 1, n_cyc, n_act) if turn == 0 else
                               (n_thr, n_cyc, n_act + 1) if turn == 1 else
                               (n_thr, n_cyc + 1, n_act))

    L.extend(top_lines)
    box("activity", ahead + fit(abody, n_act), "grn")
    # The total first, then the threads carrying it. Without the header a `top` reading of 300% had
    # no counterpart here — the rows are per-core shares, and three cores spread thin across a
    # hundred threads is a panel full of 3% rows and no way to add them up. An empty list under a
    # non-zero total is then a fact about the spread, not a broken panel.
    box("threads", thead + fit(trows or [f"{c['dim']}{' ' * COL_LABEL}nothing over 1% of a core — "
                                         f"the work above is spread thinner than that{c['r']}"],
                               max(1, n_thr - 1)), "grn")
    if keep_cyc:
        box(ptitle, phead + fit(psyms, n_cyc), "mag")
    L.extend(bot_lines)
    # Pinned to the bottom of the terminal, like every other view. The budget above aims to fill the
    # height exactly, but when a plan comes up a row or two short the keys should still be on the
    # last line rather than floating above a gap.
    keybar = status(c, W)          # not `bar` — that is the bar-glyph helper this frame calls
    L += [""] * max(0, height - len(L) - len(keybar))
    L.extend(keybar)
    return L




def apply_setting(container, port, password, name, value):
    """SET GLOBAL one setting. Returns (ok, message).

    Quoting: values go through a single-quoted literal with '' escaping, and the NAME is validated
    against an identifier pattern rather than quoted — it comes from the server's own settings list,
    but a dashboard that can be talked into running arbitrary SQL by a setting name is a dashboard
    with an injection bug, and the check costs nothing.
    """
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name or ""):
        return False, f"refusing: {name!r} is not a plain identifier"
    lit = str(value).replace("'", "''")
    out = psql(container, port, password, [f"SET GLOBAL {name} = '{lit}'"])
    if out is None:
        return False, "the server rejected it (or is unreachable) — value unchanged"
    return True, f"SET GLOBAL {name} = '{value}' — applied, NOT persisted"


def persistence(scope):
    """(marker, one-line, explanation) for whether a setting can be changed now, and whether it lasts.

    This exists because "I changed it and it looked like it worked" cost real time twice in one day:
    `SET GLOBAL temp_directory` took effect and `current_setting` confirmed it, and it would have
    reverted on the next restart because the flagfile still carried the old value. The same shape
    caught MySQL's binlog expiry an hour earlier — `SET PERSIST` wrote mysqld-auto.cnf, and the
    command line overrode it.

    The rule, in both cases: a runtime change is real until the process restarts, and the file wins
    at boot. So a runtime SET is a fix for now, never a fix.
    """
    sc = (scope or "").upper()
    if sc == "GLOBAL":
        return ("~", f"{'runtime'}",
                "Settable now with SET GLOBAL, and it takes effect immediately. It does NOT "
                "survive a restart: the flagfile is re-read at boot and wins. To make it stick, put "
                "it in serened.conf (mounted over /etc/serenedb/serened.conf) as well — changing "
                "only the runtime value means the next recreate silently reverts it.")
    if sc == "LOCAL":
        return ("·", "session",
                "Per-session. SET affects this connection only; other sessions and anything the "
                "server does on its own are unchanged.")
    return (" ", sc.lower() or "?", "Scope not reported by the server.")


def config_frame(rows, s, col, width, scroll, sel, detail, edit=None, msg=None):
    """The `c` view: every effective setting, selectable, with the full description on Enter.

    Descriptions are truncated in the list because 297 rows only fit if each is one line — but a
    truncated description is exactly the thing you needed when you came looking. So the list is a
    cursor, and Enter opens the untruncated text for the highlighted row.

    Hazards are pulled to the top and annotated with consequences we MEASURED here; the server
    already ships the description and repeating it would bury the part that matters.
    """
    c = C if col else NOCOLOR
    # The whole terminal. This was capped at 140 columns while the descriptions were then cut to
    # W - 62, so on a 200-column screen every description lost sixty characters to a limit that had
    # nothing to do with the screen — and a truncated description is the exact thing you opened this
    # view to read.
    W = max(70, width)
    DESC = max(20, W - 62)
    by = {r[0]: r for r in rows if r}

    if detail and detail in by:
        r = by[detail]
        name, val, desc = r[0], r[1] if len(r) > 1 else "", r[2] if len(r) > 2 else ""
        out = [f"{c['b']}{c['cyn']}{name}{c['r']}", ""]
        out.append(f"  {c['dim']}value{c['r']}   {c['b']}{val}{c['r']}")
        if len(r) > 3:
            out.append(f"  {c['dim']}type{c['r']}    {r[3]}")
        if len(r) > 4:
            mark, short, expl = persistence(r[4])
            pc = c["yel"] if short == "runtime" else c["dim"]
            out.append(f"  {c['dim']}scope{c['r']}   {r[4]}  {pc}{mark} {short}{c['r']}")
            out.append("")
            out += [f"  {pc}{ln}{c['r']}" for ln in textwrap.wrap(expl, W - 4)]
        out.append("")
        out += [f"  {ln}" for ln in textwrap.wrap(desc, W - 4)] if desc else []
        if name in HAZARDS:
            why, check = HAZARDS[name]
            out += ["", f"  {c['yel']}why it matters{c['r']}"]
            out += [f"  {c['dim']}{ln}{c['r']}" for ln in textwrap.wrap(why, W - 4)]
            warn = check(val, s) if check else None
            if warn:
                out += ["", f"  {c['red']}on this server{c['r']}"]
                out += [f"  {c['red']}{ln}{c['r']}" for ln in textwrap.wrap(warn, W - 4)]
        editable = len(r) > 4 and (r[4] or "").upper() == "GLOBAL"
        if edit is not None:
            out += ["", f"  {c['yel']}new value{c['r']} {c['b']}{edit}{c['r']}█",
                    f"  {c['dim']}enter apply · esc cancel{c['r']}",
                    f"  {c['dim']}applies immediately and reverts on restart — put it in "
                    f"serened.conf to keep it{c['r']}"]
        elif editable:
            out += ["", f"{c['dim']}  e edit · enter/esc back · q quit{c['r']}"]
        else:
            out += ["", f"{c['dim']}  not runtime-settable · enter/esc back · q quit{c['r']}"]
        if msg:
            out += ["", f"  {c['grn'] if msg[0] else c['red']}{msg[1]}{c['r']}"]
        return out, scroll, sel

    out = [f"{c['b']}{c['cyn']}effective configuration{c['r']}  "
           f"{c['dim']}{len(rows)} settings · {c['yel']}~{c['dim']} runtime-settable (reverts on restart) · jk move · enter details · c back · q{c['r']}", ""]
    out.append(f"{c['b']}{c['yel']}worth an opinion{c['r']}")
    for name, (why, check) in HAZARDS.items():
        r = by.get(name)
        if not r:
            continue
        val = r[1] if len(r) > 1 else "?"
        warn = check(val, s) if check else None
        out.append(f"  {c['b']}{name:24}{c['r']} {(c['red'] if warn else c['grn'])}{val[:30]:30}{c['r']} "
                   f"{c['dim']}{clip(why, DESC)}{c['r']}")
    out += ["", f"{c['b']}{c['cyn']}all settings{c['r']}"]

    body = sorted([r for r in rows if len(r) >= 3], key=lambda x: x[0].lower())
    sel = max(0, min(sel, len(body) - 1)) if body else 0
    view = max(6, shutil.get_terminal_size((100, 40)).lines - len(out) - 3)
    scroll = max(0, min(scroll, max(0, len(body) - view)))
    if sel < scroll:
        scroll = sel
    elif sel >= scroll + view:
        scroll = sel - view + 1
    for i, r in enumerate(body[scroll:scroll + view], start=scroll):
        cur = i == sel
        hot = r[0] in HAZARDS
        mark = f"{c['cyn']}›{c['r']}" if cur else " "
        lab = (c["b"] if (cur or hot) else c["dim"])
        pm, _, _ = persistence(r[4] if len(r) > 4 else "")
        out.append(f"{mark} {lab}{r[0][:28]:28}{c['r']} "
                   f"{c['yel']}{pm}{c['r']} "
                   f"{(c['yel'] if hot else '')}{r[1][:22]:22}{c['r']} "
                   f"{c['dim']}{clip(r[2], DESC)}{c['r']}")
    out.append(f"{c['dim']}  {sel + 1}/{len(body)}{c['r']}")
    out.extend(status(c, W, f"{c['b']}e{c['r']} {c['dim']}edit{c['r']}  "
                            f"{c['dim']}·{c['r']}  {c['b']}enter{c['r']} {c['dim']}describe{c['r']}"))
    return out, scroll, sel


# ── the producer/consumer binding ───────────────────────────────────────────────────────────────
#
# perf-snap.sh writes captures; this reads them. Rather than leave the dashboard to notice on its
# next tick, the two are bound explicitly: the dashboard drops its pid in the perf directory, and
# perf-snap sends SIGUSR1 after each capture is chowned and its symbols are written.
#
# The dashboard still rescans on every tick, so the signal is an optimisation, not a requirement —
# if perf-snap is an older copy, or someone drops a capture in by hand, it is picked up within one
# refresh anyway. A binding that is required to work is a binding that breaks the tool when it does
# not; this one only makes it faster.
#
# Self-pipe rather than a bare flag: the wait sits in select(), and Python retries select on EINTR
# (PEP 475), so a handler that only sets a variable would not wake it until the timeout expired —
# which is exactly the latency the signal exists to remove.
_WAKE_R, _WAKE_W = os.pipe()
os.set_blocking(_WAKE_W, False)
os.set_blocking(_WAKE_R, False)


def _on_usr1(_sig, _frm):
    try:
        os.write(_WAKE_W, b"x")
    except OSError:
        pass


def write_pidfile(perf_dir):
    try:
        os.makedirs(perf_dir, exist_ok=True)
        p = os.path.join(perf_dir, ".serenedash.pid")
        with open(p, "w") as f:
            f.write(f"{os.getpid()}\n")
        return p
    except OSError:
        return None


def wait_key(timeout):
    """Sleep, waking early on a keypress OR on SIGUSR1. Returns the key, 'wake', or ''.

    Assumes the terminal is already in cbreak — main sets it once for the whole session. It used to
    be set here and restored on the way out, which left the terminal in cooked mode with echo ON for
    the entire render, and a render includes several docker execs. Keys pressed in that window were
    echoed by the driver and queued: holding `j` sprayed `j`s across the bottom of the screen, and a
    function key left `^[[2;2~` sitting there.
    """
    # Everything here is fd-level. sys.stdin.read(1) pulls a whole chunk into Python's buffer, so
    # the follow-up select() saw an empty fd and every arrow key was reported as a bare Esc — which
    # exited the view — while the rest of the sequence sat in the buffer waiting to be returned as
    # separate keystrokes.
    if not sys.stdin.isatty():
        return ""
    fd = sys.stdin.fileno()

    def rd(n=1):
        try:
            return os.read(fd, n).decode("utf-8", "ignore")
        except OSError:
            return ""

    end = time.monotonic() + timeout
    while True:
        left = end - time.monotonic()
        if left <= 0:
            return ""
        r = select.select([_WAKE_R, fd], [], [], left)[0]
        if _WAKE_R in r:
            try:
                os.read(_WAKE_R, 4096)
            except OSError:
                pass
            return "wake"
        if fd in r:
            ch = rd()
            if not ch:
                return ""
            if ch != "\x1b":
                return ch.lower()
            # ESC is ambiguous: it is either the Esc key, or the first byte of an arrow/function
            # sequence (\x1b[A etc). Peek with a short timeout — if more bytes are waiting it was a
            # sequence, if not the user pressed Esc.
            if not select.select([fd], [], [], 0.02)[0]:
                return "\x1b"
            if rd() != "[":
                return "\x1b"
            # Consume to the sequence's final byte (@ through ~) rather than one character. A
            # modified key sends \x1b[2;2~ — reading a single byte left ";2~" behind to be taken as
            # three more keystrokes and echoed into the corner of the screen.
            code, fin = "", ""
            while True:
                fin = rd()
                if not fin or "\x40" <= fin <= "\x7e":
                    break
                code += fin
            return {"A": "up", "B": "down", "C": "right", "D": "left"}.get(
                fin, {"5": "pgup", "6": "pgdn"}.get(code, ""))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-n", "--interval", type=float, default=5.0)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--container", default=os.environ.get("SERENEDB_CONTAINER", "oracle-serenedb"))
    ap.add_argument("--port", default=os.environ.get("SERENEDB_PORT", "7890"))
    ap.add_argument("--password", default=os.environ.get("PGPASSWORD", "oracle-sdb"))
    ap.add_argument("--data", default="/var/lib/serenedb")
    ap.add_argument("--perf-dir", default=os.environ.get(
        "SERENEDASH_PERF_DIR", os.path.expanduser("~/.cache/serenedash/perf")),
        help="where perf-snap.sh writes captures. The dashboard reads them; it cannot record "
             "on its own because perf_event_paranoid blocks attaching to a container process "
             "without root, and making the whole dashboard run as root to get a panel is a bad "
             "trade.")
    a = ap.parse_args()
    col = not a.no_color and sys.stdout.isatty()

    prev, sz, tick, shown, fresh, s = None, {}, 0, [], True, None
    hist = {"mem": []}
    perf = (None, [], {})
    crows = []
    thr, tcpu, tprev, tlast = [], 0.0, {}, time.time()
    hinfo, wh = {}, (0, 0)
    view, scroll, sel, detail = "main", 0, 0, None
    edit, msg = None, None
    hpid = host_pid(a.container)
    # Prime the tick counters before the first frame. Percentages are deltas, so a cold start has
    # nothing to subtract from and the panel came up empty — for the whole of a first tick that also
    # runs du and parses a capture, which is long enough to look broken rather than pending.
    if hpid:
        _, _, tprev, tlast = threads(hpid, {}, tlast)
    signal.signal(signal.SIGUSR1, _on_usr1)
    # Turn a kill into an orderly exit so the finally block actually runs. Default SIGTERM/SIGHUP
    # handling ends the process without unwinding, which would leave the terminal on the alternate
    # screen with no cursor — the same shape of bug as a destructor that never gets to clean up.
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: sys.exit(0))
    pidfile = write_pidfile(a.perf_dir)
    # Raw-ish mode for the whole session, not per keystroke. Restored in the finally below, in the
    # same breath as the cursor and the alternate screen.
    old_tty = None
    if not a.once and sys.stdin.isatty():
        old_tty = termios.tcgetattr(sys.stdin.fileno())
        tty.setcbreak(sys.stdin.fileno())
    if not a.once:
        # Alternate screen buffer: the dashboard draws on a scratch screen and the shell's scrollback
        # is handed back untouched on exit. Without it a session's worth of frames is left in the
        # buffer, and a frame one line too tall scrolls the terminal instead of just being clipped.
        sys.stdout.write("\033[?1049h\033[?25l\033[2J")
    try:
        while True:
            # A keypress redraws; it does not re-query. Falling through to the sampler on every key
            # cost two docker execs per press — one for the sample and, in the config view, another
            # for all 297 settings — which is why holding `j` there felt like the list was stuck
            # rather than slow. Only a timeout or a SIGUSR1 wake refreshes the data.
            if fresh:
                s = sample(a.container, a.port, a.password)
            if s is None:
                lines = [f" cannot reach {a.container}:{a.port}"]
            else:
                if fresh:
                    if tick % SLOW_EVERY == 0:
                        sz = slow(a.container, a.data, sz)
                    hist["mem"] = (hist["mem"] + [s["mem"]])[-HIST:]
                    # Per tag as well as in total. The total tells you memory moved; only the
                    # per-tag traces say WHICH pool moved, and that is the difference between a
                    # query holding a hash table and a table cache that has been growing all
                    # afternoon. Tags absent from this sample record a zero rather than freezing,
                    # so a pool that drains reads as dropping to the floor instead of holding its
                    # last value forever.
                    for key, val in (("cpu", tcpu), ("rss", hinfo.get("rss") or 0),
                                     ("swap", hinfo.get("swap") or 0)):
                        hist[key] = (hist.get(key, []) + [val])[-HIST:]
                    now_tags = dict(s["memtags"])
                    for key in set(now_tags) | {k[2:] for k in hist if k.startswith("t:")}:
                        hist["t:" + key] = (hist.get("t:" + key, [])
                                            + [now_tags.get(key, 0)])[-HIST:]
                    perf = perf_window(a.perf_dir)
                    hpid = hpid or host_pid(a.container)
                    if hpid:
                        thr, tcpu, tprev, tlast = threads(hpid, tprev, tlast)
                    hinfo = hostinfo(hpid, a.container)
                tsz = shutil.get_terminal_size((100, 40))
                w, h = tsz.columns, tsz.lines
                # A resize invalidates every line on screen, but the redraw below only rewrites the
                # ones whose TEXT changed — so after growing the terminal the old frame sat there in
                # pieces until each line happened to differ. Clear once and repaint in full.
                if (w, h) != wh:
                    wh, shown = (w, h), []
                    if not a.once:
                        sys.stdout.write("\033[2J")
                cc = C if col else NOCOLOR
                if view == "graph":
                    nm, ls = callstacks(a.perf_dir)
                    keybar = status(cc, w, f"{cc['b']}g{cc['r']} {cc['dim']}back{cc['r']}  "
                                           f"{cc['dim']}·{cc['r']}  {cc['b']}j/k{cc['r']} "
                                           f"{cc['dim']}scroll{cc['r']}")
                    lines = [f"{cc['b']}call graph{cc['r']}  {cc['dim']}{nm or 'no captures'}"
                             f"{cc['r']}", ""] + ls[scroll:scroll + max(1, h - 2 - len(keybar))]
                    lines += [""] * max(0, h - len(lines) - len(keybar)) + keybar
                # Every panel has a view behind it, keyed by its own name. They share one shape:
                # build the whole thing, slice to the window, and end with the status bar carrying
                # the key that goes back — so no view is a place you can get stuck.
                elif view in DETAIL:
                    body = {"storage": lambda: storage_frame(s, sz, hinfo, col, w, scroll),
                            "memory": lambda: memory_frame(s, hist, hinfo, col, w, scroll),
                            "activity": lambda: activity_frame(s, col, w, scroll),
                            "threads": lambda: threads_frame(thr, tcpu, perf[2], hinfo, col, w,
                                                             scroll),
                            "profile": lambda: profile_frame(perf, col, w, scroll),
                            "host": lambda: host_frame(hinfo, s, col, w, scroll),
                            "legend": lambda: legend_frame(col, w, scroll)}[view]()
                    keybar = status(cc, w, f"{cc['b']}{DETAIL[view]}{cc['r']} "
                                           f"{cc['dim']}back{cc['r']}  {cc['dim']}·{cc['r']}  "
                                           f"{cc['b']}j/k{cc['r']} {cc['dim']}scroll{cc['r']}")
                    # Pinned to the last rows of the terminal, not left floating under whatever the
                    # view happened to be tall. The keys belong in the same place on every screen.
                    lines = body[:max(1, h - len(keybar))]
                    lines += [""] * max(0, h - len(lines) - len(keybar)) + keybar
                elif view == "config":
                    # 297 settings is a big result and they change only when someone changes them,
                    # so it is fetched on the data tick and reused for every keypress in between.
                    if fresh or not crows:
                        cfg = psql(a.container, a.port, a.password,
                                   ["select name, value, coalesce(description,''), "
                                    "input_type, scope from duckdb_settings()"])
                        crows = cfg[0] if cfg else []
                    lines, scroll, sel = config_frame(crows, s, col, w, scroll, sel, detail, edit, msg)
                else:
                    lines = frame(s, prev, sz, hist, perf, thr, tcpu, hinfo, col, w, h)
                prev = s
            tick += 1
            if a.once:
                print("\n".join(lines))
                return 0
            for i, ln in enumerate(lines):
                if i >= len(shown) or shown[i] != ln:
                    sys.stdout.write(f"\033[{i + 1};1H\033[2K{ln}")
            for i in range(len(lines), len(shown)):
                sys.stdout.write(f"\033[{i + 1};1H\033[2K")
            shown = lines
            sys.stdout.write(f"\033[{len(lines) + 1};1H")
            sys.stdout.flush()
            k = wait_key(a.interval)
            # '' is the interval elapsing, 'wake' is perf-snap signalling a new capture. Anything
            # else is a keystroke, and a keystroke only changes what is drawn, never what is known.
            fresh = k in ("", "wake")
            if edit is not None:
                # A tiny line editor. Enter applies, Esc cancels, backspace deletes; anything
                # printable appends. Deliberately no history or cursor movement — this is for
                # changing one value, not for living in.
                if k in ("\r", "\n"):
                    msg = apply_setting(a.container, a.port, a.password, detail, edit)
                    edit = None
                elif k == "\x1b":
                    edit, msg = None, None
                elif k in ("\x7f", "\b"):
                    edit = edit[:-1]
                elif k and len(k) == 1 and k.isprintable():
                    edit += k
                shown = [None] * len(shown)
                continue
            if k == "e" and view == "config" and detail:
                row = next((r for r in crows if r and r[0] == detail), None)
                if row and len(row) > 4 and (row[4] or "").upper() == "GLOBAL":
                    edit, msg = (row[1] if len(row) > 1 else ""), None
                    shown = [None] * len(shown)
                continue
            if k == "\x1b" and (detail or view != "main"):
                # One level at a time. Escaping out of a setting's description dropped the config
                # list as well and landed on the main frame, so getting back to where you were meant
                # pressing c and scrolling to the row again.
                if detail:
                    detail = None
                else:
                    view = "main"
                shown = [None] * len(shown)
                continue
            if k == "q":
                return 0
            # One toggle rule for every view, so a key never means two things and no view can be
            # reached that its own key does not leave.
            bykey = {v: n for n, v in DETAIL.items()}
            bykey.update({"g": "graph", "c": "config"})
            if k in bykey:
                view = "main" if view == bykey[k] else bykey[k]
                scroll, shown = 0, [None] * len(shown)
            elif k in ("j", "down"):
                if view == "config" and not detail:
                    sel += 1
                else:
                    scroll += 5
            elif k in ("k", "up"):
                if view == "config" and not detail:
                    sel = max(0, sel - 1)
                else:
                    scroll = max(0, scroll - 5)
            elif k in ("\r", "\n", " ") and view == "config":
                # Enter opens the highlighted setting; enter/esc again closes it.
                if detail:
                    detail = None
                else:
                    body = sorted([r for r in crows if len(r) >= 3], key=lambda x: x[0].lower())
                    detail = body[sel][0] if body and sel < len(body) else None
                shown = [None] * len(shown)
    except KeyboardInterrupt:
        return 0
    finally:
        if old_tty is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_tty)
        if not a.once:
            # Wipe the scratch screen before handing the terminal back, then leave the alternate
            # buffer and restore the cursor — all in the finally block, so a crash or a kill cannot
            # strand the terminal on the scratch buffer or leave a half-drawn frame behind.
            sys.stdout.write("\033[2J\033[H\033[?25h\033[?1049l")
            sys.stdout.flush()
        if pidfile:
            try:
                os.unlink(pidfile)
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""serenedash — a live dashboard for a SereneDB server, cheap enough to leave open.

    ./serenedash.py                 refresh every 5s
    ./serenedash.py -n 2            faster
    ./serenedash.py --once          one frame, plain text (scripts, logs, cron mail)
    ./serenedash.py --no-color
    ./serenedash.py --container oracle-serenedb --port 7890

Keys: `q` / `Esc` quit.

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
import time
import tty

SEP = "---8<---"
SLOW_EVERY = 12
HIST = 24
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
        "select coalesce(state,'?'), count(*) from pg_stat_activity group by 1",
        "select coalesce(state,'?'), replace(replace(coalesce(query,''),chr(10),' '),chr(13),' ') "
        "from pg_stat_activity where coalesce(query,'') <> '' order by state",
        "select current_setting('temp_directory'), current_setting('threads'), "
        "  current_setting('checkpoint_threshold')",
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
        "settings": b[4][0] if b[4] and len(b[4][0]) >= 3 else ["?", "?", "?"],
        "t": time.time(),
    }


def du(container, path):
    try:
        o = subprocess.run(["docker", "exec", container, "du", "-sm", path],
                           capture_output=True, text=True, timeout=240)
        return int(o.stdout.split()[0]) * 2**20
    except Exception:                                           # noqa: BLE001
        return None


def slow(container, data_dir):
    return {
        "index": du(container, f"{data_dir}/engine_search"),
        "duck": du(container, f"{data_dir}/engine_duckdb"),
        "temp": du(container, f"{data_dir}/tmp"),
        "total": du(container, data_dir),
    }


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
        return None, []
    if not files:
        return None, []

    agg, newest = {}, None
    for p in files:
        try:
            key = (p, os.path.getmtime(p))
        except OSError:
            continue
        newest = newest or os.path.basename(p)
        if key not in cache:
            # -F overhead,symbol: `--sort symbol` still emits the event's other columns padded
            # per-section with '-' placeholders, so the same symbol arrives as two different strings
            # and splits in two. And a hybrid CPU reports one table PER PMU, so reading only the
            # first gives the E-core view — the minority of the cycles.
            try:
                out = subprocess.run(
                    ["perf", "report", "-i", p, "--stdio", "-F", "overhead,symbol",
                     "--no-children", "-g", "none", "--percentage", "absolute"],
                    capture_output=True, text=True, timeout=120)
                rows, ec, tot = {}, 0.0, 0.0
                for ln in out.stdout.splitlines():
                    m = re.match(r"^# Event count \(approx\.\): (\d+)", ln)
                    if m:
                        ec = float(m.group(1))
                        tot += ec
                        continue
                    m = re.match(r"^\s+([\d.]+)%\s+(.+?)\s*$", ln)
                    if m and ec:
                        rows[m.group(2)] = rows.get(m.group(2), 0.0) + float(m.group(1)) * ec
                cache[key] = {k: v / tot for k, v in rows.items()} if tot else {}
            except Exception:                                   # noqa: BLE001
                cache[key] = {}
        for sym, pct in cache[key].items():
            agg[sym] = agg.get(sym, 0.0) + pct / len(files)
    if len(cache) > 32:
        for k in list(cache)[:-32]:
            cache.pop(k, None)
    return newest, sorted(agg.items(), key=lambda kv: -kv[1])[:6]



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


def threads(pid, prev, dt):
    """Per-thread CPU and state, newest first by CPU.

    Threads, not the process total. 100% of one core out of 24 reads as "4% busy" at process level
    and as a pinned thread here — and that difference is the whole diagnosis when a single thread
    spins. `R` running vs `S` sleeping alongside it distinguishes work from a stall.
    """
    out, cur = [], {}
    try:
        for t in os.scandir(f"/proc/{pid}/task"):
            try:
                with open(f"{t.path}/stat") as f:
                    fields = f.read().rsplit(") ", 1)
                comm = fields[0].split("(", 1)[1]
                rest = fields[1].split()
                st, ticks = rest[0], int(rest[11]) + int(rest[12])
            except (OSError, IndexError, ValueError):
                continue
            cur[t.name] = ticks
            if prev and t.name in prev and dt > 0:
                pct = (ticks - prev[t.name]) / os.sysconf("SC_CLK_TCK") / dt * 100
                if pct > 1:
                    out.append((pct, comm[:18], st))
    except OSError:
        return [], cur
    return sorted(out, reverse=True)[:6], cur


def callstacks(perf_dir, limit=40):
    """Caller-oriented call graph from the newest capture.

    Flat symbol lists say WHAT is hot; they cannot say what led into it. That distinction is what
    separated a spinning COPY feeder from a spinning recv loop when both showed the same leaf.
    """
    try:
        files = sorted((os.path.join(dp, f) for dp, _, fs in os.walk(perf_dir) for f in fs
                        if f.endswith(".data")), key=os.path.getmtime, reverse=True)
    except OSError:
        return None, []
    if not files:
        return None, []
    try:
        o = subprocess.run(["perf", "report", "-i", files[0], "--stdio", "--sort", "symbol",
                            "-g", "graph,0.5,caller"], capture_output=True, text=True, timeout=180)
        lines = [ln.rstrip() for ln in o.stdout.splitlines()
                 if ln.strip() and not ln.startswith("#")][:limit]
        return os.path.basename(files[0]), lines
    except Exception:                                           # noqa: BLE001
        return os.path.basename(files[0]), []


def spark(v, w=HIST):
    v = [x for x in v[-w:] if x is not None]
    if len(v) < 2:
        return "·" * max(1, len(v))
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


def frame(s, prev, sz, hist, perf, thr, col, width):
    c = C if col else NOCOLOR
    W = max(70, min(width, 120))
    inner = W - 2
    L = []

    def box(title, rows, accent="cyn"):
        t = f"{c[accent]}{c['b']}{title}{c['r']}" if col else title
        L.append(f"{c['dim']}┌─{c['r']}{t}{c['dim']} " + "─" * max(0, inner - len(title) - 2) + f"┐{c['r']}")
        for r in rows:
            vis = len(strip(r))
            r = r if vis <= inner - 1 else r[:inner - 2] + "…"
            L.append(f"{c['dim']}│{c['r']} {r}" + " " * max(0, inner - min(vis, inner - 1) - 1) + f"{c['dim']}│{c['r']}")
        L.append(f"{c['dim']}└" + "─" * inner + f"┘{c['r']}")

    # ── storage: the WAL ratio is the headline, not the sizes ───────────────────────────────────
    ratio = s["wal"] / s["size"] if s["size"] else 0
    wcol = c["red"] if ratio > 1 else (c["yel"] if ratio > 0.3 else c["grn"])
    rows = [
        f"{c['dim']}database{c['r']}  {c['b']}{human(s['size']):>8}{c['r']}   "
        f"{c['dim']}wal{c['r']} {wcol}{human(s['wal']):>8}{c['r']}   "
        f"{c['dim']}ratio{c['r']} {wcol}{ratio:5.2f}x{c['r']}  "
        + (f"{c['red']}checkpoint is not completing{c['r']}" if ratio > 1 else
           f"{c['dim']}wal/db{c['r']}"),
    ]
    tb, ub, fb, bs = s["blocks"]
    if tb:
        rows.append(f"{c['dim']}blocks{c['r']}    {ub:,}/{tb:,} used  "
                    f"{bar(ub / tb, 18, c['blu'] if col else '')} "
                    f"{c['dim']}{fb:,} free @ {human(bs)}{c['r']}")
    if sz:
        for k, label in (("duck", "columnar"), ("index", "search idx"), ("temp", "spill")):
            v = sz.get(k)
            if v is None:
                continue
            tot = sz.get("total") or 1
            hot = c["red"] if (k == "temp" and v > 2**30) else (c["blu"] if col else "")
            rows.append(f"{c['dim']}{label:10}{c['r']}{human(v):>8}   "
                        f"{bar(v / tot, 18, hot)} {c['dim']}of {human(tot)} on disk{c['r']}")
    box("storage", rows, "blu")

    # ── memory ──────────────────────────────────────────────────────────────────────────────────
    mrows = []
    if s["memlimit"]:
        f = s["mem"] / s["memlimit"]
        mcol = c["red"] if f > 0.9 else (c["yel"] if f > 0.7 else c["grn"])
        mrows.append(f"{c['dim']}in use{c['r']}    {c['b']}{human(s['mem']):>8}{c['r']}   "
                     f"{bar(f, 18, mcol)} {f * 100:4.1f}% {c['dim']}of {human(s['memlimit'])}{c['r']}  "
                     f"{c['mag']}{spark(hist['mem'])}{c['r']}")
    top = max((v for _, v in s["memtags"]), default=1) or 1
    for tag, v in s["memtags"][:5]:
        if v <= 0:
            continue
        mrows.append(f"{c['dim']}{tag[:16]:16}{c['r']}{human(v):>8}   {bar(v / top, 18, c['cyn'] if col else '')}")
    box("memory", mrows, "cyn")

    # ── activity ────────────────────────────────────────────────────────────────────────────────
    st = s["states"]
    act, idle = st.get("active", 0), st.get("idle", 0)
    arows = [f"{c['dim']}sessions{c['r']}  {c['grn']}{act} active{c['r']}   "
             f"{c['dim']}{idle} idle{c['r']}   "
             + "   ".join(f"{c['dim']}{k}{c['r']} {v}" for k, v in sorted(st.items())
                          if k not in ("active", "idle"))]
    # A pinned core with nothing active is the orphaned-work signature — say so rather than
    # leaving an empty panel that reads as "nothing is happening".
    live = [q for stt, q in s["queries"] if stt == "active" and "pg_stat_activity" not in q]
    if not live and act <= 1:
        arows.append(f"{c['dim']}no client query running — if a core is pinned, it is orphaned "
                     f"server-side work{c['r']}")
    for stt, q in s["queries"][:6]:
        if "pg_stat_activity" in q:
            continue
        mark = f"{c['grn']}▸{c['r']}" if stt == "active" else f"{c['dim']}·{c['r']}"
        arows.append(f"{mark} {c['dim']}{stt[:6]:6}{c['r']} {q[:88]}")
    box("activity", arows, "grn")

    # ── threads: where the parallelism actually is ──────────────────────────────────────────────
    if thr:
        top = thr[0][0] or 1
        trows = [f"{c['dim']}{len(thr)} thread(s) over 1% · R=running S=sleeping{c['r']}"]
        for pct, comm, st in thr:
            sc = c["grn"] if st == "R" else c["dim"]
            trows.append(f"{sc}{st}{c['r']} {c['b']}{pct:6.1f}%{c['r']} "
                         f"{bar(pct / top, 16, c['grn'] if st == 'R' else c['blu'])} "
                         f"{c['dim']}{comm}{c['r']}")
        box("threads", trows, "grn")

    # ── where the cycles are ────────────────────────────────────────────────────────────────────
    newest, tops = perf
    if tops:
        # perf marks every sample [k] or [.]. Kernel SYMBOLS need kptr_restrict=0, but the split
        # itself is always available and answers the question that matters: engine, or syscalls.
        kern = sum(v for sym, v in tops if sym.startswith("[k]"))
        user = sum(v for sym, v in tops if not sym.startswith("[k]"))
        tot = (kern + user) or 1
        prows = [f"{c['dim']}shape{c['r']}    "
                 f"{c['cyn']}user {user / tot * 100:4.1f}%{c['r']}  "
                 f"{c['mag']}kernel {kern / tot * 100:4.1f}%{c['r']}  "
                 f"{c['dim']}{newest}{c['r']}"]
        top1 = tops[0][1] or 1
        for sym, pct in tops:
            k = sym.startswith("[k]")
            name = re.sub(r"^\[[.k]\]\s*", "", sym)
            prows.append(f"{(c['mag'] if k else c['cyn'])}{pct:5.1f}%{c['r']} "
                         f"{bar(pct / top1, 12, c['mag'] if k else c['cyn'])} "
                         f"{c['dim'] if k else ''}{name[:60]}{c['r']}")
        box("cycles", prows, "mag")
    elif newest is None:
        box("cycles", [f"{c['dim']}no captures yet — run: sudo ./perf-snap.sh --name serened"
                       f"{c['r']}"], "dim")

    # ── config that has burned us ───────────────────────────────────────────────────────────────
    tmpdir, threads, ckpt = (s["settings"] + ["?", "?", "?"])[:3]
    bad = not str(tmpdir).startswith("/")
    box("config", [
        f"{c['dim']}temp_directory{c['r']}       "
        + (f"{c['red']}{tmpdir}  ← RELATIVE: every spill fails on a stock image{c['r']}"
           if bad else f"{c['grn']}{tmpdir}{c['r']}"),
        f"{c['dim']}threads{c['r']} {threads}    {c['dim']}checkpoint_threshold{c['r']} {ckpt}"
        f"    {c['dim']}press{c['r']} c {c['dim']}for the full effective config{c['r']}",
    ], "yel" if bad else "dim")
    return L


def config_frame(rows, s, col, width, scroll):
    """The `c` view: every effective setting, with the server's own description.

    Hazards first and annotated, because 297 settings sorted alphabetically is a list nobody reads
    to the end. The annotations are consequences we have MEASURED here, not a restatement of the
    description — the server already ships that, and repeating it would bury the part that matters.
    """
    c = C if col else NOCOLOR
    W = max(70, min(width, 140))
    out = [f"{c['b']}{c['cyn']}effective configuration{c['r']}  "
           f"{c['dim']}{len(rows)} settings · q quit · c back · ↑↓/jk scroll{c['r']}", ""]

    by = {r[0]: r for r in rows if r}
    out.append(f"{c['b']}{c['yel']}worth an opinion{c['r']}")
    for name, (why, check) in HAZARDS.items():
        r = by.get(name)
        if not r:
            continue
        val = r[1] if len(r) > 1 else "?"
        warn = check(val, s) if check else None
        vcol = c["red"] if warn else c["grn"]
        out.append(f"  {c['b']}{name:24}{c['r']} {vcol}{val[:34]:34}{c['r']} {c['dim']}{why}{c['r']}")
        if warn:
            for i in range(0, len(warn), W - 8):
                out.append(f"      {c['red']}{warn[i:i + W - 8]}{c['r']}")
    out.append("")
    out.append(f"{c['b']}{c['cyn']}all settings{c['r']}")

    body = []
    for r in sorted(rows, key=lambda x: x[0].lower()):
        if len(r) < 3:
            continue
        name, val, desc = r[0], r[1], r[2]
        hot = name in HAZARDS
        body.append(f"  {(c['b'] if hot else c['dim'])}{name[:28]:28}{c['r']} "
                    f"{(c['yel'] if hot else '')}{val[:26]:26}{c['r']} "
                    f"{c['dim']}{desc[:W - 62]}{c['r']}")
    view = max(8, shutil.get_terminal_size((100, 40)).lines - len(out) - 2)
    scroll = max(0, min(scroll, max(0, len(body) - view)))
    out += body[scroll:scroll + view]
    out.append(f"{c['dim']}  {scroll + 1}-{min(scroll + view, len(body))} of {len(body)}{c['r']}")
    return out, scroll


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
        p = os.path.join(perf_dir, ".serendash.pid")
        with open(p, "w") as f:
            f.write(f"{os.getpid()}\n")
        return p
    except OSError:
        return None


def wait_key(timeout):
    """Sleep, waking early on a keypress OR on SIGUSR1. Returns the key, 'wake', or ''."""
    fds = [_WAKE_R] + ([sys.stdin] if sys.stdin.isatty() else [])
    old = None
    if sys.stdin.isatty():
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        tty.setcbreak(fd)
    try:
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return ""
            r = select.select(fds, [], [], left)[0]
            if _WAKE_R in r:
                try:
                    os.read(_WAKE_R, 4096)
                except OSError:
                    pass
                return "wake"
            if r:
                return sys.stdin.read(1).lower()
    finally:
        if old is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old)


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
        "SERENDASH_PERF_DIR", os.path.expanduser("~/.cache/serendash/perf")),
        help="where perf-snap.sh writes captures. The dashboard reads them; it cannot record "
             "on its own because perf_event_paranoid blocks attaching to a container process "
             "without root, and making the whole dashboard run as root to get a panel is a bad "
             "trade.")
    a = ap.parse_args()
    col = not a.no_color and sys.stdout.isatty()

    prev, sz, tick, shown = None, {}, 0, []
    hist = {"mem": []}
    perf = (None, [])
    thr, tprev, tlast = [], {}, time.time()
    view, scroll = "main", 0
    hpid = host_pid(a.container)
    signal.signal(signal.SIGUSR1, _on_usr1)
    pidfile = write_pidfile(a.perf_dir)
    if not a.once:
        sys.stdout.write("\033[?25l\033[2J")
    try:
        while True:
            s = sample(a.container, a.port, a.password)
            if s is None:
                lines = [f" cannot reach {a.container}:{a.port}"]
            else:
                if tick % SLOW_EVERY == 0:
                    sz = slow(a.container, a.data)
                hist["mem"] = (hist["mem"] + [s["mem"]])[-HIST:]
                perf = perf_window(a.perf_dir)
                hpid = hpid or host_pid(a.container)
                if hpid:
                    now = time.time()
                    thr, tprev = threads(hpid, tprev, now - tlast)
                    tlast = now
                w = shutil.get_terminal_size((100, 40)).columns
                if view == "stacks":
                    nm, ls = callstacks(a.perf_dir)
                    lines = ([f"{'' if a.no_color else C['b']}call graph{'' if a.no_color else C['r']}  "
                              f"{nm or 'no captures'}  ·  q quit · s back · jk scroll", ""]
                             + ls[scroll:scroll + max(8, shutil.get_terminal_size((100, 40)).lines - 4)])
                elif view == "config":
                    cfg = psql(a.container, a.port, a.password,
                               ["select name, value, coalesce(description,'') "
                                "from duckdb_settings()"])
                    lines, scroll = config_frame(cfg[0] if cfg else [], s, col, w, scroll)
                else:
                    lines = frame(s, prev, sz, hist, perf, thr, col, w)
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
            if k in ("q", "\x1b"):
                return 0
            if k == "s":
                view = "main" if view == "stacks" else "stacks"
                scroll, shown = 0, []
            elif k == "c":
                view = "main" if view == "config" else "config"
                scroll, shown = 0, []
            elif k in ("j", "B"):
                scroll += 5
            elif k in ("k", "A"):
                scroll = max(0, scroll - 5)
    except KeyboardInterrupt:
        return 0
    finally:
        if not a.once:
            sys.stdout.write("\033[?25h\n")
            sys.stdout.flush()
        if pidfile:
            try:
                os.unlink(pidfile)
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())

"""serenedash.views"""
import re
import shutil
import textwrap
import time

from .fmt import C, COL_BAR, COL_LABEL, COL_VALUE, NOCOLOR, bar, clip, dur, human, line, spark, strip
from .hazards import HAZARDS, kernel_of


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
        ("swapped", "how much of the process the kernel has paged out to swap. Every touch of it is "
                    "a disk read, memory_limit does not count it, and it is the usual reason "
                    "`resident` sits below duckdb_memory()"),
        ("peak", "high-water RSS since serened started — what it has held at its worst, which the "
                 "current figure will not tell you"),
        ("tags", "per-tag breakdown, every pool holding anything. The bar is against the largest "
                 "pool so the small ones are still visible; the note is its share of all tagged "
                 "memory, which is the number that adds to 100"),
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
        ("vector / text / columnar / wire / alloc / kernel", "the engine a symbol is attributed "
            "to, matched on the symbol name. `other` is a symbol no pattern claimed, not an engine"),
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
          "host": "h", "doctor": "d", "legend": "l"}


# No j/k here: nothing on the main frame scrolls, so it is carried by the views that do scroll and
# the bar gets its width back. Eleven labelled keys need ~100 columns and wrapped onto a second line
# on a 96-column terminal.
KEYS = (("q", "quit"), ("s", "storage"), ("m", "memory"), ("a", "activity"), ("t", "threads"),
        ("p", "profile"), ("g", "graph"), ("c", "config"), ("h", "host"), ("d", "doctor"),
        ("l", "legend"), ("x", "mouse"))


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
           f"means — or point at one, these are what the tooltip says{c['r']}", ""]
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


def activity_frame(s, col, width, scroll, full=None):
    """The `a` view: every session and its whole statement, not the first 90 characters.

    `full` is a separately-fetched, untruncated copy of the statements — this is the one screen that
    wants them, so it is the one screen that pays for them. The per-tick sample carries only what
    the main panel can display (see `sample`), which on this deployment is the difference between
    40 KB and 1.84 MB every five seconds.
    """
    c = C if col else NOCOLOR
    W = max(70, width)
    st = s["states"]
    rows = [(stt, q, n) for stt, q, n in s["queries"] if "pg_stat_activity" not in q]
    if full:
        rows = [(stt, q, n) for stt, q, n in full if "pg_stat_activity" not in q]
    out = [f"{c['b']}activity{c['r']}  {c['dim']}"
           + "  ".join(f"{k} {v}" for k, v in sorted(st.items())) + f"{c['r']}", ""]
    if not rows:
        out.append(f"{c['dim']}no sessions{c['r']}")
    for stt, q, n in rows:
        run = stt == "active"
        size = f"  {c['dim']}{human(n)}{c['r']}" if n > 2000 else ""
        out.append(f"{(c['grn'] + '▸') if run else (c['dim'] + '·')} {stt}{c['r']}{size}")
        # Wrapped, not truncated: the interesting part of a statement is rarely in its first line,
        # and the main panel already shows the head of it.
        shown = q if len(q) >= n else q + f"  … {human(n - len(q))} more not fetched"
        for chunk in (textwrap.wrap(shown, max(30, W - 4)) or ["(no statement)"]):
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


def doctor_frame(rows, fix, col, width, scroll, msg=None):
    c = C if col else NOCOLOR
    W = max(70, width)
    mark = {"ok": (c["grn"], "ok  "), "warn": (c["yel"], "warn"),
            "fail": (c["red"], "fail"), "info": (c["dim"], "note")}
    out = [f"{c['b']}doctor{c['r']}  {c['dim']}what is working, what is missing, and what it "
           f"costs you{c['r']}", ""]
    for st, name, detail, fix in rows:
        col_, tag = mark[st]
        out.append(f"  {col_}{tag}{c['r']}  {c['b']}{name:<15}{c['r']}"
                   + textwrap.fill(detail, max(30, W - 26),
                                   subsequent_indent=" " * 24).lstrip())
        if fix:
            out += [f"        {c['cyn']}{ln}{c['r']}" for ln in
                    textwrap.wrap(fix, max(30, W - 10), initial_indent="→ ",
                                  subsequent_indent="  ")]
        out.append("")
    if fix:
        kind, arg = fix
        what = (f"registers {arg}" if kind == "register"
                else f"copies {arg} out of the container and registers it")
        out.append(f"  {c['yel']}r{c['r']} {c['dim']}{what}{c['r']}")
    if msg:
        out += ["", f"  {c['grn'] if msg[0] else c['red']}{msg[1]}{c['r']}"]
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
        return [a + " " + b for a, b in zip(left, right, strict=True)]

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
    live = [q for stt, q, _ in s["queries"] if stt == "active" and "pg_stat_activity" not in q]
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
             for stt, q, _ in ordered]

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
           f"{c['dim']}{len(rows)} settings · {c['yel']}~{c['dim']} runtime-settable "
           f"(reverts on restart) · jk move · enter details · c back · q{c['r']}", ""]
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

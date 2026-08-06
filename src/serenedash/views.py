"""serenedash.views"""
import re
import shutil
import textwrap
import time

from .anomaly import index, scan
from .fmt import C, COL_BAR, COL_LABEL, COL_VALUE, NOCOLOR, bar, clip, dur, human, line, spark, strip
from .hazards import HAZARDS, kernel_of
from .logs import counts as log_counts
from .mcplog import digest as mcp_digest
from .mcplog import failed as mcp_failed
from .mcplog import sessions as mcp_sessions
from .mcplog import pretty as mcp_pretty


def anom_colour(c, a):
    """The label colour for a row a rule fired on, or None.

    Only the label, and only a colour. The rows are already at their width — the memory notes fill
    their field exactly — so there is nowhere to put a word without taking one away, and the panel
    is not the place for the reasoning anyway. This marks the row; `m`, the tooltip and the MCP
    findings carry what was measured and why.
    """
    if a is None:
        return None
    return c["red"] if a.rule == "spike" else c["yel"]


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
        ("duckdb_temporary_files()", "how many temp files the SERVER has open, and how many "
            "bytes, against the count du found on disk. The orphan split above is inferred from "
            "file mtimes, which is circumstantial; this is the server's own answer, and 0 held "
            "against 24 present is that inference proving itself"),
        ("indexes", "how many indexes sdb_metrics reports, with their live documents, segments and "
                    "deleted count. Deleted is num_docs - num_live_docs: removed, but still held in "
                    "a segment until a consolidation drops them. Press i for the per-index rows"),
        ("engine tasks", "search refresh, compaction and cleanup tasks the server reports "
                         "running, and how many are waiting behind them. Refresh and compaction "
                         "are the two candidate explanations for the periodic CPU spikes on this "
                         "deployment, and this row is what tells them apart while one happens"),
    )),
    ("search", (
        ("search engine", "sdb_metrics — the counters SereneDB's own search engine keeps. Every "
                          "other panel measures the process, the column store or the host; this is "
                          "the engine, and it is what SereneDB is"),
        ("refresh / compaction / cleanup", "tasks currently running, and tasks waiting to run, "
                                           "server-wide rather than per index. The server's own "
                                           "words for the four counters behind them"),
        ("connections", "pg_connections and http_connections. pg_connections counts every pg-wire "
                        "client the server has, including whatever connection this dashboard is "
                        "holding while it reads — the activity panel's `sessions` excludes its "
                        "own, so the two are counting different sets and will not agree"),
        ("live docs", "num_live_docs, with num_docs beside it. The difference is documents that "
                      "were deleted and are still occupying a segment; consolidation is what "
                      "removes them"),
        ("buffered", "num_buffered_docs — 'documents buffered in the writer, not yet committed', "
                     "in the server's own description"),
        ("segments", "num_segments, and the num_files backing them. Every segment is memory-mapped, "
                     "which is why vm.max_map_count is a limit that matters here"),
        ("index size", "index_size, and this index's share of every index sdb_metrics reports. It is "
                    "the engine's own figure for its own files, not du of a directory, so it will "
                    "not match the storage panel's `search idx`"),
        ("avg commit / avg consolidation / avg cleanup", "avg_commit_time_ms, "
            "avg_consolidation_time_ms, avg_cleanup_time_ms. 'Average time of the last few', in the "
            "server's words — a recent average, not a lifetime one, so it moves. Beside each is its "
            "num_failed_* count. This deployment read 672 ms in one sample and 16,893 ms an hour "
            "later, which is the difference a lifetime average would have flattened away"),
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
        ("spill", "duckdb_memory()'s temporary_storage_bytes, per pool — WHICH pool put bytes in "
                  "the temp directory, not just that the directory has files in it. Listed by `m` "
                  "whether or not anything spilled, so an empty list is a reading rather than a "
                  "panel that never appeared"),
    )),
    ("activity", (
        ("sessions", "connected sessions by state, EXCLUDING this dashboard's own session"),
        ("▸ / ·", "active / idle. The text is the statement the server reports for that session"),
        ("statement size", "the statement's length in characters, base 1000, shown before it once "
                           "it is over 2000 — the row itself only ever carries one line of it. "
                           "68.2k is 68,209 characters, and length() is the server's, so it counts "
                           "the whole statement and not the head that was fetched"),
        ("one literal", "the largest single quoted literal or bracketed list in the statement, and "
                        "its share of the statement's characters — only in the `a` view, which is "
                        "the one that fetches whole statements. Yellow when that one literal is "
                        "over a quarter of the statement: a 1024-dim embedding sent as text is "
                        "~21,700 characters the parser re-reads on every query"),
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
        ("vector / text / parse / columnar / wire / alloc / kernel", "the engine a symbol is "
            "attributed to, matched on the symbol name. `other` is a symbol no pattern claimed, "
            "not an engine"),
        ("parse", "reading the statement TEXT - the PEG grammar's matchers. A large share means the "
                  "statements themselves are expensive to read, which usually means big literals: "
                  "a 1024-dim embedding sent as text is ~21,700 characters re-parsed on every "
                  "query. Binary bind parameters fix it. These symbols are in the duckdb:: "
                  "namespace, so they used to be counted as `columnar` and read as storage work"),
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
    ("anomalies", (
        ("a coloured label", "a rule fired on that row's history: yellow for a level change or a "
                             "steady climb, red for a single-sample excursion. The threshold "
                             "findings elsewhere compare against a fixed number; these compare "
                             "against the row's own recent past, which is the only way to see a "
                             "pool that has been growing all afternoon without being over any "
                             "limit yet. Point at the row, or press m, for what was measured"),
        ("baseline", "a median, and the spread a median absolute deviation. Both survive up to "
                     "half the window being the event itself, so a big excursion cannot inflate "
                     "the bar it has to clear — which a mean and a standard deviation would"),
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
# `search` takes i, for index: s was storage before there was a search engine on screen at all, and
# moving it would retrain the one key that is used most.
# The one map. Everything that needs "which key opens what" reads it: the key bar below, the
# terminal's dispatch, the page's key handler, the clickable boxes over the bar, and the tests.
# In bar order, because the bar is generated from it.
#
# A `view` of None is an action rather than a panel - the terminal quits and toggles its own mouse
# tracking, and a browser can do neither, which is why the served bar is filtered rather than
# copied. It advertised both for a while, along with `g` and `c`, which the page had never been
# given at all.
BINDINGS = (
    ("q", None, "quit"),
    ("f", "findings", "findings"),
    ("s", "storage", "storage"),
    ("m", "memory", "memory"),
    ("a", "activity", "activity"),
    ("t", "threads", "threads"),
    ("p", "profile", "profile"),
    ("i", "search", "search"),
    ("o", "logs", "logs"),
    ("n", "mcp", "mcp"),
    ("g", "graph", "graph"),
    ("c", "config", "config"),
    ("h", "host", "host"),
    ("l", "legend", "legend"),
    ("x", None, "mouse"),
)

# Keys that reach a view under another name. `d` was doctor for as long as there was a doctor view;
# it is the same screen now and the fingers do not know that. Off the bar on purpose: two rows for
# one screen is not documentation, it is noise.
ALIAS = {"d": "findings"}

# What a browser cannot do. Quitting closes nothing and there is no terminal mouse tracking to
# turn off, so the served bar does not offer them - the bar is the documentation, and documentation
# that lists a key which does nothing is worse than a shorter bar.
NOT_ON_THE_PAGE = ("q", "x")

# view -> key, for the "press this to go back" hint and for the served view list.
DETAIL = {view: key for key, view, _label in BINDINGS if view}

KEYS = tuple((key, label) for key, _view, label in BINDINGS)
WEB_KEYS = tuple((key, label) for key, _view, label in BINDINGS
                 if key not in NOT_ON_THE_PAGE)


def view_hint(view, nav=None, c=None):
    """What this view answers to right now, for the key bar to carry.

    Depth matters: `enter` opens a session on the mcp list and does nothing inside the call box, so
    a hint that named it in both would be wrong in one of them. Written here rather than at the
    foot of each frame because a hint that moves with the content is a hint you have to look for.
    """
    c = c or NOCOLOR
    n = nav or {}

    def keys(*pairs):
        return f"  {c['dim']}·{c['r']}  ".join(f"{c['b']}{k}{c['r']} {c['dim']}{what}{c['r']}"
                                               for k, what in pairs)

    if view == "mcp":
        if n.get("popup"):
            return keys(("j/k", "scrolls the reply"), ("esc", "closes"))
        if n.get("open") is not None:
            return keys(("j/k", "moves"), ("enter", "shows the call"), ("esc", "back"))
        return keys(("j/k", "moves"), ("enter", "opens the session"))
    if view in ("findings", "activity"):
        if n.get("open"):
            return keys(("j/k", "scrolls"), ("esc", "back"))
        opens = "reads the finding" if view == "findings" else "opens the statement"
        out = [("j/k", "moves"), ("enter", opens)]
        if view == "findings" and n.get("fixable"):
            out.append(("r", "runs the fix"))
        return keys(*out)
    if view == "logs":
        return keys(("space", "follow"), ("/", "filter"), ("j/k", "scrolls"))
    return keys(("j/k", "scroll"))


def key_to_view(extra=None):
    """{key: view} for every key that switches a view, aliases included.

    One producer, because the terminal and the page each built this themselves and drifted: the
    page was handed a map made from DETAIL alone, so `d` did nothing there while it worked in the
    terminal, and the bar advertised `g` and `c` which were in neither.
    """
    return {**{key: view for key, view, _l in BINDINGS if view}, **ALIAS, **(extra or {})}


# `setup` is what `doctor` used to be a whole view of: the dashboard's own preconditions. It is a
# kind rather than a screen because the distinction between "the tool cannot measure this" and "the
# server has a problem" is the tool's, not the reader's - both are a check that came out badly.
KINDNAME = {"storage": "storage", "memory": "memory", "setting": "setting",
            "search": "search", "trend": "trend", "setup": "setup", "other": "other"}
KINDCOL = {"storage": "cyn", "memory": "mag", "setting": "yel", "search": "grn", "trend": "blu",
           "setup": "yel"}

def qty(n):
    """A count, abbreviated base 1000. NOT human() — that is base 1024 and these are documents.

    11,216,808 live documents through human() prints 10.7M, which is the right glyph over the wrong
    arithmetic on a quantity that is not bytes.
    """
    n = float(n)
    for u in ("", "k", "M", "G"):
        if abs(n) < 1000 or u == "G":
            return f"{n:,.0f}" if not u else f"{n:.1f}{u}"
        n /= 1000
    return f"{n:.1f}G"


def msec(v):
    """A millisecond figure from sdb_metrics, in the unit that can be read at a glance.

    16893 ms is the number the server reports and 16.9s is the number anyone reads it as; both are
    the same measurement, so only the second is printed and the legend names the metric.
    """
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "?"
    return f"{v:,.0f}ms" if v < 1000 else f"{v / 1000:.1f}s"


def status(c, width, extra="", keys=KEYS):
    """The key bar, as however many lines it needs. The bindings used to hide in the config panel.

    Wrapped rather than clipped: with a view behind every panel there are ten of them, which do not
    fit one line of a 100-column terminal, and a binding you cannot see is a binding you do not have.
    Returns a list so the caller can count the rows it costs — clipping silently cost the last few
    keys, and the single line was also one column too wide for its own frame.
    """
    items = ([f"{c['b']}{k}{c['r']} {c['dim']}{v}{c['r']}" for k, v in keys]
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


def storage_frame(s, sz, host, col, width, scroll, held=None):
    """The `s` view: where the bytes on disk actually are, file by file for the temp directory.

    The main panel has room for a size and a verdict. This has room for the evidence behind the
    verdict — every temp file with its age, so "orphaned" is something you can check rather than
    something the dashboard asserts.

    `held` is temp_files_held(): (count, bytes) the server has open right now. The orphan split
    below it is inferred from mtimes against the process start time, which is sound and still
    circumstantial; the server's own count of open temp files is the other half of the argument.
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
    # The server's own count of open temp files, next to du's count on disk. Two independent
    # measurements of the same directory: 0 held against 24 present is what turns "these look
    # orphaned" into "nothing has a descriptor into any of them".
    holds = (f"  {c['dim']}·  the server holds {held[0]} of them open "
             f"({human(held[1])}){c['r']}" if held else "")
    out += ["", f"{c['cyn']}{c['b']}temp files{c['r']}  "
            + (f"{c['dim']}{len(files)} files, {len(orph)} older than this serened "
               f"({human(sum(f[1] for f in orph))} reclaimable){c['r']}{holds}" if files
               else f"{c['dim']}none — nothing has spilled{c['r']}{holds}")]
    for mtime, size, name in files[:64]:
        old = started and mtime < started
        age = dur(time.time() - mtime)
        out.append(line(c, "", human(size), " " * COL_BAR,
                        f"{c['yel'] if old else c['grn']}{'orphan' if old else 'live  '}{c['r']}  "
                        f"{c['dim']}{age:>8} old  {name[:52]}{c['r']}"))
    return out[scroll:]


def _m(d, key):
    """One sdb_metrics value as a number, 0 if it is not one.

    The table is (metric, value) text and `search()` casts what it can, so a value the server ever
    reports in another shape must not take the whole panel down with it.
    """
    try:
        return float(d.get(key, 0))
    except (TypeError, ValueError):
        return 0.0


def search_frame(sea, col, width, scroll):
    """The `i` view: sdb_metrics, per index and server-wide.

    The dashboard measured the process, the column store and the host, and had no number at all from
    the search engine — which is what SereneDB is. This deployment runs a 54.1 GiB inverted index
    over 11.2M documents in 15 segments, and none of it was on screen.

    The row that pays for the view on its own is `avg consolidation`: it read 672 ms in one sample
    and 16.9s an hour later with compaction_active at 1, and refresh-versus-compaction was the
    open question about this server's periodic CPU spikes for a week.
    """
    c = C if col else NOCOLOR
    W = max(70, width)
    if sea is None:
        # Not a degraded panel with zeros in it: every number on this screen is from one table, so
        # there is nothing left to show when that table does not answer.
        return [f"{c['b']}search engine{c['r']}  {c['yel']}sdb_metrics did not answer{c['r']}", "",
                f"  {c['dim']}every number in this view comes from that one table — an index count "
                f"of 0 drawn from an empty result would be a false panel, not a degraded one{c['r']}",
                ][scroll:]
    srv, idx = sea.get("server") or {}, sea.get("indexes") or {}
    live = sum(_m(m, "num_live_docs") for m in idx.values())
    segs = sum(_m(m, "num_segments") for m in idx.values())
    disk = sum(_m(m, "index_size") for m in idx.values())
    out = [f"{c['b']}search engine{c['r']}  {c['dim']}sdb_metrics · {len(idx)} indexes · "
           f"{qty(live)} live docs · {qty(segs)} segments · {human(disk)} on disk{c['r']}", "",
           f"{c['cyn']}{c['b']}server{c['r']}  {c['dim']}the counters that are not per index{c['r']}"]
    for kind, what in (("refresh", "reopening the index for readers"),
                       ("compaction", "merging segments together"),
                       ("cleanup", "dropping files no segment references")):
        act, pend = int(_m(srv, f"{kind}_active")), int(_m(srv, f"{kind}_pending"))
        out.append(line(c, kind, f"{act:,}", " " * COL_BAR,
                        f"{c['yel'] if pend else c['dim']}{pend:,} pending{c['r']}  "
                        f"{c['dim']}{what}{c['r']}",
                        vc=c["b"] if act else None))
    out.append(line(c, "connections", f"{int(_m(srv, 'pg_connections')):,}", " " * COL_BAR,
                    f"{c['dim']}pg-wire · {int(_m(srv, 'http_connections')):,} HTTP{c['r']}"))
    if not idx:
        out += ["", f"{c['dim']}sdb_metrics reports no per-index rows on this server{c['r']}"]
    for rel, m in sorted(idx.items(), key=lambda kv: -_m(kv[1], "index_size")):
        nd, nl, size = _m(m, "num_docs"), _m(m, "num_live_docs"), _m(m, "index_size")
        out += ["", f"{c['cyn']}{c['b']}index {rel}{c['r']}  "
                f"{c['dim']}sdb_metrics relation_id{c['r']}"]
        # Every bar in this block divides by something named on its own row: live documents by the
        # documents in the index, bytes by the bytes of every index. Nothing divides by "the biggest
        # one on screen", which is how the thread bars were wrong for a month.
        out.append(line(c, "live docs", f"{nl:,.0f}", bar(nl / (nd or 1), COL_BAR,
                                                          c["grn"] if col else ""),
                        f"{c['dim']}of {nd:,.0f} in the index  ·  {nd - nl:,.0f} deleted{c['r']}",
                        vc=c["b"]))
        out.append(line(c, "buffered", f"{_m(m, 'num_buffered_docs'):,.0f}", " " * COL_BAR,
                        f"{c['dim']}in the writer, not yet committed{c['r']}"))
        out.append(line(c, "segments", f"{_m(m, 'num_segments'):,.0f}", " " * COL_BAR,
                        f"{c['dim']}{_m(m, 'num_files'):,.0f} files back the index{c['r']}"))
        out.append(line(c, "index size", human(size),
                        bar(size / (disk or 1), COL_BAR, c["blu"] if col else ""),
                        f"{c['dim']}{size / (disk or 1) * 100:.0f}% of {human(disk)} across "
                        f"{len(idx)} indexes{c['r']}"))
        for label, metric, failed, noun in (
                ("avg commit", "avg_commit_time_ms", "num_failed_commits", "commits"),
                ("avg consolidation", "avg_consolidation_time_ms", "num_failed_consolidations",
                 "consolidations"),
                ("avg cleanup", "avg_cleanup_time_ms", "num_failed_cleanups", "cleanups")):
            nf = int(_m(m, failed))
            # "average time of the last few", in the server's own words — so this is a recent
            # average and not a lifetime one, and it is why the number moves between samples.
            out.append(line(c, label, msec(_m(m, metric)), " " * COL_BAR,
                            f"{c['dim']}of the last few  ·  {c['r']}"
                            f"{c['red'] if nf else c['dim']}{nf:,} failed {noun}{c['r']}",
                            vc=c["b"] if label == "avg consolidation" else None))
    # Clipped to the terminal here rather than row by row: every row above is built to say what it
    # has to say, and only the screen decides where that stops. clip(), because these rows are full
    # of escapes and a byte slice would cut one in half and eat the row after it — and to W - 1,
    # because a clipped row spends one more real column on the ellipsis (mkbox has the same -1).
    return [clip(ln, W - 1) for ln in out][scroll:]


# A quoted literal or a bracketed list. `'[^']*'` rather than anything that understands a doubled
# quote: the alternation form is a nested quantifier that backtracks catastrophically on an
# unterminated string, and splitting one escaped literal into two only ever understates a share.
_LITERAL = re.compile(r"'[^']*'|\[[^\[\]]*\]", re.S)


def biggest_literal(q):
    """(length, head) of the largest quoted literal or bracketed list in a statement.

    Spans only until the winner is known: this runs over every statement the view shows on every
    redraw, and the statements on this deployment are 68 KB each.

    It exists for one finding. A RAGFlow query here is 68,209 characters, 21,684 of them (31.8%) a
    single 1024-dimension embedding sent as a text literal — three copies of it, in fact. A panel
    showing the first 90 characters of that statement cannot say so, and it took a vendor reading a
    screenshot of this dashboard to spot it.
    """
    best = max((m.span() for m in _LITERAL.finditer(q)), key=lambda sp: sp[1] - sp[0], default=None)
    if best is None:
        return 0, ""
    lo, hi = best
    return hi - lo, q[lo:lo + 48]


def activity_frame(s, col, width, scroll, full=None, sel=0, open_=False, height=40,
                   anchors=None):
    """The `a` view: every session, collapsed, and the whole statement of the one you open.

    Collapsed by DEFAULT. This view used to wrap every statement in full on the theory that the
    main panel already truncates them - and on this deployment a hybrid-search statement is 68 KB,
    31% of it one float literal, so three of them buried the session list they belonged to. A list
    you can walk answers "what is running" without answering "what is in it" first.

    `full` is a separately-fetched, untruncated copy - this is the one screen that wants it, so it
    is the one screen that pays for it. The per-tick sample carries only what the main panel can
    display (see `sample`), which here is the difference between 40 KB and 1.84 MB every 5 seconds.
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
        return out[scroll:]
    sel = max(0, min(sel, len(rows) - 1))
    if open_:
        return out[:1] + _statement(rows[sel], c, W, height, scroll)

    room = max(1, height - len(out) - 1)
    start = max(0, min(sel - room // 2, len(rows) - room))
    for i, (stt, q, n) in enumerate(rows[start:start + room], start=start):
        run = stt == "active"
        mark = f"{c['b']}›{c['r']}" if i == sel else " "
        if anchors is not None:
            anchors.append((len(out), i))
        state = f"{c['grn']}▸ active{c['r']}" if run else f"{c['dim']}· {stt:<6}{c['r']}"
        lit, _preview = biggest_literal(q) if n > 2000 and len(q) >= n else (0, "")
        # The size and the literal share are the two things that decide whether a statement is
        # worth opening, so they go on the collapsed row rather than inside it.
        size = f"{c['dim']}{n:,} chars{c['r']}" if n > 2000 else f"{c['dim']}{n:,}{c['r']}"
        share = ""
        if lit:
            lc = c["yel"] if lit > n / 4 else c["dim"]
            share = f"{lc}{lit / n * 100:.0f}% one literal{c['r']}"
        head = " ".join(str(q).split())
        out.append(f" {mark}{state} {size:<24} {share:<26} "
                   f"{'' if run else c['dim']}{clip(head, max(20, W - 60))}{c['r']}")
    if start:
        out.insert(2, f"  {c['dim']}… {start} above{c['r']}")
    return out


def _statement(row, c, W, height, scroll):
    """One statement, whole. Wrapped rather than truncated - the interesting part of a statement is
    rarely in its first line, which is why the main panel's head is not enough."""
    stt, q, n = row
    run = stt == "active"
    head = f"  {(c['grn'] + '▸ active') if run else (c['dim'] + '· ' + stt)}{c['r']}"
    lit, preview = biggest_literal(q) if n > 2000 and len(q) >= n else (0, "")
    if n > 2000:
        # Characters, not bytes - human() is base 1024 and this is text the server measured with
        # length(). One denominator for the row: every figure on it divides by these characters.
        head += f"  {c['dim']}{n:,} chars{c['r']}"
        if lit:
            # A quarter is the comparison, and it is named rather than left as a colour.
            lc = c["yel"] if lit > n / 4 else c["dim"]
            head += (f"  {lc}{lit:,} ({lit / n * 100:.0f}%) of them in one literal"
                     f"{', over a quarter' if lit > n / 4 else ''}{c['r']}")
    out = ["", head, ""]
    if lit > n / 4:
        # Only for the case the line above flagged, and the ellipsis says the literal runs on
        # rather than that this line was cut - two different claims about the same row.
        txt = "that literal starts " + preview + ("…" if lit > len(preview) else "")
        out.append(f"  {c['dim']}{clip(txt, max(30, W - 6))}{c['r']}")
        out.append("")
    shown = q if len(q) >= n else q + f"  … {human(n - len(q))} more not fetched"
    body = textwrap.wrap(shown, max(30, W - 6)) or ["(no statement)"]
    room = max(3, height - len(out) - 3)
    scroll = max(0, min(scroll, max(0, len(body) - room)))
    out += [f"  {'' if run else c['dim']}{chunk}{c['r']}" for chunk in body[scroll:scroll + room]]
    if len(body) > scroll + room:
        out.append(f"  {c['dim']}… {len(body) - scroll - room} more lines - j/k scrolls{c['r']}")
    return out


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
    found = index(hist)
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
                        lc=anom_colour(c, found.get(key)), vc=c["b"]))
    out += ["", f"{c['cyn']}{c['b']}pools{c['r']}  {c['dim']}duckdb_memory(), largest first{c['r']}"]
    for tag, v in tags:
        h = (hist.get(f"t:{tag}") or [])[-room:]
        lc = None if v else c["dim"]
        out.append(line(c, tag, human(v), bar(v / top, COL_BAR, c["cyn"] if col and v else ""),
                        f"{c['dim']}{f'{v / tot * 100:.1f}% of tagged':<20}{c['r']}"
                        f"{c['cyn'] if v else c['dim']}"
                        f"{spark(h, top=top) if h else ''}{c['r']}",
                        lc=lc or anom_colour(c, found.get(f"t:{tag}"))))
    # temporary_storage_bytes, off the same duckdb_memory() row as the pool above it. The storage
    # panel can say the temp directory holds files; only this says which pool put them there, and
    # the two are not the same claim — du sees a killed query's wreckage exactly as it sees a live
    # spill, and this does not.
    #
    # Drawn whether or not anything spilled. A section that appears only when it is non-empty makes
    # "nothing spilled" and "this dashboard never looked" the same picture.
    spill = sorted((s.get("memspill") or {}).items(), key=lambda kv: -kv[1])
    stot = sum(v for _, v in spill) or 1
    out += ["", f"{c['cyn']}{c['b']}spill{c['r']}  {c['dim']}temporary_storage_bytes, per pool"
                f"{c['r']}"]
    for tag, v in spill:
        out.append(line(c, tag, human(v), bar(v / stot, COL_BAR, c["red"] if col else ""),
                        f"{c['dim']}{v / stot * 100:.0f}% of the {human(stot)} spilled{c['r']}",
                        lc=c["red"]))
    if not spill:
        out.append(f"{' ' * COL_LABEL}{c['dim']}no pool reports any{c['r']}")
    # The main frame can only colour a label — it has no line to spare. Here there is room to say
    # what was actually measured, which is the part that makes a highlight arguable rather than
    # something to be believed.
    hits = scan(hist)
    if hits:
        out += ["", f"{c['yel']}{c['b']}anomalies{c['r']}  {c['dim']}against each series' own "
                    f"recent past, not a threshold{c['r']}"]
        for a in hits:
            for i, part in enumerate(textwrap.wrap(f"{a.label()} — {a.detail}", max(40, W - 26))):
                out.append(f"  {c['yel']}{a.name if i == 0 else '':<22}{c['r']} {c['dim']}{part}"
                           f"{c['r']}")
    return out[scroll:]


def logs_frame(rows, source, why, needle, col, width, scroll, height=40, follow=True,
               typing=False):
    """The `o` view: the server's own log, newest last, filtered.

    Newest LAST, like `tail -f` and unlike most log UIs. You read a log to find out what happened
    just now, and the eye goes to the bottom of a terminal - putting the newest line at the top means
    reading time backwards.

    The header says which source answered. That is not decoration: an empty log means one thing when
    it came from `docker logs` and something else entirely when nothing answered at all.
    """
    c = C if col else NOCOLOR
    W = max(70, width)
    lv, ty = log_counts(rows)
    head = f"{c['b']}log{c['r']}  {c['grn'] if follow else c['yel']}"
    head += f"{'following' if follow else 'paused'}{c['r']}  "
    head += f"{c['dim']}space follow  / filter{c['r']}  "
    if source:
        head += f"{c['dim']}{source}{c['r']}  {c['dim']}·{c['r']}  {len(rows)} lines"
        if lv:
            def _lc(k):
                return (c["red"] if k.startswith(("ERR", "FATAL")) else
                        c["yel"] if k.startswith("WARN") else c["dim"])
            head += "  " + "  ".join(f"{_lc(k)}{n} {k.lower()}{c['r']}"
                                     for k, n in sorted(lv.items()))
        if ty:
            head += f"  {c['dim']}·{c['r']}  " + " ".join(f"{c['cyn']}{k}{c['r']}" for k in sorted(ty))
    else:
        head += f"{c['yel']}no source answered{c['r']}"
    out = [head]
    if typing:
        # A block for a cursor, because the terminal's real one is parked wherever the last write
        # left it and this editor does not move it.
        out.append(f"  {c['yel']}/{c['r']}{c['b']}{needle}{c['r']}\u2588  "
                   f"{c['dim']}{len(rows)} matching · enter keeps it, esc drops it{c['r']}")
    elif needle:
        out.append(f"  {c['yel']}/{c['r']}{c['b']}{needle}{c['r']}  "
                   f"{c['dim']}{len(rows)} matching{c['r']}")
    out.append("")
    if why:
        out += [f"  {c['dim']}{ln}{c['r']}" for ln in textwrap.wrap(why, max(40, W - 4))]
        return out[scroll:]
    if not rows:
        out.append(f"  {c['dim']}nothing matched /{needle}{c['r']}" if needle else
                   f"  {c['dim']}the log is empty. That is a fact about the log, not about the "
                   f"server - serened logs at info by default{c['r']}")
        return out[scroll:]
    lvcol = {"ERROR": c["red"], "FATAL": c["red"], "WARN": c["yel"], "WARNING": c["yel"],
             "DEBUG": c["dim"], "TRACE": c["dim"]}
    # The window is anchored to the END of the buffer, not the start. A log is read from the bottom,
    # and slicing from the top would show the oldest lines and scroll the reader away from the new
    # ones on every tick - the opposite of what tailing is for. `scroll` counts lines back from the
    # newest, so 0 is "following".
    room = max(1, height - len(out) - 3)
    start = max(0, len(rows) - room - scroll)
    shown = rows[start:start + room]
    if start > 0:
        out.append(f"  {c['dim']}… {start} older line{'s' if start != 1 else ''} above{c['r']}")
    for when, typ, lvl, msg in shown:
        mark = lvcol.get(lvl.upper(), c["grn"] if lvl == "INFO" else c["dim"])
        stamp = f"{c['dim']}{when[5:] if when else ' ' * 14:<14}{c['r']}"
        kind = f"{c['cyn']}{(typ or '')[:10]:<10}{c['r']}"
        out.append(f"  {stamp} {mark}{lvl[:5]:<5}{c['r']} {kind} "
                   f"{clip(msg, max(20, W - 36))}")
    behind = len(rows) - (start + len(shown))
    if behind > 0:
        out.append(f"  {c['yel']}… {behind} newer below - space follows{c['r']}")
    return out


NOSQL = {"db": "", "size": 0, "wal": 0, "mem": 0, "memlimit": 0, "blocks": (0, 0, 0, 0),
         "memtags": [], "memspill": {}, "states": {}, "queries": [], "settings": {}, "t": 0}


def frame(s, prev, sz, hist, perf, thr, tcpu, host, col, width, height=40, sql_why=None,
          sea=None, held=None):
    """The main screen. `s` is None when SQL is unavailable; `sql_why` is sql_status's (reason, fix).

    Named `sql_why` and not `why` because the config panel below already binds `why` per hazard, and
    a parameter this function overwrites halfway through is a parameter that reads correctly and is
    not — the same shape as the local `bar` that once shadowed the imported one.

    Without a connection this used to be a single line saying it could not reach the server, which
    threw away everything that does not need one — the whole threads panel, the profile, the host,
    and every finding derived from them. The dashboard is running on the machine; /proc, du and the
    perf captures are all still there, and a server that will not accept a connection is exactly
    when the host-side numbers are worth looking at.

    `sea` is search() and `held` is temp_files_held(), both optional and both refreshed on the data
    tick — never on a redraw. Each buys exactly two rows and one tail here; the rest of what they
    carry is behind `i` and `s`.
    """
    c = C if col else NOCOLOR
    nosql = s is None
    if nosql:
        s = dict(NOSQL)
    found = index(hist)
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
    # The database and WAL sizes are the server's own; the directory rows below are du and do not
    # need it. Only this row is dropped when there is no connection.
    rows = [] if nosql else [line(c, "database", human(s["size"]), " " * COL_BAR,
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
            #
            # `server holds N` is the second measurement of the same directory: du counted the files
            # and duckdb_temporary_files() says how many of them the server has open. 0 held against
            # 24 present is what turns an inference from mtimes into something checked from inside.
            holds = f"{c['dim']}server holds {held[0]}{c['r']}  " if held else ""
            rows.append(line(c, "orphaned", human(orph_b),
                             bar(orph_b / tot, COL_BAR, c["yel"] if col else ""),
                             f"{c['yel']}{orph_n} old temp files{c['r']}  {holds}"
                             f"{c['dim']}{orph_b / tot * 100:.0f}% reclaimable{c['r']}",
                             vc=c["yel"]))

    # ── search ──────────────────────────────────────────────────────────────────────────────────
    #
    # Two rows, always two, folded into the panel that already carries `search idx`: the store's
    # directory sizes and the engine's own counts of what is in that directory belong next to each
    # other, and a box of its own costs two more borders than an 80x24 terminal has. `i` opens the
    # per-index rows behind these. Held apart from `rows` until the budget below has decided whether
    # they fit — on an 80x30 terminal two more pinned rows cost four, because the pair is stacked.
    searchrows = []
    if not nosql:
        srv = (sea or {}).get("server") or {}
        idx = (sea or {}).get("indexes") or {}
        kinds = ("refresh", "compaction", "cleanup")
        if sea is None:
            # Still two rows. A panel whose height depends on whether one extra query answered would
            # move everything below it the first time it did not, which is the same complaint as a
            # frame that resizes when a query ends.
            searchrows += [line(c, "indexes", "?", " " * COL_BAR,
                                f"{c['dim']}sdb_metrics did not answer{c['r']}"), ""]
        else:
            live = sum(_m(m, "num_live_docs") for m in idx.values())
            dead = sum(_m(m, "num_docs") - _m(m, "num_live_docs") for m in idx.values())
            segs = sum(_m(m, "num_segments") for m in idx.values())
            searchrows.append(line(c, "indexes", str(len(idx)), " " * COL_BAR,
                                   f"{c['dim']}{qty(live)} live docs · {qty(segs)} segments · "
                                   f"{qty(dead)} deleted{c['r']}", vc=c["b"] if idx else None))
            act = sum(int(_m(srv, f"{k}_active")) for k in kinds)
            pend = sum(int(_m(srv, f"{k}_pending")) for k in kinds)
            # Which ones, when there is a which. Idle, the tail names the three counters it is the
            # total of, so the row says what it is watching rather than only that it is zero.
            busy = "  ".join(f"{k} {int(_m(srv, f'{k}_active'))}+{int(_m(srv, f'{k}_pending'))}"
                             for k in kinds if _m(srv, f"{k}_active") or _m(srv, f"{k}_pending"))
            searchrows.append(line(c, "engine tasks", str(act), " " * COL_BAR,
                                   f"{c['yel'] if pend else c['dim']}{pend} pending{c['r']}  "
                                   f"{c['dim']}{busy or 'refresh, compaction, cleanup'}{c['r']}",
                                   vc=c["yel"] if act else None))
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
                                c["mag"], s["memlimit"]),
                          lc=anom_colour(c, found.get("mem")), vc=c["b"]))
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
                          lc=anom_colour(c, found.get("rss")),
                          vc=c["yel"] if swap else None))
        # Both unconditional once /proc has answered at all. Emitting `swapped` only when non-zero
        # made the panel one row taller the moment the kernel first paged something out, which is
        # both a jump and the worst moment to move the rows under someone.
        mrows.append(line(c, "swapped", human(swap), bar(swap / max(rss + swap, 1), COL_BAR,
                                                         (c["red"] if swap else c["dim"]) if col
                                                         else ""),
                          trace("swap", f"{c['red'] if swap else c['dim']}paged out{c['r']}",
                                c["red"], max(rss + swap, 1)),
                          lc=anom_colour(c, found.get("swap")),
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
                        c["cyn"], top), lc=anom_colour(c, found.get(f"t:{tag}")))
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
    # The head is all that was fetched, so the row itself cannot show how long the statement is —
    # length() rides along beside it for exactly this. `68.2k` in front of a one-line row is the
    # difference between a query and a query carrying three copies of a 21,684-character embedding,
    # and `a` is where the split between statement and literal is measured.
    abody = []
    for stt, q, n in ordered:
        mark = f"{(c['grn'] + '▸') if stt == 'active' else (c['dim'] + '·')}{c['r']} "
        size = f"{c['dim']}{qty(n)}{c['r']} " if n > 2000 else ""
        body = (clip(q, max(20, WIDE - len(strip(size)))) if q
                else f"({stt}, no statement)")
        abody.append(f"{mark}{size}{'' if stt == 'active' else c['dim']}{body}{c['r']}")

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

    # Panels that need a connection say so, in place, and the rest of the frame is unaffected. Each
    # keeps its box rather than disappearing: a panel that vanishes reads as "nothing to report",
    # which is the opposite of what is true here, and a frame whose shape depends on whether the
    # password is set is a frame you have to re-read every time.
    if nosql:
        reason, fix = sql_why or ("no connection", "")
        note = [f"{c['yel']}{reason}{c['r']}",
                *[f"{c['dim']}{ln}{c['r']}" for ln in textwrap.wrap(fix, max(30, WIDE))]]
        # Storage and memory keep the rows that never needed the server: the directory sizes are du
        # and `resident`/`swapped` are /proc, so the panels lose `database`, `blocks`, `in use` and
        # the pools and keep the rest. Activity and config are replaced whole — every number in them
        # is the server's. `sessions 0` and `nothing running` drawn off an empty result would not be
        # a degraded panel, it would be a false one.
        srows, mrows = note + srows, note + mrows
        ahead, crows = note, note
        mtags, abody = [], []

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
                  f"{c['r']}", lc=anom_colour(c, found.get("cpu")), vc=c["b"]),
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
    def floor_t(keep_sea):
        # The search rows are two more PINNED rows, and stacked they cost four - so whether they fit
        # is a budget question, not something to append and hope. 80x30 overran by exactly one line
        # with them unconditional.
        return max(len(srows) + (len(searchrows) if keep_sea else 0), len(mrows) + 3)

    floor_bot = max(len(crows), len(hrows))

    def cost(pin, keep_cfg, keep_host, keep_cyc, keep_sea):
        botn = keep_cfg + keep_host
        th, bh = max(pin, floor_t(keep_sea)) + 2, max(pin, floor_bot) + 2
        top = th if pair else 2 * th
        bot = bh if (pair and botn == 2) else botn * bh
        return top + bot + (0 if keep_cyc else -(2 + len(phead)))

    # Search down for the tallest arrangement that still leaves the three list panels three rows
    # each: shrink the pinned rows first, and only then drop `host` and `config`. Those two are
    # context — slow-moving, and `c` shows all of it in full — while the panels above them are the
    # reason the dashboard is open. On an 80x24 terminal seven box frames alone are 14 of the rows,
    # so on the smallest screens something has to go, and it should not be live data.
    # Order of sacrifice: the two search rows, then pinned padding, then `host`, then `config`, then
    # `profile` last. Profile goes only at the very end because it is live data — but it is the one
    # live panel that depends on an external capture and has a whole view of its own behind `s`, so
    # on a screen too small for everything it is the one that costs least to lose.
    #
    # The search rows go first, and the ladder below them is exactly the one that was here before
    # them, so no terminal loses a panel it used to get in order to gain two rows. They cost more
    # than they look: the two pinned panels share one height, so stacked they are four rows for two,
    # and at 120x45 keeping them meant giving up the whole config panel — seven rows carrying five
    # predicates — for a net three. Every figure in them is behind `i` in full.
    plans = ([(p, 1, 1, 1, 1) for p in (5, 4, 3, 2)] + [(p, 1, 1, 1, 0) for p in (5, 4, 3, 2)]
             + [(p, 1, 0, 1, 0) for p in (4, 3, 2)]
             + [(2, 0, 0, 1, 0), (2, 0, 0, 0, 0)])
    # Two passes: first insisting on three rows per list, then accepting one. The fixed panels have
    # a hard floor now that they never truncate — storage alone is six rows, and stacked on a narrow
    # 24-line terminal the pair is 16 before anything else is drawn — so without the second pass the
    # frame gave up and ran off the bottom, which loses the top of the screen rather than the least
    # important row of a list.
    pin, keep_cfg, keep_host, keep_cyc, keep_sea = next(
        (pl for need in (9, 3) for pl in plans if height - overhead - cost(*pl) >= need),
        plans[-1])
    if not keep_cyc:
        overhead -= 2 + len(phead)
        phead, psyms = [], []
    if keep_sea:
        srows = srows + searchrows

    top_n, bot_n = max(pin, floor_t(keep_sea)), max(pin, floor_bot)
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
    body = max(0, height - len(keybar))
    # The invariant, enforced rather than aimed at. The plan ladder above tries to fit the content
    # to the terminal, but below about 26 lines nothing fits: storage and memory stacked are sixteen
    # rows with their borders before activity and threads get one each, so no amount of dropping
    # panels helps and the ladder's last plan still overran — 27 lines into an 80x24 terminal, and
    # 27 into a 70x20. Overrunning is the worst outcome available: the terminal scrolls, so the TOP
    # of the frame is what is lost, along with the key bar. Cutting the bottom instead costs a
    # border and keeps the keys on the last line. A clipped box is ugly; a scrolled frame is unusable.
    if len(L) > body:
        L = L[:body]
    L += [""] * max(0, body - len(L))
    L.extend(keybar)
    # The other half of the same invariant. `W = max(70, width)` gives the layout a floor to compute
    # against, which is deliberate - the grid stops meaning anything below that - but it also means
    # a 60-column terminal was handed 70-column rows and wrapped every one of them, turning a
    # 15-line frame into 25 wrapped lines and undoing the height work above. Clip on the way out,
    # and only when it can bite, so the normal case pays nothing.
    # `width - 1`, because clip() spends a column on its ellipsis when it actually truncates - the
    # same reason mkbox asks it for `iw - 2` to fill `iw - 1`. Asking for `width` yields `width + 1`
    # on any line that gets cut, which is the invariant failing by one and looking like it passed.
    return [clip(ln, width - 1) for ln in L] if width < W else L


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


def mcp_frame(rows, live, col, width, scroll, sel=0, height=40, open_pid=None, call_sel=-1,
              popup=False):
    """The `n` view, at whichever of its three depths is open.

    Sessions, then that session's calls, then one call in full. Three levels because they answer
    three questions and only the first fits on a screen: which agents are talking to this
    deployment, what one of them asked, and what exactly it was told. The last is the point of the
    whole view - a model read `status()` here and reported three conclusions the findings do not
    support - and a reply is thousands of characters, so it gets its own frame rather than a corner
    of one.
    """
    c = C if col else NOCOLOR
    if open_pid is not None:
        mine = [r for r in rows if (r.get("pid") or 0) == open_pid]
        who = next((s for s in mcp_sessions(rows, live) if s["pid"] == open_pid), None)
        if popup and mine:
            return _mcp_call(mine[max(0, min(call_sel if call_sel >= 0 else len(mine) - 1,
                                             len(mine) - 1))], who, c, width, height, scroll)
        return _mcp_calls(mine, who, c, width, height, call_sel, scroll)
    return _mcp_sessions(mcp_sessions(rows, live), rows, c, width, height, sel, scroll)


def _mcp_sessions(sess, rows, c, width, height, sel, scroll):
    """One row per session, with enough beside it to tell them apart.

    Which is the whole difficulty: MCP over stdio carries no client identity, four `claude`
    sessions look identical from /proc, and they are four different conversations. So the row
    carries what does distinguish them - pid, whether it is still live, how long it has been
    connected, what it has actually asked and when.
    """
    W = max(70, width)
    live_n = sum(1 for s in sess if s["live"])
    errs = sum(s["errors"] for s in sess)
    head = f"{c['b']}mcp{c['r']}  {len(sess)} session{'s' if len(sess) != 1 else ''}"
    if sess:
        head += (f"  {c['dim']}·{c['r']}  {c['grn']}{live_n} live{c['r']}"
                 f"  {c['dim']}·{c['r']}  {len(rows)} call{'s' if len(rows) != 1 else ''}")
        if errs:
            head += f"  {c['dim']}·{c['r']}  {c['red']}{c['b']}{errs} failed{c['r']}"
    out = [head, ""]
    if not sess:
        out += [f"  {c['dim']}{ln}{c['r']}" for ln in textwrap.wrap(
            "No agent has connected. Each client session spawns its own serenedash-mcp and it "
            "exits with the session, so this fills in as soon as one starts - and a server that "
            "connected without asking anything shows up here too.", max(40, W - 4))]
        return out[scroll:]
    room = max(1, height - len(out) - 1)      # just a possible "… above"; the hint is on the bar
    sel = max(0, min(sel, len(sess) - 1))
    start = max(0, min(sel - room // 2, len(sess) - room))
    for i, s in enumerate(sess[start:start + room], start=start):
        mark = f"{c['b']}›{c['r']}" if i == sel else " "
        state = f"{c['grn']}live{c['r']}" if s["live"] else f"{c['dim']}gone{c['r']}"
        # "no calls" is not "0 calls": one is an agent that connected and asked nothing, which is
        # a state worth seeing, and the other reads like a measurement of nothing.
        what = (f"{s['calls']} call{'s' if s['calls'] != 1 else ''}" if s["calls"]
                else f"{c['dim']}asked nothing{c['r']}")
        when = (time.strftime("%H:%M:%S", time.localtime(s["last"])) if s["last"]
                else (f"up {dur(s['uptime_s'])}" if s["uptime_s"] is not None else ""))
        tools = " ".join(f"{t}{'×' + str(n) if n > 1 else ''}"
                         for t, n in sorted(s["tools"].items(), key=lambda kv: -kv[1])[:4])
        err = (f" {c['red']}{c['b']}{s['errors']} failed{c['r']}" if s["errors"]
               else " " * (len(str(s["errors"])) + 8))
        out.append(f" {mark}{state} {c['dim']}pid{c['r']} {s['pid']:<8} "
                   f"{c['cyn']}{clip(s['client'] or '?', 30):<30}{c['r']} {what:<22}{err} "
                   f"{c['dim']}{when:<9} {clip(tools, max(6, W - 82))}{c['r']}")
    if start:
        out.insert(2, f"  {c['dim']}… {start} above{c['r']}")
    return out


def _mcp_calls(mine, who, c, width, height, call_sel, scroll):
    """Every call one session made. Truncated per line - enter opens the one under the cursor."""
    W = max(70, width)
    title = f"{c['b']}mcp{c['r']}  {c['dim']}pid{c['r']} {who['pid'] if who else '?'}  "
    title += f"{c['cyn']}{(who or {}).get('client') or '?'}{c['r']}"
    if who:
        title += (f"  {c['dim']}·{c['r']}  {who['calls']} call"
                  f"{'s' if who['calls'] != 1 else ''}  {c['dim']}·{c['r']}  "
                  f"{human(who['bytes'])} returned  {c['dim']}·{c['r']}  "
                  f"{'live' if who['live'] else 'gone'}")
        if who["errors"]:
            title += f"  {c['dim']}·{c['r']}  {c['red']}{c['b']}{who['errors']} failed{c['r']}"
    out = [title, ""]
    if not mine:
        out.append(f"  {c['dim']}this session has not asked anything yet{c['r']}")
        return out
    room = max(1, height - len(out) - 1)      # just a possible "… earlier"
    sel = len(mine) - 1 if call_sel < 0 else max(0, min(call_sel, len(mine) - 1))
    start = max(0, min(sel - room // 2, len(mine) - room))
    for i, r in enumerate(mine[start:start + room], start=start):
        mark = f"{c['b']}›{c['r']}" if i == sel else " "
        ms = r.get("ms") or 0
        mc = c["red"] if ms > 3000 else c["yel"] if ms > 800 else c["dim"]
        # Two marker columns, not one. A failure gets a glyph as well as a colour - colour alone
        # is invisible to a reader scanning the left edge, and gone entirely with --no-color or in
        # a pasted screenshot - but sharing one column with the cursor meant the mark disappeared
        # exactly when you selected the failed row you were looking for.
        bad = mcp_failed(r)
        okc = c["red"] if bad else c["r"]
        mark += f"{c['red']}{c['b']}✗{c['r']}" if bad else " "
        what = mcp_digest(r)
        if r.get("args"):
            # On a FAILED call the reason comes first. Argument-then-result is the right order for
            # a call that worked, but a 120-character SELECT pushed "Table with name
            # pg_compression does not exist!" off the end of the line - which is the whole content
            # of a failure, and the reader was left with "query failed".
            what = f"{what} ← {r['args']}" if bad else f"{r['args']} → {what}"
        out.append(f"{mark}{c['dim']}{time.strftime('%H:%M:%S', time.localtime(r.get('t', 0)))}"
                   f"{c['r']} {c['cyn']}{(r.get('tool') or '?'):<10}{c['r']} {mc}{ms:>6.0f}ms{c['r']}"
                   f" {c['dim']}{human(r.get('bytes') or 0):>7}{c['r']} "
                   f"{okc}{clip(what, max(10, W - 42))}{c['r']}")
    if start:
        out.insert(2, f"  {c['dim']}… {start} earlier{c['r']}")
    return out


def _mcp_call(r, who, c, width, height, scroll):
    """One call in full, in a box: what was asked, and every line of what came back.

    Scrollable, because this is the only place the reply is not truncated for space - the stored
    copy is capped at 4000 characters and that cap is stated when it bites, rather than the text
    just stopping.
    """
    W = max(70, width)
    inner = W - 4
    body = [f"{c['dim']}when{c['r']}   {time.strftime('%H:%M:%S', time.localtime(r.get('t', 0)))}"
            f"   {c['dim']}took{c['r']} {r.get('ms', 0):.0f}ms"
            f"   {c['dim']}returned{c['r']} {human(r.get('bytes') or 0)}"
            f"   {c['dim']}client{c['r']} {(who or {}).get('client') or r.get('client') or '?'}"]
    if r.get("args"):
        body += ["", f"{c['b']}arguments{c['r']}"]
        body += [f"  {x}" for x in textwrap.wrap(r["args"], inner - 2)]
    body += ["", f"{c['b']}reply{c['r']}"
             + (f"   {c['dim']}stored copy, first {human(len(r.get('reply') or '')) }"
                f" of {human(r.get('bytes') or 0)}{c['r']}"
                if (r.get("reply") or "").endswith("…") else "")]
    lines = mcp_pretty(r, inner - 2)
    room = max(1, height - len(body) - 4)
    scroll = max(0, min(scroll, max(0, len(lines) - room)))
    body += [f"  {c['red'] if r.get('error') else c['r']}{x}{c['r']}"
             for x in lines[scroll:scroll + room]]
    if len(lines) > scroll + room:
        body.append(f"  {c['dim']}… {len(lines) - scroll - room} more lines - j/k scrolls{c['r']}")
    title = f" {r.get('tool') or '?'} "
    out = [f"{c['dim']}┌─{c['r']}{c['yel']}{c['b']}{title}{c['r']}{c['dim']}"
           + "─" * max(0, inner - len(title) - 1) + f"┐{c['r']}"]
    for x in body:
        pad = max(0, inner - len(strip(x)) - 1)
        out.append(f"{c['dim']}│{c['r']} {x}{' ' * pad}{c['dim']}│{c['r']}")
    out.append(f"{c['dim']}└{'─' * inner}┘{c['r']}")
    return out


def mcp_counts(rows):
    """{tool: (calls, total_ms)}. Here rather than imported so the view has one source for both."""
    out = {}
    for r in rows:
        n, ms = out.get(r.get("tool", "?"), (0, 0.0))
        out[r.get("tool", "?")] = (n + 1, ms + (r.get("ms") or 0))
    return out


# The keys that move something, as opposed to the ones that switch view. Named once so the terminal
# and the page cannot end up answering to different sets - the page answered to none of them, which
# is how j/k came to do nothing there while working in the terminal.
# "enter" and "esc" are spelled out as well as sent raw: PAGE is an ordinary Python string, so
# a carriage return written into the JS becomes a REAL one in the served text and breaks the
# string literal it sits in - which is how the page came to throw on load. Names travel safely.
NAV_KEYS = ("j", "k", "down", "up", "pgup", "pgdn", "end", "home", "enter", "esc",
            "\r", "\n", "\x1b")


def mcp_nav(nav, key, rows, live=()):
    """Apply one key to the mcp view's position. Returns a NEW dict; never mutates.

    One reducer for both front ends. The terminal owns its keyboard and the page has to ask the
    server, but what a key MEANS is the same question in both, and two copies of an answer that
    subtle diverge on the first change to either.

    `nav` is {scroll, sel, open, call, popup}. Esc unwinds one level and returns None for `open`
    only when there is nothing left to close, so the caller can tell "I handled it" from "this Esc
    is yours" - which is what keeps Esc leaving the view when nothing is open.
    """
    n = {"scroll": 0, "sel": 0, "open": None, "call": -1, "popup": False, **(nav or {})}
    key = {"enter": "\r", "esc": "\x1b"}.get(key, key)
    step = 1 if key in ("j", "k", "down", "up") else 10
    up = key in ("k", "up", "pgup")
    if key == "\x1b":
        # One level at a time: the box, then the session, then it is not ours.
        if n["popup"]:
            n["popup"], n["scroll"] = False, 0
        elif n["open"] is not None:
            n["open"], n["call"] = None, -1
        else:
            return None                     # nothing left to close - the caller leaves the view
        return n
    if n["popup"]:
        # Inside the box, j/k scroll the reply. Enter is INERT: there is no level below this one.
        # It was not inert - it fell through to the scroll arithmetic, where anything that is not
        # j/k/up/down counts as a page and jumped the reply ten lines.
        if key in ("end", "home"):
            n["scroll"] = 0
        elif key in ("j", "k", "down", "up", "pgup", "pgdn"):
            n["scroll"] = max(0, n["scroll"] + (-step if up else step))
        return n
    if n["open"] is not None:
        mine = [r for r in rows if (r.get("pid") or 0) == n["open"]]
        if key in ("\r", "\n"):
            n["popup"], n["scroll"] = bool(mine), 0
        elif key in ("end", "home"):
            n["call"] = -1                  # back to following the newest
        elif mine:
            cur = n["call"] if n["call"] >= 0 else len(mine) - 1
            n["call"] = max(0, min(len(mine) - 1, cur + (-step if up else step)))
            if n["call"] == len(mine) - 1:
                n["call"] = -1              # landing on the newest resumes following it
        return n
    sess = mcp_sessions(rows, live)
    if key in ("\r", "\n"):
        if sess:
            n["open"], n["call"], n["scroll"] = sess[min(n["sel"], len(sess) - 1)]["pid"], -1, 0
    elif key in ("end", "home"):
        n["sel"] = 0
    else:
        n["sel"] = max(0, min(max(0, len(sess) - 1), n["sel"] + (-step if up else step)))
    return n


def summary_line(found, c, width, key="f"):
    """What is wrong with the server, in one centred rule, for the top of every view.

    The counterpart of the key bar: that says what you can press from anywhere, this says what
    tripped from anywhere. Findings used to be readable only from the main frame, so answering "is
    anything wrong" while reading the memory panel meant leaving the memory panel.

    Centred and unlabelled. It carried the word `status` and the key that opens the screen, and
    both were noise: the counts say what it is, and the key bar at the other end of the frame
    already lists every key there is. Same counts as the findings screen's own header, from the
    same list, rather than a second thing counting the same findings its own way.
    """
    W = max(20, width)
    tripped = [f for f in found if f.get("severity", 1) > 0]
    passed = len(found) - len(tripped)
    if not found:
        plain, body = "nothing measured yet", f"{c['dim']}nothing measured yet{c['r']}"
    elif not tripped:
        tail = f" · {passed} passed" if passed else ""
        plain = f"nothing tripped{tail}"
        body = f"{c['grn']}nothing tripped{c['r']}{c['dim']}{tail}{c['r']}"
    else:
        kinds = {}
        for f in tripped:
            kinds[f.get("kind", "other")] = kinds.get(f.get("kind", "other"), 0) + 1
        counts = "  ".join(f"{n} {KINDNAME.get(k, k)}"
                           for k, n in sorted(kinds.items(), key=lambda kv: -kv[1]))
        tail = f" · {passed} passed" if passed else ""
        plain = f"{len(tripped)} finding{'s' if len(tripped) != 1 else ''} · {counts}{tail}"
        body = (f"{c['yel']}{c['b']}{len(tripped)} finding"
                f"{'s' if len(tripped) != 1 else ''}{c['r']}{c['dim']} · {counts}{tail}{c['r']}")
    # Clipped before it is centred. A rule that only ever pads runs past the frame on a narrow
    # terminal and wraps, which puts a stray half-rule under the top of every redraw.
    if len(plain) > W - 6:
        body, plain = clip(strip(body), W - 7), plain[:W - 7] + "…"
    # Measured against the VISIBLE text, not the escaped string - the arithmetic every border in
    # this renderer needs, and the one that goes wrong first.
    left = max(1, (W - len(plain) - 3) // 2)
    right = max(1, W - len(plain) - left - 3)
    return f"{c['dim']}{'─' * left}{c['r']} {body} {c['dim']}{'─' * right}{c['r']}"


def findings_frame(found, col, width, scroll, sel=0, height=40, open_=False,
                   anchors=None):
    """The `f` view: every measured finding, countable at a glance and readable one at a time.

    The summary line first, because "5 findings" is the answer to the question you had before you
    opened this - and it counts by KIND, which each finding carries from the collector rather than
    from a categoriser reading its wording. An empty list is not silence: nothing tripped is a
    result, and it says so in as many words.
    """
    c = C if col else NOCOLOR
    W = max(70, width)
    # Trouble first, passed checks last, insertion order kept inside each group. A screen that
    # opens on twelve green rows makes you scroll to find the one red one.
    found = sorted(found, key=lambda f: -f.get("severity", 1))
    # No header. The counts are on the rule pinned above every view, including this one, and a
    # screen whose first line repeats the line directly above it is a screen with one fewer row of
    # content and one more thing to read twice.
    #
    # And no leading blank where the header used to be: every view starts on the same row, which is
    # the row the storage border starts on. One view sitting a line lower than the rest reads as
    # the frame having shifted rather than as a different panel.
    out = []
    if not found:
        out += [f"  {c['dim']}{ln}{c['r']}" for ln in textwrap.wrap(
            "Nothing tripped is a result, not an absence of one: every comparison ran and none of "
            "them came out the wrong way. What was checked is in the legend (l); a panel that "
            "could not be read at all says so on its own screen rather than by staying quiet here.",
            max(40, W - 4))]
        return out[scroll:]

    sel = max(0, min(sel, len(found) - 1))
    if open_:
        return out[:1] + _finding_detail(found[sel], c, W, height, scroll)

    room = max(1, height - len(out) - 1)
    start = max(0, min(sel - room // 2, len(found) - room))
    for i, f in enumerate(found[start:start + room], start=start):
        mark = f"{c['b']}›{c['r']}" if i == sel else " "
        kc = KINDCOL.get(f.get("kind"), "cyn")
        ok = f.get("severity", 1) == 0
        # Where this row landed, for whoever draws a clickable box over it. Reported by the frame
        # rather than found by a scanner: a row is whatever the data says, with no shape to match.
        if anchors is not None:
            anchors.append((len(out), i))
        # The first sentence of the detail, not the whole of it: what the finding IS goes in the
        # left column and the reason has to survive being cut, so the numbers come first in every
        # detail string the collector writes.
        why = strip((f.get("detail") or "").split(". ")[0])
        label = "passed" if ok else KINDNAME.get(f.get("kind"), "?")
        out.append(f" {mark}{(c['grn'] + c['dim']) if ok else c[kc]}{label:<8}{c['r']} "
                   f"{'' if ok else c['b']}{c['dim'] if ok else ''}"
                   f"{clip(f.get('what', '?'), 34):<34}{c['r']} "
                   f"{c['dim']}{clip(why, max(10, W - 50))}{c['r']}")
    if start:
        out.insert(2, f"  {c['dim']}… {start} above{c['r']}")
    return out


def _finding_detail(f, c, W, height, scroll):
    """One finding, whole: what was measured, the numbers behind it, and how to check it.

    The numbers are printed as themselves rather than summarised. A finding is an argument, and the
    reader is meant to be able to disagree with it - which needs the operands.
    """
    kc = KINDCOL.get(f.get("kind"), "cyn")
    out = ["", f"  {c[kc]}{c['b']}{f.get('what', '?')}{c['r']}"
              f"   {c['dim']}{KINDNAME.get(f.get('kind'), '')}{c['r']}", ""]
    for ln in textwrap.wrap(f.get("detail") or "", max(40, W - 6)):
        out.append(f"  {ln}")
    # Every key that is not prose: the operands the sentence above was computed from.
    nums = {k: v for k, v in f.items()
            if k not in ("what", "detail", "kind", "fix", "verify", "note") and v is not None}
    if nums:
        out.append("")
        for k, v in nums.items():
            shown = human(v) if isinstance(v, int) and abs(v) > 9999 and "bytes" in k else v
            out.append(f"  {c['dim']}{k:<28}{c['r']}{shown}")
    if f.get("action"):
        out.append("")
        out.append(f"  {c['yel']}press r{c['r']} {c['dim']}and the dashboard does this itself"
                   f"{c['r']}")
    for label, key in (("fix", "fix"), ("check it", "verify"), ("note", "note")):
        if f.get(key):
            out.append("")
            out.append(f"  {c['yel']}{label}{c['r']}")
            out += [f"  {c['dim']}{ln}{c['r']}"
                    for ln in textwrap.wrap(str(f[key]), max(40, W - 6))]
    room = max(3, height - 2)
    if len(out) > room:
        scroll = max(0, min(scroll, len(out) - room))
        out = out[scroll:scroll + room - 1] + [f"  {c['dim']}… j/k scrolls{c['r']}"]
    return out


def list_nav(nav, key, items):
    """A list you walk, with one item open. Shared by the findings and activity screens because
    they are the same interaction, and two copies would drift on the first change to either."""
    return findings_nav(nav, key, items)


def findings_nav(nav, key, found):
    """One key, applied to the findings screen. Same contract as `mcp_nav`, same reasons.

    None means "not mine": there is nothing left to close, so the caller leaves the view. Shared by
    the terminal and the page so a key cannot come to mean two things.
    """
    n = {"scroll": 0, "sel": 0, "open": False, **(nav or {})}
    key = {"enter": "\r", "esc": "\x1b"}.get(key, key)
    step = 1 if key in ("j", "k", "down", "up") else 10
    up = key in ("k", "up", "pgup")
    if key == "\x1b":
        if n["open"]:
            n["open"], n["scroll"] = False, 0
            return n
        return None
    if key.startswith("sel:"):
        # A click, from the page. The row is addressed by index because that is what the hit area
        # knows - it was computed from the frame that is on screen.
        try:
            n["sel"], n["open"], n["scroll"] = int(key[4:]), True, 0
        except ValueError:
            pass
        return n
    if key in ("\r", "\n"):
        n["open"], n["scroll"] = not n["open"], 0
        return n
    if n["open"]:
        n["scroll"] = 0 if key in ("end", "home") else max(0, n["scroll"] + (-step if up else step))
        return n
    if key in ("end", "home"):
        n["sel"] = 0
    elif found:
        n["sel"] = max(0, min(len(found) - 1, n["sel"] + (-step if up else step)))
    return n

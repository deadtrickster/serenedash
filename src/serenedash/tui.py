"""serenedash.tui"""
import argparse
import json
import os
import select
import shutil
import signal
import sys
import termios
import textwrap
import threading
import time
import tty

from .config import config_files, load_config
from .fmt import C, HIST, NOCOLOR
from .anomaly import index as anom_index
from .hover import describe, panel_at, place, tip_box
from . import export as exporter
from . import history
from . import serve as _serve
from . import snapshot as snap
from . import logs as _logs
from . import mcplog as _mcplog
from .db import apply_setting, full_queries, query, sample, search, sql_status, temp_files_held
from .system import SLOW_EVERY, host_pid, hostinfo, slow, threads
from .perf import callstacks, perf_window
from .symbols import extract_container_binary, doctor, register_symbols
from .views import (
    DETAIL,
    activity_frame,
    config_frame,
    doctor_frame,
    frame,
    host_frame,
    legend_frame,
    logs_frame,
    mcp_frame,
    memory_frame,
    profile_frame,
    search_frame,
    status,
    storage_frame,
    threads_frame,
)


# A self-pipe, so a signal arriving mid-select is noticed immediately rather than at the next
# timeout. Written from the handler, drained by wait_key. Non-blocking on both ends: a handler that
# blocks on a full pipe would deadlock the process it is trying to wake.
_WAKE_R, _WAKE_W = os.pipe()
os.set_blocking(_WAKE_W, False)
os.set_blocking(_WAKE_R, False)


def _on_usr1(_sig, _frm):
    try:
        os.write(_WAKE_W, b"x")
    except OSError:
        pass


# 1003 is any-event tracking: the pointer reports where it is without a button held, which is what
# a hover needs. 1006 is the SGR encoding of the reply — the original one offsets each coordinate by
# 32 into a single byte and so cannot name a column past 223.
#
# While this is on, the terminal's own text selection is off, which is why `x` turns it off again.
# Most terminals also let Shift bypass tracking and select as usual.
MOUSE_ON = "\033[?1003h\033[?1006h"
MOUSE_OFF = "\033[?1003l\033[?1006l"


# The views whose numbers all come from SQL. Everything else on the screen is /proc, du or a perf
# capture, and none of that stops working because a password is wrong. `search` is here for the same
# reason as the rest: every figure in it is one row of sdb_metrics.
NEEDS_SQL = ("storage", "memory", "activity", "search")


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
        # No keyboard to wait on, so SLEEP the interval - do not return instantly. Returning made
        # the caller treat every spin as an elapsed tick, and the loop ran the whole collection path
        # flat out: a `--serve` run under systemd, nohup or a container pinned a core and hammered
        # the server it is supposed to be watching. Still woken by the perf-snap signal.
        select.select([_WAKE_R], [], [], timeout)
        try:
            os.read(_WAKE_R, 4096)
        except OSError:
            return ""
        return "wake"
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
            if code.startswith("<"):
                return mouse_event(code, fin)
            return {"A": "up", "B": "down", "C": "right", "D": "left"}.get(
                fin, {"5": "pgup", "6": "pgdn"}.get(code, ""))


def mouse_event(code, fin):
    """An SGR mouse report (\\033[<b;x;yM) as ('mouse', col, row, kind), 0-based. '' if malformed.

    SGR (1006) rather than the original encoding: that one packs the coordinates into single bytes
    offset by 32, so it cannot address a column past 223 and silently reports the wrong cell on a
    wide terminal — which is most of them here.

    Bit 32 of the button byte marks motion, and 64 marks the wheel. Everything else is a press we
    do not distinguish: the pointer only ever selects a panel.
    """
    parts = code[1:].split(";")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return ""
    b, x, y = (int(p) for p in parts)
    if b & 64:
        return ("mouse", x - 1, y - 1, "wheelup" if b & 1 == 0 else "wheeldn")
    if b & 32:
        return ("mouse", x - 1, y - 1, "move")
    # A release carries no button identity, so it would land on panel 0 — only the press acts.
    return ("mouse", x - 1, y - 1, "press" if fin == "M" else "release")


# How many log lines a served frame carries. The terminal sizes its log window from the window; a
# browser has no rows to speak of, so the served frame is built at the same height as the main one
# it sits beside on the page.
WEB_ROWS = 44


def view_lines(name, cfg, perf_dir, lines, s, sz, hist, perf, thr, tcpu, hinfo, sea, col, w,
               full=None, logtail=None, needle=""):
    """One view by name, or the main frame. The single dispatch both the export and --serve use.

    The terminal has its own dispatch inside the loop because it also owns scroll, selection and the
    key bar. This one is for consumers that just want a panel: give it a name, get its lines.
    """
    if name == "main" or (name in NEEDS_SQL and s is None):
        return lines
    if name == "storage":
        return storage_frame(s, sz, hinfo, col, w, 0)
    if name == "memory":
        return memory_frame(s, hist, hinfo, col, w, 0)
    if name == "activity":
        return activity_frame(s, col, w, 0, full=full)
    if name == "search":
        return search_frame(sea, col, w, 0) if sea else lines
    if name == "threads":
        return threads_frame(thr, tcpu, perf[2], hinfo, col, w, 0)
    if name == "profile":
        return profile_frame(perf, col, w, 0)
    if name == "host":
        return host_frame(hinfo, s, col, w, 0)
    if name == "doctor":
        return doctor_frame(*doctor(cfg, perf_dir), col, w, 0)
    if name == "legend":
        return legend_frame(col, w, 0)
    if name == "mcp":
        rows = _mcplog.tail(perf_dir or cfg.get("perf_dir", ""))
        return mcp_frame(rows, _mcplog.live(), col, w, 0, len(rows) - 1, WEB_ROWS)
    if name == "logs":
        # Tailed here when the caller has none, the same way `doctor` runs its own checks: a
        # consumer that just wants the panel should not have to know where this server keeps its
        # log. Always following - a page with no scroll state has nothing to hold a position for.
        rows, src, why = logtail or _logs.tail(cfg, 400)
        return logs_frame(_logs.matching(rows, needle), src, why, needle, col, w, 0, WEB_ROWS, True)
    return lines


def export(a, cfg, lines, s, sz, hist, perf, thr, tcpu, hinfo, sea, held, w):
    """The frames as SVG, or as one page carrying every panel.

    Built from the SAME frame functions the terminal draws, so an export cannot say something the
    dashboard does not - the rule that keeps `--format json` and the MCP server honest, applied to
    a third consumer. `svg` is the main frame alone, for dropping into a document; `html` is the
    whole dashboard, which is the one worth looking at.
    """
    if a.format == "svg":
        return exporter.svg(lines).rstrip()
    col = True
    views = [("main", lines),
             ("storage", storage_frame(s, sz, hinfo, col, w, 0) if s else []),
             ("memory", memory_frame(s, hist, hinfo, col, w, 0) if s else []),
             ("activity", activity_frame(s, col, w, 0, full=full_queries(cfg)) if s else []),
             ("search", search_frame(sea, col, w, 0) if sea else []),
             ("threads", threads_frame(thr, tcpu, perf[2], hinfo, col, w, 0)),
             ("profile", profile_frame(perf, col, w, 0)),
             ("host", host_frame(hinfo, s, col, w, 0)),
             ("doctor", doctor_frame(*doctor(cfg, a.perf_dir), col, w, 0)),
             ("legend", legend_frame(col, w, 0))]
    when = time.strftime("%Y-%m-%d %H:%M:%S")
    where = f"{cfg['container']}:{cfg['port']}"
    return exporter.page(views, title=f"serenedash · {where}",
                         subtitle=f"{when} · rendered at {w} columns · every panel as the terminal "
                                  f"draws it")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    # Every connection flag defaults to None so `load_config` can tell "not given" from "given the
    # same value as the default" — without that distinction a flag could not lose to anything, and
    # the config file would be unreachable for any value that happened to match a default.
    ap.add_argument("-n", "--interval", type=float, default=None)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--format", choices=("text", "json", "html", "svg"), default="text",
                    help="--once only. json emits the same structure the MCP server returns; "
                         "html is every panel as one page; svg is the main frame alone, for "
                         "embedding. Both keep colour regardless of where the output is going.")
    ap.add_argument("--width", type=int, default=None,
                    help="columns to render for --format html/svg, when the exporting terminal is "
                         "not the size you want to look at")
    ap.add_argument("--serve", metavar="[HOST:]PORT", default=None,
                    help="also serve the dashboard as a live page. Frames are pushed over "
                         "server-sent events, so a browser draws whatever the terminal draws, at "
                         "the same interval. Read-only, and it binds 127.0.0.1 unless told "
                         "otherwise.")
    ap.add_argument("--no-color", action="store_true")
    # Both directions, and neither defaults — otherwise the flag could not lose to the config file,
    # which is the layer someone who does not want tooltips will actually set.
    ap.add_argument("--mouse", dest="mouse", action="store_true", default=None,
                    help="pointer tracking: hover tooltips, click a panel to open it, wheel to "
                         "scroll. On unless turned off in config or with --no-mouse")
    ap.add_argument("--no-mouse", dest="mouse", action="store_false", default=None)
    ap.add_argument("--container", default=None)
    ap.add_argument("--port", default=None)
    ap.add_argument("--password", default=None)
    ap.add_argument("--data", default=None)
    ap.add_argument("--perf-dir", dest="perf_dir", default=None,
                    help="where perf-snap.sh writes captures. The dashboard reads them; it cannot "
                         "record on its own because perf_event_paranoid blocks attaching to a "
                         "container process without root, and making the whole dashboard run as "
                         "root to get a panel is a bad trade.")
    ap.add_argument("--config", metavar="PATH", help="a TOML file, highest-precedence of the files")
    ap.add_argument("--print-config", action="store_true",
                    help="resolved settings and which layer each came from, then exit")
    a = ap.parse_args()
    if a.config:
        os.environ["SERENEDASH_CONFIG"] = a.config
    cfg, prov = load_config({k: getattr(a, k) for k in
                             ("container", "port", "password", "data", "perf_dir", "interval",
                              "mouse")})
    if a.print_config:
        for k in sorted(cfg):
            shown_val = "<set>" if k == "password" and cfg[k] else cfg[k]
            print(f"  {k:18} {str(shown_val):46} {prov[k]}")
        print("\n  precedence: flag > environment > config file > default")
        print("  files, in increasing precedence:")
        for p in config_files():
            print(f"    {'✓' if os.path.exists(p) else '·'} {p}")
        return 0
    if a.once and a.format == "json":
        # The same builder the MCP server uses, so the two can never disagree about what a
        # snapshot contains. Reads the recorded history too, which is what lets a one-shot run in
        # a cron job report drift rather than only this instant.
        print(json.dumps(snap.collect(cfg, hist=history.load(cfg["perf_dir"])),
                         indent=2, sort_keys=True))
        return 0
    a.container, a.port, a.password = cfg["container"], cfg["port"], cfg["password"]
    a.data, a.perf_dir, a.interval = cfg["data"], cfg["perf_dir"], cfg["interval"]
    col = (not a.no_color) and (sys.stdout.isatty() or a.format in ("html", "svg"))

    prev, sz, tick, shown, fresh, s = None, {}, 0, [], True, None
    sea, held = None, None
    hist = {"mem": []}
    perf = (None, [], {})
    crows = []
    thr, tcpu, tprev, tlast = [], 0.0, {}, time.time()
    hinfo, wh = {}, (0, 0)
    drows, dfix, dmsg, fullq = None, None, None, None
    why, recording = None, True
    # Follow is the default and pausing FREEZES the buffer rather than just holding an offset. An
    # offset from the end drifts as lines arrive - you stay N lines from the newest while the lines
    # you were reading slide past - which is the thing that makes a tailer unusable for reading.
    lrows, lsrc, lwhy, lfollow, lscroll, lneedle = [], "", None, True, 0, ""
    lfind = None      # the filter being typed, or None when not typing. '' is a filter, not absence.
    # -1 selects the newest call, and KEEPS selecting it as calls arrive. An absolute index would
    # slide onto a different call every time an agent asked something.
    mrows, mlive, msel = [], [], -1
    view, scroll, sel, detail = "main", 0, 0, None
    edit, msg = None, None
    # Pointer state. `tip` is what is drawn, `tipat` where, and `tipkey` the (row, tooltip) that
    # produced it — any-motion tracking reports every cell the pointer crosses, and rebuilding the
    # frame for a move that lands on the same row saying the same thing is pure heat.
    mouse = cfg["mouse"] and not a.once and sys.stdout.isatty()
    tip, tipkey, tipon, tipage, mpos = None, None, False, 0, (0, 0)
    hpid = host_pid(cfg)
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
    hub = wsrv = None
    wstate = {}   # only the render closure now; which view a tab shows is that tab's business
    wlock = threading.Lock()
    if a.serve:
        # 127.0.0.1 unless a host is given. This exposes storage sizes, statement text and settings
        # with no authentication, so the default must not be the whole network - binding wider is a
        # decision someone makes deliberately by typing it.
        host, _, port = a.serve.rpartition(":")
        # DETAIL already carries doctor and legend; listing them again put doctor in the nav twice.
        # DETAIL is {view: key}; the page wants {key: view}, and the same keys as the terminal so
        # the two are one tool rather than two.
        wkeys = {k: v for v, k in DETAIL.items()}
        hub, wsrv = _serve.start(host or "127.0.0.1", int(port),
                                 ["main", *sorted(DETAIL)], wstate, wkeys)
        print(f"serving http://{host or '127.0.0.1'}:{int(port)}/", file=sys.stderr)
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
        if mouse:
            sys.stdout.write(MOUSE_ON)
    try:
        while True:
            # A keypress redraws; it does not re-query. Falling through to the sampler on every key
            # cost two docker execs per press — one for the sample and, in the config view, another
            # for all 297 settings — which is why holding `j` there felt like the list was stuck
            # rather than slow. Only a timeout or a SIGUSR1 wake refreshes the data.
            if fresh:
                # Fetch as much statement text as the widest row could show, and no more. The
                # terminal is read first precisely so the query can be sized to it.
                s = sample(cfg, query_head=max(200, shutil.get_terminal_size((100, 40)).columns))
            if fresh:
                # sdb_metrics on the same tick as the sample, and never on a redraw. A second round
                # trip rather than a column in sample(): it is the search engine's own table, a
                # deployment without an index has nothing in it, and sample() runs on every tick of
                # every deployment. 4 ms against sample's 111 ms here - 8 server rows and 12 per
                # index.
                sea = search(cfg) if s is not None else None
                # Only on the data tick: sql_status opens its own connection to find out WHY, and
                # doing that per keypress would put a failing connect in front of every redraw.
                why = sql_status(cfg) if s is None else None
                # du does not need the server. It used to sit behind the same branch as the SQL
                # panels, so losing the connection also lost the storage sizes, which are read off
                # the filesystem this process can see perfectly well.
                if tick % SLOW_EVERY == 0:
                    sz = slow(cfg, a.data, sz)
                    # On the du tick, not the sample tick, because it is only ever read against du's
                    # count of the same directory. Two measurements a minute apart would be a
                    # comparison of two different moments printed as one sentence.
                    held = temp_files_held(cfg) if s is not None else None
            if fresh and s is not None:
                hist["mem"] = (hist["mem"] + [s["mem"]])[-HIST:]
                # Per tag as well as in total. The total tells you memory moved; only the per-tag
                # traces say WHICH pool moved, and that is the difference between a query holding a
                # hash table and a table cache that has been growing all afternoon. Tags absent from
                # this sample record a zero rather than freezing, so a pool that drains reads as
                # dropping to the floor instead of holding its last value forever.
                now_tags = dict(s["memtags"])
                for key in set(now_tags) | {k[2:] for k in hist if k.startswith("t:")}:
                    hist["t:" + key] = (hist.get("t:" + key, [])
                                        + [now_tags.get(key, 0)])[-HIST:]
            if fresh and view == "logs" and lfollow:
                lrows, lsrc, lwhy = _logs.tail(cfg, 400)
            if fresh:
                perf = perf_window(a.perf_dir)
                hpid = hpid or host_pid(cfg)
                if hpid:
                    thr, tcpu, tprev, tlast = threads(hpid, tprev, tlast)
                hinfo = hostinfo(hpid, cfg["container"])
                for key, val in (("cpu", tcpu), ("rss", hinfo.get("rss") or 0),
                                 ("swap", hinfo.get("swap") or 0)):
                    hist[key] = (hist.get(key, []) + [val])[-HIST:]
                # On disk as well as in memory. The in-memory window is a bit over two hours and
                # dies with the process, which makes it a poor baseline for the anomaly rules and
                # no use at all to the MCP server, which is a different process entirely.
                if recording:
                    recording = history.append(a.perf_dir, time.time(),
                                               {k: v[-1] for k, v in hist.items() if v})
            tsz = shutil.get_terminal_size((100, 40))
            w, h = tsz.columns, tsz.lines
            # An export is read in a browser, not in this terminal, so the exporting terminal's
            # width is the wrong thing to inherit. 168 columns is where the paired panels and the
            # widest tails all fit; --width overrides it.
            if a.format in ("html", "svg"):
                w, h = (a.width or 168), max(h, 44)
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
            elif view == "doctor":
                if drows is None or fresh:
                    drows, dfix = doctor(cfg, a.perf_dir)
                keybar = status(cc, w, f"{cc['b']}d{cc['r']} {cc['dim']}back{cc['r']}"
                                + (f"  {cc['dim']}·{cc['r']}  {cc['b']}r{cc['r']} "
                                   f"{cc['dim']}register symbols{cc['r']}" if dfix else ""))
                body = doctor_frame(drows, dfix, col, w, scroll, dmsg)
                lines = body[:max(1, h - len(keybar))]
                lines += [""] * max(0, h - len(lines) - len(keybar)) + keybar
            elif view in DETAIL and s is None and view in NEEDS_SQL:
                # Reachable: its key still works, and a view that refuses to open reads as a broken
                # key rather than as a missing connection. It says which of the two it is.
                body = [f"{cc['b']}{view}{cc['r']}  {cc['yel']}{(why or ('unavailable', ''))[0]}"
                        f"{cc['r']}", ""]
                body += [f"  {cc['dim']}{ln}{cc['r']}"
                         for ln in textwrap.wrap((why or ("", ""))[1], max(30, w - 4))]
                body += ["", f"  {cc['dim']}threads, profile and host do not need a connection and "
                             f"are still live.{cc['r']}"]
                keybar = status(cc, w, f"{cc['b']}{DETAIL[view]}{cc['r']} {cc['dim']}back{cc['r']}")
                lines = body[:max(1, h - len(keybar))]
                lines += [""] * max(0, h - len(lines) - len(keybar)) + keybar
            elif view in DETAIL:
                # The one view that shows whole statements is the one that pays to fetch them,
                # and only while it is open — but on the data tick, not on every redraw. Inline
                # in the lambda below it ran per keypress, and with the pointer reporting every
                # cell it crosses that became a round trip for 185 KB statements per mouse move.
                if view != "activity":
                    fullq = None
                elif fresh or fullq is None:
                    fullq = full_queries(cfg)
                if view == "logs" and not lrows and not lwhy:
                    lrows, lsrc, lwhy = _logs.tail(cfg, 400)   # first entry, before any data tick
                if view == "mcp" and (fresh or not mrows):
                    mrows, mlive = _mcplog.tail(a.perf_dir), _mcplog.live()
                body = {"mcp": lambda: mcp_frame(mrows, mlive, col, w, scroll, msel, h - 1),
                        "logs": lambda: logs_frame(
                            _logs.matching(lrows, lneedle), lsrc, lwhy, lneedle, col, w,
                            lscroll, h - 1, lfollow),
                        "storage": lambda: storage_frame(s, sz, hinfo, col, w, scroll, held),
                        "memory": lambda: memory_frame(s, hist, hinfo, col, w, scroll),
                        "activity": lambda: activity_frame(s, col, w, scroll, full=fullq),
                        "threads": lambda: threads_frame(thr, tcpu, perf[2], hinfo, col, w,
                                                         scroll),
                        "profile": lambda: profile_frame(perf, col, w, scroll),
                        "host": lambda: host_frame(hinfo, s, col, w, scroll),
                        "search": lambda: search_frame(sea, col, w, scroll),
                        "legend": lambda: legend_frame(col, w, scroll)}[view]()
                keybar = status(cc, w, f"{cc['b']}{DETAIL[view]}{cc['r']} "
                                       f"{cc['dim']}back{cc['r']}  {cc['dim']}·{cc['r']}  "
                                       f"{cc['b']}j/k{cc['r']} {cc['dim']}scroll{cc['r']}")
                # Pinned to the last rows of the terminal, not left floating under whatever the
                # view happened to be tall. The keys belong in the same place on every screen.
                lines = body[:max(1, h - len(keybar))]
                lines += [""] * max(0, h - len(lines) - len(keybar)) + keybar
            elif view == "config" and s is None:
                keybar = status(cc, w, f"{cc['b']}c{cc['r']} {cc['dim']}back{cc['r']}")
                lines = ([f"{cc['b']}config{cc['r']}  {cc['yel']}"
                          f"{(why or ('unavailable', ''))[0]}{cc['r']}", "",
                          f"  {cc['dim']}the settings are the server's own; there is no reading "
                          f"them without a connection{cc['r']}"])
                lines += [""] * max(0, h - len(lines) - len(keybar)) + keybar
            elif view == "config":
                # 297 settings is a big result and they change only when someone changes them,
                # so it is fetched on the data tick and reused for every keypress in between.
                if fresh or not crows:
                    rows_ = query(cfg,
                               ["select name, value, coalesce(description,''), "
                                "input_type, scope from duckdb_settings()"])
                    crows = rows_[0] if rows_ else []
                lines, scroll, sel = config_frame(crows, s, col, w, scroll, sel, detail, edit, msg)
            else:
                lines = frame(s, prev, sz, hist, perf, thr, tcpu, hinfo, col, w, h, why,
                              sea, held)
            prev = s
            tick += 1
            if hub is not None and fresh:
                # Only on the data tick. A keypress or a pointer move redraws the terminal; it does
                # not change what the server measured, and pushing a frame per mouse cell would be
                # the same mistake the activity view made by re-fetching per redraw.
                try:
                    ww = a.width or 168
                    # The main frame has to be rebuilt at the export width. Handing the browser the
                    # terminal's frame made the page follow whatever size this window happened to
                    # be, so a 100-column terminal served a 100-column dashboard to a 4K display.
                    wmain = frame(s, prev, sz, hist, perf, thr, tcpu, hinfo, True, ww, 44, why,
                                  sea, held)
                    # Published as a closure so the HTTP thread can re-render any view from the
                    # data this tick already collected, instead of the browser waiting up to a whole
                    # interval for the next one. Rebound each tick, so it always closes over current
                    # data; the lock keeps a render off a half-updated tick.
                    def _render(name, needle="",
                                _d=(wmain, s, sz, hist, perf, thr, tcpu, hinfo, sea, ww)):
                        wm, _s, _sz, _h, _p, _t, _tc, _hi, _sea, _w = _d
                        with wlock:
                            return _serve.frame_payload(name, view_lines(
                                name, cfg, a.perf_dir, wm, _s, _sz, _h, _p, _t, _tc, _hi, _sea,
                                True, _w,
                                full=full_queries(cfg) if name == "activity" else None,
                                needle=needle), cols=_w, keys=wkeys)
                    wstate["render"] = _render
                    # One render per view someone is actually watching. Nothing is rendered when
                    # no browser is connected.
                    hub.publish_tick(_render)
                except Exception:                                # noqa: BLE001
                    pass          # a browser must never be able to take the terminal down with it
            if a.once:
                if a.format in ("html", "svg"):
                    print(export(a, cfg, lines, s, sz, hist, perf, thr, tcpu, hinfo, sea, held, w))
                else:
                    print("\n".join(lines))
                return 0
            for i, ln in enumerate(lines):
                if i >= len(shown) or shown[i] != ln:
                    sys.stdout.write(f"\033[{i + 1};1H\033[2K{ln}")
            for i in range(len(lines), len(shown)):
                sys.stdout.write(f"\033[{i + 1};1H\033[2K")
            # A copy, not the list itself: the tooltip marks the rows it covered dirty so the next
            # frame repaints them, and doing that through an alias would edit the frame instead.
            shown = list(lines)
            # Re-derived from the frame that is about to be drawn, not carried over from the move
            # that produced it. A tooltip is a statement about what is on screen, so it has to be
            # recomputed when the screen changes under a pointer that has not moved — otherwise it
            # keeps answering for the row that used to be there after a scroll, a view change, or a
            # thread dropping off the list.
            tip = describe(lines, mpos[1], mpos[0], view, anom_index(hist)) if tipon else None
            if tip:
                # Drawn over the finished frame rather than into it. The frame's height is a budget
                # every panel is fitted to, and a box that pushed rows down to make room for itself
                # would move the very thing being pointed at.
                box, bw = tip_box(tip, cc, w)
                top, left = place(bw, len(box), *mpos, w, h)
                for i, ln in enumerate(box):
                    if 0 <= top + i < h:
                        sys.stdout.write(f"\033[{top + i + 1};{left + 1}H{ln}")
                        if top + i < len(shown):
                            shown[top + i] = None
            sys.stdout.write(f"\033[{len(lines) + 1};1H")
            sys.stdout.flush()
            k = wait_key(a.interval)
            # '' is the interval elapsing, 'wake' is perf-snap signalling a new capture. Anything
            # else is a keystroke or a mouse report, and neither changes what is known, only what is
            # drawn. Set before the mouse block below, which returns to the top of the loop by its
            # own routes — leaving it after them let a pointer move inherit the previous tick's
            # `fresh` and re-run the whole sampler for a redraw.
            fresh = k in ("", "wake")
            # A tooltip has to be able to go away on its own. There is no "pointer left the window"
            # event in the protocol — the last thing reported is whatever cell it crossed on the way
            # out — so a box that lives until the next move sits on top of the frame for as long as
            # the terminal is in the background. It expires after two refreshes instead, which is
            # long enough to read and short enough not to be furniture. Any keypress also drops it:
            # the keyboard taking over says the pointer is not what is being looked at.
            if tipon and fresh:
                tipage += 1
                tipon = tipage < 2
            elif tipon and not isinstance(k, tuple) and k and k != "\x1b":
                tipon = False
            if isinstance(k, tuple):
                _, mx, my, kind = k
                mpos, tipage = (mx, my), 0
                # Leaving through an edge is the one exit that does report a cell. The outermost
                # ring carries the frame's border and the key bar, and neither has a tooltip worth
                # keeping, so treating it as "gone" costs nothing and catches the common case.
                if kind == "move" and (mx <= 0 or my <= 0 or mx >= wh[0] - 1 or my >= wh[1] - 1):
                    if not tipon:
                        continue
                    tipon, tipkey = False, None
                    continue
                if kind in ("wheelup", "wheeldn"):
                    # The wheel scrolls whatever j/k scroll, so there is nothing new to learn.
                    if view == "config" and not detail:
                        sel = max(0, sel + (-1 if kind == "wheelup" else 1))
                    else:
                        scroll = max(0, scroll + (-3 if kind == "wheelup" else 3))
                    shown = [None] * len(shown)
                    continue
                if kind == "press":
                    # Clicking a panel opens its view, the same one its key opens. Only from the
                    # main frame, so a click inside a view cannot silently switch to another.
                    p = panel_at(lines, my, mx, view)
                    if view == "main" and p in DETAIL:
                        view, scroll, tipon = p, 0, False
                        shown = [None] * len(shown)
                    continue
                if kind == "release":
                    continue
                new = describe(lines, my, mx, view, anom_index(hist))
                # Redraw only when the answer or the row changes. Moving along a row leaves the box
                # exactly where it is: any-event tracking fires once per cell crossed, so following
                # the pointer sideways costs a frame rebuild per column and gives a box that jitters
                # while you read it.
                if (my, new) == tipkey and tipon:
                    continue
                tipkey, tipon, mpos = (my, new), True, (mx, my)
                continue
            if edit is not None:
                # A tiny line editor. Enter applies, Esc cancels, backspace deletes; anything
                # printable appends. Deliberately no history or cursor movement — this is for
                # changing one value, not for living in.
                if k in ("\r", "\n"):
                    msg = apply_setting(cfg, detail, edit)
                    edit = None
                elif k == "\x1b":
                    edit, msg = None, None
                elif k in ("\x7f", "\b"):
                    edit = edit[:-1]
                elif k and len(k) == 1 and k.isprintable():
                    edit += k
                shown = [None] * len(shown)
                continue
            if k == "r" and view == "doctor" and dfix:
                kind, arg = dfix
                if kind == "extract":
                    # docker cp first: the binary in the container is the one that produced the
                    # capture, so no build has to be found or matched.
                    dest, err = extract_container_binary(
                        cfg, arg, os.path.join(cfg["perf_dir"], "binaries"))
                    dmsg = (False, err) if err else register_symbols(dest)
                else:
                    dmsg = register_symbols(arg)
                drows, dfix = doctor(cfg, a.perf_dir)
                perf_window.__defaults__[-1].clear()   # drop parses made before symbols resolved
                shown = [None] * len(shown)
                continue
            if k == "e" and view == "config" and detail:
                row = next((r for r in crows if r and r[0] == detail), None)
                if row and len(row) > 4 and (row[4] or "").upper() == "GLOBAL":
                    edit, msg = (row[1] if len(row) > 1 else ""), None
                    shown = [None] * len(shown)
                continue
            if k == "\x1b" and (tipon or detail or view != "main"):
                # One level at a time. Escaping out of a setting's description dropped the config
                # list as well and landed on the main frame, so getting back to where you were meant
                # pressing c and scrolling to the row again.
                #
                # A tooltip is NOT one of those levels, and treating it as one was wrong. You never
                # navigate into a tooltip - it appears because the pointer happens to be somewhere.
                # So while the pointer rests over a panel you clicked into, every Esc was spent
                # dismissing a box you did not ask for, and leaving the view took two or three
                # presses. It only counts as a level when there is nothing else to leave, which is
                # what keeps the escape hatch for a terminal filling the screen: no cell outside the
                # window means the edge rule never fires, and Esc is then the only way out.
                if detail:
                    detail, tipon = None, False
                elif view != "main":
                    view, scroll, tipon = "main", 0, False
                elif tipon:
                    tipon, tipkey = False, None
                shown = [None] * len(shown)
                continue
            if lfind is not None:
                # The same tiny line editor as the config one, on the log filter. Enter keeps it and
                # gets out of the way, Esc drops it - matching the box on the web page, which is the
                # same filter arriving down the stream URL.
                if k in ("\r", "\n"):
                    lneedle, lfind = lfind, None
                elif k == "\x1b":
                    lneedle, lfind = "", None
                elif k in ("\x7f", "\b"):
                    lfind = lfind[:-1]
                elif k and len(k) == 1 and k.isprintable():
                    lfind += k
                lscroll = 0
                shown = [None] * len(shown)
                continue
            if view == "mcp" and k in ("j", "k", "down", "up", "pgup", "pgdn", "end"):
                n = len(mrows)
                cur = msel if msel >= 0 else n - 1
                step = 1 if k in ("j", "k", "down", "up") else 10
                if k == "end":
                    msel = -1                      # back to following the newest
                else:
                    up = k in ("k", "up", "pgup")
                    msel = max(0, min(n - 1, cur + (-step if up else step)))
                    if msel == n - 1:
                        msel = -1                  # landing on the newest resumes following it
                shown = [None] * len(shown)
                continue
            if view == "logs" and k == "/":
                lfind = lneedle
                shown = [None] * len(shown)
                continue
            if view == "logs" and k in ("f", "j", "k", "down", "up", "pgup", "pgdn"):
                if k == "f":
                    # Resuming jumps to the newest, because "follow" and "sitting where you were"
                    # are different requests and the key says follow.
                    lfollow, lscroll = not lfollow, 0
                    if lfollow:
                        lrows, lsrc, lwhy = _logs.tail(cfg, 400)
                else:
                    step = 1 if k in ("j", "k", "down", "up") else 10
                    up = k in ("k", "up", "pgup")
                    lscroll = max(0, lscroll + (step if up else -step))
                    if up:
                        lfollow = False        # scrolling up IS the request to stop being moved
                shown = [None] * len(shown)
                continue
            if k == "q":
                return 0
            if k == "x":
                # Tracking off hands text selection back to the terminal, which is the one thing it
                # costs. Kept as a key rather than a flag because it is wanted for a moment — copy a
                # symbol out of the profile panel, then turn it back on.
                mouse, tipon = not mouse, False
                sys.stdout.write(MOUSE_ON if mouse else MOUSE_OFF)
                shown = [None] * len(shown)
                continue
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
            # strand the terminal on the scratch buffer or leave a half-drawn frame behind. Mouse
            # tracking goes out the same way and for the same reason: left on, every movement over
            # the shell that follows prints escape sequences into the prompt.
            sys.stdout.write(MOUSE_OFF + "\033[2J\033[H\033[?25h\033[?1049l")
            sys.stdout.flush()
        if pidfile:
            try:
                os.unlink(pidfile)
            except OSError:
                pass

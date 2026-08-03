"""serenedash.tui"""
import argparse
import os
import select
import shutil
import signal
import sys
import termios
import time
import tty

from .config import config_files, load_config
from .fmt import C, HIST, NOCOLOR
from .db import apply_setting, full_queries, query, sample
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
    memory_frame,
    profile_frame,
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
    # Every connection flag defaults to None so `load_config` can tell "not given" from "given the
    # same value as the default" — without that distinction a flag could not lose to anything, and
    # the config file would be unreachable for any value that happened to match a default.
    ap.add_argument("-n", "--interval", type=float, default=None)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--format", choices=("text", "json"), default="text",
                    help="--once only. json emits the same structure the MCP server returns.")
    ap.add_argument("--no-color", action="store_true")
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
                             ("container", "port", "password", "data", "perf_dir", "interval")})
    if a.print_config:
        for k in sorted(cfg):
            shown_val = "<set>" if k == "password" and cfg[k] else cfg[k]
            print(f"  {k:18} {str(shown_val):46} {prov[k]}")
        print("\n  precedence: flag > environment > config file > default")
        print("  files, in increasing precedence:")
        for p in config_files():
            print(f"    {'✓' if os.path.exists(p) else '·'} {p}")
        return 0
    a.container, a.port, a.password = cfg["container"], cfg["port"], cfg["password"]
    a.data, a.perf_dir, a.interval = cfg["data"], cfg["perf_dir"], cfg["interval"]
    col = not a.no_color and sys.stdout.isatty()

    prev, sz, tick, shown, fresh, s = None, {}, 0, [], True, None
    hist = {"mem": []}
    perf = (None, [], {})
    crows = []
    thr, tcpu, tprev, tlast = [], 0.0, {}, time.time()
    hinfo, wh = {}, (0, 0)
    drows, dfix, dmsg = None, None, None
    view, scroll, sel, detail = "main", 0, 0, None
    edit, msg = None, None
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
                # Fetch as much statement text as the widest row could show, and no more. The
                # terminal is read first precisely so the query can be sized to it.
                s = sample(cfg, query_head=max(200, shutil.get_terminal_size((100, 40)).columns))
            if s is None:
                lines = [f" cannot reach {a.container}:{a.port}"]
            else:
                if fresh:
                    if tick % SLOW_EVERY == 0:
                        sz = slow(cfg, a.data, sz)
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
                    hpid = hpid or host_pid(cfg)
                    if hpid:
                        thr, tcpu, tprev, tlast = threads(hpid, tprev, tlast)
                    hinfo = hostinfo(hpid, cfg["container"])
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
                elif view == "doctor":
                    if drows is None or fresh:
                        drows, dfix = doctor(cfg, a.perf_dir)
                    keybar = status(cc, w, f"{cc['b']}d{cc['r']} {cc['dim']}back{cc['r']}"
                                    + (f"  {cc['dim']}·{cc['r']}  {cc['b']}r{cc['r']} "
                                       f"{cc['dim']}register symbols{cc['r']}" if dfix else ""))
                    body = doctor_frame(drows, dfix, col, w, scroll, dmsg)
                    lines = body[:max(1, h - len(keybar))]
                    lines += [""] * max(0, h - len(lines) - len(keybar)) + keybar
                elif view in DETAIL:
                    body = {"storage": lambda: storage_frame(s, sz, hinfo, col, w, scroll),
                            "memory": lambda: memory_frame(s, hist, hinfo, col, w, scroll),
                            # The one view that shows whole statements is the one that pays to
                            # fetch them, and only while it is open.
                            "activity": lambda: activity_frame(
                                s, col, w, scroll,
                                full=full_queries(cfg)),
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
                        rows_ = query(cfg,
                                   ["select name, value, coalesce(description,''), "
                                    "input_type, scope from duckdb_settings()"])
                        crows = rows_[0] if rows_ else []
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

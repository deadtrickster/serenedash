"""serenedash.perf"""
import os
import re
import subprocess



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

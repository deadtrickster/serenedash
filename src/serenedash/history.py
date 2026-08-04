"""serenedash.history — the series, on disk, so they outlive the process that recorded them.

The dashboard keeps 160 samples in memory, which at the default interval is a bit over two hours,
and loses all of it on restart. Two things need more than that:

- the anomaly rules judge a value against its own past, and a baseline that resets whenever the
  dashboard is restarted is a baseline that is always young when it matters
- the MCP server is a different process. It has no history of its own — each call is one instant —
  so without a file it can report thresholds and nothing about drift

One line of JSON per sample, appended. The file is trimmed in place when it grows past twice the
retention, which is a rewrite every few hundred samples rather than every one. Nothing here is
load-bearing: every reader treats a missing or unreadable file as "no history", because a dashboard
that will not start because a cache file is corrupt is a worse outcome than one without sparklines.
"""
import json
import os

# 4096 samples is about 5.7 hours at the default 5s interval, and roughly 1 MB of file. Enough for
# an overnight comparison without turning into something that needs managing.
KEEP = 4096

NAME = "history.jsonl"


def path(perf_dir):
    return os.path.join(perf_dir, NAME)


def append(perf_dir, t, series):
    """One sample: {"t": epoch, "v": {series: value}}. Silent on any failure — see the module note.

    Returns True when written, so a caller can stop trying after a permission error rather than
    doing the same failing write every tick.
    """
    p = path(perf_dir)
    try:
        os.makedirs(perf_dir, exist_ok=True)
        with open(p, "a") as f:
            f.write(json.dumps({"t": round(t, 1),
                                "v": {k: round(float(v), 2) for k, v in series.items()
                                      if v is not None}}) + "\n")
        _trim(p)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _trim(p):
    """Keep the tail. Rewritten via a temp file and renamed, so a reader never sees a half-file."""
    try:
        if os.path.getsize(p) < KEEP * 200 * 2:
            return
        with open(p) as f:
            lines = f.readlines()
        if len(lines) <= KEEP * 2:
            return
        tmp = p + ".tmp"
        with open(tmp, "w") as f:
            f.writelines(lines[-KEEP:])
        os.replace(tmp, p)
    except OSError:
        pass


def load(perf_dir, limit=KEEP):
    """{series: [values]} in recording order, ready for anomaly.scan.

    Series absent from a sample record a zero rather than being skipped: a pool that drains has to
    read as dropping to the floor, not as holding its last value forever, and a list with holes in
    it cannot be compared position by position with another.
    """
    try:
        with open(path(perf_dir)) as f:
            lines = f.readlines()[-limit:]
    except OSError:
        return {}
    rows, keys = [], set()
    for ln in lines:
        try:
            v = json.loads(ln).get("v") or {}
        except ValueError:
            continue                                             # a torn last line, nothing more
        rows.append(v)
        keys |= set(v)
    return {k: [r.get(k, 0.0) for r in rows] for k in sorted(keys)}

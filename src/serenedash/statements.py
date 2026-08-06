"""serenedash.statements — what ran, kept from what was running.

`pg_stat_activity` is a present-tense view: it answers "what is running now" and forgets. This
server has no `pg_stat_statements`, `log_query_path` is empty and profiling is off, so once a
statement ends there is no trace it ever existed - "why was the server slow an hour ago" is
unanswerable, while "why is it slow now" was answerable all along.

The dashboard already samples that view every tick. Recording what it saw costs nothing on the
server and turns the same data into the missing half: a statement that ran for 42 hours and has
since been terminated still has its duration here.

## What this can and cannot say

SAMPLED, at the refresh interval. So:

- a statement shorter than one tick is invisible, which for a slow-query list is the right blind
  spot but has to be said rather than implied by silence
- `ran_for_s` is the last age the server reported, so it is accurate to within one interval and is
  a LOWER bound on the true duration - the statement may have run on after the last tick that saw
  it, and `ended_before` says when it was last seen rather than when it finished
- a statement seen exactly once has a duration the server gave us, not one this file measured

Identity takes three things at once - see `match()`. The start is DERIVED, and derived loosely: the
age is evaluated inside the query and `now` is stamped when the sample returns, so a tick that took
2.7 seconds and one that took 40 ms disagree about when the same statement began. Matching on an
exact start recorded one 45-hour statement twice; matching on the statement text alone would merge
two separate runs of it.
"""
import json
import os
import time

NAME = "statements.jsonl"

# Bounded like every other file this writes. 2000 statements is days of a quiet server and hours of
# a busy one, and the file is rewritten on trim rather than grown forever.
KEEP = 2000
HEAD = 400


def path(perf_dir):
    return os.path.join(perf_dir, NAME)


# How far two estimates of the same statement's start may differ and still be the same statement.
#
# The start is DERIVED - the server reports an age, so the start is `now - age` - and the two halves
# are measured at different instants: the age is evaluated inside the query, `now` is stamped when
# the sample returns. A tick that took 2.7 seconds (status() does) shifts the estimate by 2.7
# seconds against a tick that took 40 ms, so an exact key mints a new row for the same statement
# every time the tick duration changes. Seen on a live server: one 45-hour statement recorded twice,
# 31 seconds apart.
#
# 90 seconds is well past any tick duration and short enough that re-running the SAME statement text
# on the SAME connection inside it - the only thing this can wrongly merge - is rare, and merging
# two runs is a smaller error than reporting one run as several.
START_TOLERANCE = 90


def match(seen, pid, started, head):
    """The record this observation belongs to, or None for a statement not seen before.

    Three things have to hold at once and none of them alone is enough:

    - same pid, obviously
    - a start within START_TOLERANCE, which separates two RUNS of the same statement on one pooled
      connection - without it, two five-minute runs are reported as one that lasted an hour
    - one head a prefix of the other, which separates two DIFFERENT statements that a pool started
      seconds apart, while still matching the same statement arriving truncated at different
      lengths on different ticks

    Keying on a hash of the head fails the last one; keying on the derived start fails the first,
    which is how a 45-hour statement came to be recorded twice, 31 seconds apart.
    """
    for r in seen.values():
        if r.get("pid") != pid:
            continue
        if abs((r.get("started") or 0) - started) > START_TOLERANCE:
            continue
        old = r.get("statement") or ""
        if old.startswith(head[:len(old)]) or head.startswith(old[:len(head)]):
            return r
    return None


def observe(perf_dir, sample, interval=5.0):
    """Fold one sample into the record. Returns the number of statements updated.

    Silent on any failure - a dashboard's notebook must not be able to break the dashboard.
    """
    if not sample or not sample.get("queries"):
        return 0
    now = sample.get("t") or time.time()
    seen = _load(perf_dir)
    n = 0
    for row in sample["queries"]:
        if row[0] != "active" or "pg_stat_activity" in row[1]:
            continue
        age, conn, pid = (list(row) + [-1, -1, ""])[3:6]
        if not pid or age < 0:
            continue                    # -1 is "the server did not say", which is not "just started"
        addr, app = (list(row) + [-1, -1, "", "", ""])[6:8]
        started, head = now - age, (row[1] or "")[:HEAD]
        rec = match(seen, pid, started, head)
        k = rec["key"] if rec else f"{pid}:{int(started)}"
        rec = rec or {"key": k, "pid": pid, "started": round(started, 1),
                      "statement": head, "chars": row[2],
                      "client_addr": addr, "application_name": app,
                      "connection_age_s": conn, "samples": 0}
        rec["ran_for_s"] = max(age, rec.get("ran_for_s", 0))
        rec["last_seen"] = round(now, 1)
        rec["samples"] += 1
        # The head can arrive truncated at different lengths on different ticks; keep the longest,
        # so a row is never made less informative by being seen again.
        if len(row[1]) > len(rec["statement"]):
            rec["statement"] = row[1][:HEAD]
        rec["chars"] = max(rec.get("chars", 0), row[2])
        seen[k] = rec
        n += 1
    _save(perf_dir, seen, interval)
    return n


def recent(perf_dir, limit=KEEP):
    """Everything recorded, longest-running first. [] when nothing has been recorded yet."""
    rows = list(_load(perf_dir).values())
    rows.sort(key=lambda r: -(r.get("ran_for_s") or 0))
    return rows[:limit]


def running(rows, now=None, interval=5.0):
    """Split into (still running, ended). A row not seen for two intervals has ended.

    Two rather than one, because a tick that ran long - `doctor` shells out, `activity` refetches -
    would otherwise declare every statement finished and then resurrect it.
    """
    now = now or time.time()
    live, done = [], []
    for r in rows:
        (live if now - (r.get("last_seen") or 0) < interval * 2 + 1 else done).append(r)
    return live, done


def _load(perf_dir):
    out = {}
    try:
        with open(path(perf_dir)) as f:
            for ln in f:
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue                    # a torn line from a writer that was interrupted
                if r.get("key"):
                    out[r["key"]] = r
    except OSError:
        return {}
    return out


def _save(perf_dir, seen, interval):
    try:
        os.makedirs(perf_dir, exist_ok=True)
        rows = sorted(seen.values(), key=lambda r: r.get("last_seen") or 0)[-KEEP:]
        tmp = path(perf_dir) + ".tmp"
        with open(tmp, "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        os.replace(tmp, path(perf_dir))         # a reader never sees a half-written file
        return True
    except (OSError, TypeError, ValueError):
        return False

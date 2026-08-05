"""serenedash.mcplog — what the agents actually asked, and what they were told.

The dashboard shows the server. This shows the OTHER side of the same session: an MCP server is a
process an agent spawns and talks to over a pipe, with no window and no log, so a model reading
this deployment wrong is invisible from here. It was invisible in exactly that way once - a local
model read `status()` and reported three conclusions the tools do not support, and the only reason
anyone found out is that the answer was pasted into a chat by hand.

So both halves are recorded: the call with its arguments, and the reply. The reply matters more
than it sounds. "Which tools did it call" tells you what it looked at; only the reply tells you
what it was told, and the gap between what a finding says and what the agent then claims is the
thing worth seeing.

## Who is calling

MCP over stdio carries no client identity - there is no name, no version, nothing in the protocol
that says which agent is on the other end. The parent process does: an MCP server is spawned as a
child, so `/proc/<ppid>/cmdline` is the client, and it is specific enough to be useful. It is how
`claude --continue` from yesterday was told apart from a local qwen, which is a distinction no
field in the protocol offers.

## What is NOT here

Nothing about the model's reasoning, and nothing it did not send. A tool call is all this side of
the pipe can see. If an agent read a finding correctly and then said something else, that is
visible only as a gap between the reply here and whatever it told the user, which is a comparison a
person has to make.
"""
import json
import os
import time

NAME = "mcp.jsonl"

# Bounded by both, because these two entries are not the same size: a `config()` reply is a few
# hundred bytes and a `status()` is tens of kilobytes. A count alone would let the file reach
# hundreds of megabytes; a byte cap alone would keep four calls after one big one.
KEEP = 400
# 4000 kept about a fifth of a status(), which is the reply people most want to read - the whole
# point of the view is what an agent was actually told. 400 x 12000 is a 4.8 MB ceiling in a cache
# directory that already holds perf captures.
REPLY_CHARS = 12000
ARG_CHARS = 600


def path(perf_dir):
    return os.path.join(perf_dir, NAME)


def client(pid=None):
    """Who spawned us, as a short command line. '' when it cannot be read.

    The protocol has no client identity, so the process tree is the only evidence. Read at call
    time rather than at startup: it costs one /proc read and it stays right if the parent is
    replaced, where a value captured at import would quietly describe a process that is gone.
    """
    try:
        ppid = os.getppid() if pid is None else pid
        with open(f"/proc/{ppid}/cmdline", "rb") as f:
            argv = f.read().split(b"\0")
        parts = [p.decode("utf-8", "replace") for p in argv if p]
        if not parts:
            return ""
        # Just the program, then any argument that identifies the session. A full agent command
        # line runs to several hundred characters of flags nobody is reading in a panel.
        # Program plus whatever identifies the session, with paths reduced to their basename: the
        # useful half of `--mcp-config /home/dead/Projects/oracle/oracle-mcp.json` is the filename,
        # and the flag alone said nothing that `claude` had not already said.
        head = os.path.basename(parts[0])
        tail = []
        for a in parts[1:]:
            if a.startswith("--") and "=" not in a:
                tail.append(a[2:])
            elif not a.startswith("-"):
                tail.append(os.path.basename(a))
            if len(tail) >= 2:
                break
        return " ".join([head, *tail])[:44]
    except (OSError, ValueError):
        return ""


def record(perf_dir, tool, args, ms, reply, ok=True, err=""):
    """Append one call. Silent on failure - a dashboard's log must not break the tool it records."""
    try:
        os.makedirs(perf_dir, exist_ok=True)
        text = reply if isinstance(reply, str) else json.dumps(reply, default=str)
        # A tool that cannot answer returns {"error": ...} rather than raising - the pipe worked,
        # the call did not - so `ok` cannot come from "did this raise" alone. It said True on three
        # failed queries in a row and the view believed it.
        if ok and isinstance(reply, dict) and reply.get("error"):
            ok = False
        row = {"t": round(time.time(), 2), "tool": tool, "ms": round(ms, 1), "ok": bool(ok),
               "client": client(), "pid": os.getpid(),
               # Summarised HERE, with the whole object in hand. Doing it in the view means parsing
               # the stored copy, and the stored copy of a `status()` is truncated JSON that will
               # not parse - so the one reply most worth summarising was the one that fell back to
               # showing raw braces.
               "summary": _summarize(reply, err),
               "args": _short(args), "bytes": len(text),
               "reply": text[:REPLY_CHARS] + ("…" if len(text) > REPLY_CHARS else "")}
        if err:
            row["error"] = str(err)[:400]
        with open(path(perf_dir), "a") as f:
            f.write(json.dumps(row) + "\n")
        _trim(path(perf_dir))
        return True
    except (OSError, TypeError, ValueError):
        return False


def _short(args):
    """Arguments as one line. `query()` carries SQL, which is the one worth seeing in full-ish."""
    if not args:
        return ""
    try:
        s = ", ".join(f"{k}={v!r}" if not isinstance(v, str) else f"{k}={v}"
                      for k, v in args.items())
    except (TypeError, ValueError):
        return ""
    return s[:ARG_CHARS] + ("…" if len(s) > ARG_CHARS else "")


def _trim(p):
    """Keep the tail. Written to a temp file and renamed, so a reader never sees a half-file."""
    try:
        with open(p) as f:
            lines = f.readlines()
        if len(lines) <= KEEP:
            return
        tmp = p + ".tmp"
        with open(tmp, "w") as f:
            f.writelines(lines[-KEEP:])
        os.replace(tmp, p)
    except OSError:
        pass


def tail(perf_dir, limit=KEEP):
    """The recorded calls, oldest first. [] when nothing has called this server."""
    try:
        with open(path(perf_dir)) as f:
            lines = f.readlines()[-limit:]
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue                  # a torn last line while a server was writing it
    return out


def live():
    """The MCP servers running right now: [{pid, client, uptime_s}], newest last.

    Not read from the log - a process that has been spawned and has answered nothing appears here
    and nowhere else, and "an agent is connected but has asked nothing" is a real state. It is also
    the answer to why several of these are running: each client session spawns its own, and one
    left open yesterday is still holding a process.
    """
    out = []
    try:
        boot = _boot_time()
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmd = f.read().decode("utf-8", "replace")
                if "serenedash-mcp" not in cmd and "mcp_server" not in cmd:
                    continue
                with open(f"/proc/{pid}/stat") as f:
                    started = float(f.read().rsplit(") ", 1)[1].split()[19]) / os.sysconf("SC_CLK_TCK")
                ppid = int(open(f"/proc/{pid}/status").read().split("PPid:")[1].split()[0])
                out.append({"pid": int(pid), "client": client(ppid),
                            "uptime_s": int(time.time() - (boot + started))})
            except (OSError, ValueError, IndexError):
                continue
    except OSError:
        return []
    return sorted(out, key=lambda r: -r["uptime_s"])


def _boot_time():
    with open("/proc/stat") as f:
        for line in f:
            if line.startswith("btime"):
                return float(line.split()[1])
    return 0.0


def counts(rows):
    """{tool: (calls, total_ms)} over the recorded calls, for a header that says what was asked."""
    out = {}
    for r in rows:
        n, ms = out.get(r.get("tool", "?"), (0, 0.0))
        out[r["tool"]] = (n + 1, ms + (r.get("ms") or 0))
    return out


# What a reply is ABOUT, in one line. The raw head of a `status()` is `{"sql": {"available":` and
# tells you nothing; the count of findings and what they are is the whole content. Reading a log of
# calls is pointless if every row says only how many bytes came back.
def digest(row):
    """One line describing the reply. Recorded with the call; parsed for older rows.

    A FAILED row is re-summarised from its reply even when it carries a summary, because rows
    written before `detail` was used say only "query failed" - the category, not the message - and
    those are exactly the rows someone is scrolling the log to read. The reply of a failure is
    small, so it parses and costs nothing.
    """
    if row.get("summary") and not failed(row):
        return row["summary"]
    if failed(row) and row.get("reply"):
        try:
            return _summarize(json.loads(row["reply"]), "")
        except ValueError:
            pass
    if row.get("summary"):
        return row["summary"]
    if row.get("error"):
        return f"error: {row['error'][:60]}"
    try:
        return _summarize(json.loads(row.get("reply") or ""), "")
    except ValueError:
        return (row.get("reply") or "")[:60]      # truncated JSON from before summaries existed


def _summarize(r, err=""):
    """'5 findings: orphaned temp files, …', '24 rows', 'refused: delete is not read-only'."""
    if err:
        return f"error: {str(err)[:60]}"
    if isinstance(r, str):
        return r[:60]
    if not isinstance(r, dict):
        return f"{len(r)} items" if isinstance(r, list) else str(r)[:60]
    if r.get("error"):
        # `detail` is where the server's own message lands; `error` alone is a category. "query
        # failed" told a reader nothing, while "Table with name pg_compression does not exist!" is
        # the entire finding.
        why = str(r.get("detail") or "").strip().splitlines()
        return (f"{r['error']}: {why[0]}" if why else str(r["error"]))[:110]
    if isinstance(r.get("findings"), list):
        w = ", ".join(str(f.get("what", "?")) for f in r["findings"][:2] if isinstance(f, dict))
        return f"{len(r['findings'])} findings" + (f": {w}" if w else "")
    if isinstance(r.get("anomalies"), list):
        return f"{len(r['anomalies'])} anomalies over {r.get('samples', '?')} samples"
    if isinstance(r.get("rows"), list):
        return f"{len(r['rows'])} rows"
    if r.get("available") is False:
        return f"unavailable: {str(r.get('reason', ''))[:52]}"
    keys = [k for k in r if k != "server"]
    return ", ".join(keys[:6]) + ("…" if len(keys) > 6 else "")


def pretty(row, width=110):
    """The reply as lines. Indented JSON, because wrapped JSON is a wall of punctuation.

    Truncated per line rather than reflowed: the structure is the readable part, and a reflow
    destroys exactly that.
    """
    if row.get("error"):
        return [row["error"]]
    body = row.get("reply") or ""
    try:
        out = json.dumps(json.loads(body), indent=2, ensure_ascii=False).splitlines()
    except ValueError:
        # Every big reply is stored truncated, so it does NOT parse - and the one that matters
        # most, status(), was rendering as a single 12000-character line. Indented structurally
        # instead, which needs no valid document and reads the same as the parsed form.
        out = _reflow(body)
    return [ln[:width] + ("…" if len(ln) > width else "") for ln in out]


def _reflow(text, indent=2):
    """Indent JSON-ish text that may be cut off mid-value. String-aware, so a brace inside a
    message does not change the depth."""
    out, cur, depth, in_str, esc = [], "", 0, False, False
    for ch in text:
        if in_str:
            cur += ch
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str, cur = True, cur + ch
        elif ch in "{[":
            depth += 1
            out.append(cur + ch)
            cur = " " * (indent * depth)
        elif ch in "}]":
            out.append(cur)
            depth = max(0, depth - 1)
            cur = " " * (indent * depth) + ch
        elif ch == ",":
            out.append(cur + ch)
            cur = " " * (indent * depth)
        elif ch == "\n":
            continue
        elif ch == " " and not cur.strip():
            continue                      # the source's own space after a comma, now a line break
        else:
            cur += ch
    out.append(cur)
    return [ln.rstrip() for ln in out if ln.strip()]


def sessions(rows, live_procs=None):
    """One row per MCP server process: [{pid, client, calls, errors, tools, first, last, live}].

    Grouped by pid rather than by client name, because the client name is not unique - four
    `claude` sessions look identical from /proc, and they are four different conversations asking
    different things. The pid is the session: one process per client connection, for its lifetime.

    Processes with no calls are included from `live_procs`. An agent that connected and has asked
    nothing is a real state and the log cannot show it, which is exactly when you want to see it.
    """
    by = {}
    for r in rows:
        pid = r.get("pid") or 0
        s = by.setdefault(pid, {"pid": pid, "client": r.get("client") or "", "calls": 0,
                                "errors": 0, "tools": {}, "first": None, "last": None,
                                "ms": 0.0, "bytes": 0, "live": False, "uptime_s": None})
        s["calls"] += 1
        s["ms"] += r.get("ms") or 0
        s["bytes"] += r.get("bytes") or 0
        if failed(r):
            s["errors"] += 1
        s["tools"][r.get("tool", "?")] = s["tools"].get(r.get("tool", "?"), 0) + 1
        t = r.get("t")
        if t:
            s["first"] = t if s["first"] is None else min(s["first"], t)
            s["last"] = t if s["last"] is None else max(s["last"], t)
        if r.get("client"):
            s["client"] = r["client"]
    for p in live_procs or []:
        s = by.setdefault(p["pid"], {"pid": p["pid"], "client": p.get("client") or "", "calls": 0,
                                     "errors": 0, "tools": {}, "first": None, "last": None,
                                     "ms": 0.0, "bytes": 0, "live": False, "uptime_s": None})
        s["live"] = True
        s["uptime_s"] = p.get("uptime_s")
        s["client"] = s["client"] or p.get("client") or ""
    # Live first, then most recently active. A session still holding a process is the one you can
    # still do something about; a dead one is history.
    return sorted(by.values(), key=lambda s: (not s["live"], -(s["last"] or 0)))


def failed(row):
    """Did this call fail? True for a raise, an error reply, or an older row that predates `ok`.

    One predicate rather than three tests scattered over the views, because the answer moved twice
    already: it used to be "did it raise", then "does the reply carry an error", and rows written
    under the old rule are still in the file.
    """
    if not row.get("ok", True):
        return True
    if row.get("error"):
        return True
    return (row.get("reply") or "").lstrip().startswith('{"error"')


def calls_of(rows, pid):
    return [r for r in rows if (r.get("pid") or 0) == pid]

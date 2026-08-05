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
REPLY_CHARS = 4000
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
    """One line describing the reply. Recorded with the call; parsed only for older rows."""
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
        return str(r["error"])[:64]               # already reads as a refusal; do not prefix it
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
        return body.splitlines() or [""]
    return [ln[:width] + ("…" if len(ln) > width else "") for ln in out]

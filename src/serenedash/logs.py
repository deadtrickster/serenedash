"""serenedash.logs — the server's own log, tailed and filtered.

Four possible sources, and which one answers is not a detail you get to ignore.

`duckdb_logs()` is the obvious one and is usually EMPTY. The table function targets whatever log
storage is active, and `serened` ships with `--log_storage=stdout`, so the rows go to the process's
stdout and the function returns nothing. Reporting that as "no log activity" would be the same class
of mistake as reporting an empty `pg_locks` stub as "no locks".

Where stdout *goes* depends on the deployment, and docker is only one of them: a container keeps it
for `docker logs`, the .deb service hands it to journald, and a shell-script or tarball install sends
it wherever the person who started it pointed it. `tail()` asks the server what it does with its log
before guessing from the target. The records themselves are denormalized TSV:

    context_id  scope  connection_id  transaction_id  query_id  thread_id  timestamp  type  level  message

with the four id fields usually empty. The subsystem is in `type`, and the ones worth filtering on
are `Startup`, `Search`, `IResearch`, `Storage`, `SSL` and `HTTP`; server messages with no type land
in the empty string, which is most of them.

Whichever source answers, the view says which one it was. A log that is quiet because logging is off
looks exactly like a log that is quiet because nothing happened, and those want different reactions.
"""
import subprocess

# The subsystems serened emits. Kept here rather than inferred from what happens to be in the
# buffer, so a filter can offer them before any line of that type has been seen.
TYPES = ("Startup", "Search", "IResearch", "Storage", "SSL", "HTTP", "QueryLog", "FileSystem")

LEVELS = ("TRACE", "DEBUG", "INFO", "WARN", "WARNING", "ERROR", "FATAL")


def parse(line):
    """One stdout line as (when, type, level, message). None if it is not a log record.

    Anything unparseable is returned as a message with no timestamp rather than dropped: a crash
    dump or a library writing straight to stderr is exactly the thing you are tailing the log for,
    and it will not be in the server's format.
    """
    parts = line.rstrip("\n").split("\t")
    if len(parts) >= 10:
        when, typ, lvl, msg = parts[6], parts[7].strip('"'), parts[8], "\t".join(parts[9:])
        if lvl in LEVELS:
            return (when[:19], typ, lvl, msg)
    return ("", "", "", line.rstrip("\n")) if line.strip() else None


def from_container(cfg, tail=400):
    """`docker logs`. (rows, None) or (None, why)."""
    name = cfg.get("container")
    if not name:
        return None, "no container name configured"
    try:
        out = subprocess.run(["docker", "logs", "--tail", str(tail), name],
                             capture_output=True, text=True, timeout=20)
    except Exception as e:                                       # noqa: BLE001
        return None, f"docker logs failed: {e}"
    if out.returncode != 0:
        return None, (out.stderr or "docker logs failed").strip().splitlines()[0][:120]
    # serened writes records to stdout; anything on stderr is the container runtime's own noise.
    rows = [r for r in (parse(ln) for ln in out.stdout.splitlines()) if r]
    return rows, None


def from_sql(cfg, tail=400):
    """`duckdb_logs()`, for a server whose log storage is memory. (rows, None) or (None, why)."""
    from .db import query                                        # noqa: PLC0415  - avoid a cycle
    b = query(cfg, ["select coalesce(timestamp::VARCHAR,''), coalesce(type,''), "
                    "coalesce(log_level,''), coalesce(message,'') from duckdb_logs() "
                    f"order by timestamp desc limit {int(tail)}"])
    if b is None:
        return None, "cannot reach the server"
    rows = [(r[0][:19], r[1], r[2], r[3]) for r in (b[0] or []) if len(r) == 4]
    return list(reversed(rows)), None


def from_file(path, limit=400):
    """A log file, when the server was told to write one. (rows, None) or (None, why)."""
    try:
        with open(path, errors="replace") as fh:
            lines = fh.readlines()[-limit:]
    except OSError as e:
        return None, f"{path}: {e.strerror or e}"
    return [r for r in (parse(ln) for ln in lines) if r], None


def from_journal(unit="serenedb", limit=400):
    """`journalctl`, which is where the .deb service's stdout goes on a native install."""
    try:
        out = subprocess.run(["journalctl", "-u", unit, "-n", str(limit), "--no-pager", "-o", "cat"],
                             capture_output=True, text=True, timeout=20)
    except Exception as e:                                       # noqa: BLE001
        return None, f"journalctl failed: {e}"
    if out.returncode != 0 or not out.stdout.strip():
        return None, (out.stderr or f"no journal for unit {unit}").strip().splitlines()[0][:120]
    return [r for r in (parse(ln) for ln in out.stdout.splitlines()) if r], None


def destination(cfg):
    """What the SERVER says it does with its log: (storage, path). ('', '') if it will not say.

    Asked rather than assumed. Docker is one deployment among several - there is a .deb service, a
    shell-script install and a tarball, and on those the log goes to journald or to a file, not to
    `docker logs`. The server already carries the answer in `sdb_settings`, so guessing from the
    target would be inventing something that is on record.
    """
    from .db import query                                        # noqa: PLC0415  - avoid a cycle
    b = query(cfg, ["select name, setting from sdb_settings "
                    "where name in ('log_storage','log_path')"])
    if not b:
        return "", ""
    d = {r[0]: r[1] for r in (b[0] or []) if len(r) == 2}
    return d.get("log_storage", ""), d.get("log_path", "")


def tail(cfg, limit=400):
    """(rows, source, why). `source` names where they came from, or '' when nothing answered.

    The ladder follows what the server says it does, then how it was deployed:

    1. `log_storage=file` - read `log_path`. Works wherever that path is visible to us.
    2. `log_storage=memory` - `duckdb_logs()`. The only one that reaches a remote server.
    3. stdout, in a container - `docker logs`.
    4. stdout, native - `journalctl -u serenedb`, which is where the .deb service's output goes.

    Whichever answers, the caller is told which. A log that is empty because logging is off looks
    exactly like a log that is empty because nothing happened, and those want different reactions.
    """
    storage, path = destination(cfg)
    tried = []
    if storage == "file" and path:
        rows, why = from_file(path, limit)
        if rows is not None:
            return rows, path, None
        tried.append(why)
    if storage == "memory" or not storage:
        rows, why = from_sql(cfg, limit)
        if rows:
            return rows, "duckdb_logs()", None
        tried.append(why or "duckdb_logs() is empty")
    if (cfg.get("target") or "docker") == "docker":
        rows, why = from_container(cfg, limit)
        if rows:
            return rows, f"docker logs {cfg['container']}", None
        tried.append(why or "docker logs returned nothing")
    else:
        rows, why = from_journal(limit=limit)
        if rows:
            return rows, "journalctl -u serenedb", None
        tried.append(why or "journalctl returned nothing")
    hint = ("serened writes to stdout by default, so there is no table to read and no file to tail: "
            "on a container that is `docker logs`, on the .deb service it is `journalctl -u "
            "serenedb`, and for a shell-script or tarball install it went wherever you redirected "
            "it. `CALL enable_logging(storage='memory')` fills duckdb_logs() from any deployment")
    return [], "", f"{'; '.join(t for t in tried if t)} - {hint}"


def matching(rows, needle="", types=(), levels=()):
    """Filter. Case-insensitive substring over the message, plus exact type and level.

    Substring rather than regex on purpose: this runs on every keystroke of a search box, and a
    half-typed regex is a syntax error rather than a narrower result.
    """
    n = needle.lower()
    return [r for r in rows
            if (not n or n in r[3].lower() or n in r[1].lower())
            and (not types or r[1] in types)
            and (not levels or r[2].upper() in levels)]


def counts(rows):
    """{level: n} and {type: n} over the rows, for a header that says what is in the buffer."""
    lv, ty = {}, {}
    for _, t, l, _m in rows:
        if l:
            lv[l.upper()] = lv.get(l.upper(), 0) + 1
        if t:
            ty[t] = ty.get(t, 0) + 1
    return lv, ty

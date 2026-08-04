"""serenedash.db"""
import re
import time

from .fmt import to_bytes
from .hazards import HAZARDS


SEP = "---8<---"


def pg_driver():
    """psycopg, or None. Imported lazily so the tool still starts without it."""
    try:
        import psycopg                                          # noqa: PLC0415
        return psycopg
    except ImportError:
        return None


def query(cfg, sql, timeout=30, params=None):
    """Run each statement, return list-of-rowlists. One client for every target.

    A driver rather than shelling out to psql, and the same path whether the server runs in a
    container, on this host, or on another one. The previous form ran `docker exec … psql` — which
    works only for a local container, needs a psql inside the image, and pays a process spawn per
    tick for a query that takes milliseconds. Branching on target for SQL bought nothing but ifs.
    Filesystem and /proc access still fork by target below; those genuinely differ.

    Values come back as strings, matching what psql's text output gave, so every parser downstream
    is unchanged. `None` means the query did not run — the caller reports why (see `sql_status`).

    `params` is a list aligned with `sql`: one tuple per statement, or None for statements that take
    none. Anything from outside this file goes here rather than into an f-string. The MCP `config`
    tool interpolated its `name` argument directly, which is an injection an agent can reach — the
    connection is read-only so it could not write, but "the damage is bounded" is not the same as
    "the query is correct".
    """
    drv = pg_driver()
    if drv is None or not cfg.get("password"):
        return None
    try:
        with drv.connect(host=cfg.get("host") or "127.0.0.1", port=int(cfg["port"]),
                         user=cfg.get("user") or "postgres", password=cfg["password"],
                         dbname=cfg.get("database") or "postgres",
                         connect_timeout=min(10, timeout)) as cn:
            cn.read_only = not cfg.get("_write")
            out = []
            with cn.cursor() as cur:
                for i, stmt in enumerate(sql):
                    args = (params or [])[i] if params and i < len(params) else None
                    cur.execute(stmt, args) if args else cur.execute(stmt)
                    rows = cur.fetchall() if cur.description else []
                    out.append([["" if v is None else str(v) for v in r] for r in rows])
            return out
    except Exception:                                           # noqa: BLE001
        return None


# Statement kinds that cannot change anything. An allowlist rather than a blocklist of DDL/DML: a
# blocklist has to be complete to be correct, and DuckDB grows statement kinds faster than anyone
# will remember to update it. Anything not named here is refused, including anything unparseable.
#
# This is a second line, not the only one. The connection is opened read-only, so the server itself
# rejects a write regardless of what gets past this — but "the server would have refused it" is a
# poor thing to find out from a caller's error message, and read-only does not stop a statement
# from being expensive.
READ_ONLY = ("select", "with", "show", "describe", "explain", "summarize", "pragma", "values",
             "table", "from", "call")

# `call` and `pragma` are the two that need thinking about: both reach procedures, and DuckDB's
# read-only mode is what actually stops the ones that write. They are here because the settings and
# memory views this whole tool is built on are pragmas, and refusing them would mean an agent can
# read every panel except through the one tool meant for asking its own questions.


def statement_kind(sql):
    """The leading keyword, with comments and leading punctuation stripped. '' if there is none."""
    s = (sql or "").strip()
    while True:
        if s.startswith("--"):
            s = s.split("\n", 1)[1].strip() if "\n" in s else ""
        elif s.startswith("/*"):
            s = s.split("*/", 1)[1].strip() if "*/" in s else ""
        elif s.startswith("("):
            s = s[1:].strip()
        else:
            break
    m = re.match(r"[A-Za-z_]+", s)
    return m.group(0).lower() if m else ""


def read_query(cfg, sql, max_rows=200, max_chars=20000, timeout=30):
    """One read-only statement, with its column names. Returns a dict, never raises.

    Bounded in three independent ways because a caller here is a context window rather than a
    terminal: the statement kind must be one that cannot write, the row count is capped, and the
    rendered size is capped after that — a hundred rows of a wide table is still megabytes, and the
    tool result limit is the same one that a 1.66 MB `activity` response blew through once already.
    """
    drv = pg_driver()
    if drv is None:
        return {"error": "no driver", "fix": "pip install 'psycopg[binary]'"}
    if not cfg.get("password"):
        return {"error": "no credentials",
                "fix": "PGPASSWORD, `password`, or `password_command` in the config file"}
    if ";" in sql.strip().rstrip(";"):
        return {"error": "one statement at a time",
                "detail": "a batch would let a read-only kind carry a write behind it"}
    kind = statement_kind(sql)
    if kind not in READ_ONLY:
        return {"error": f"refused: {kind or 'unrecognised'} is not a read-only statement",
                "allowed": list(READ_ONLY),
                "detail": "the connection is opened read-only as well, so this is the first of two "
                          "checks rather than the only one"}
    try:
        with drv.connect(host=cfg.get("host") or "127.0.0.1", port=int(cfg["port"]),
                         user=cfg.get("user") or "postgres", password=cfg["password"],
                         dbname=cfg.get("database") or "postgres",
                         connect_timeout=min(10, timeout)) as cn:
            cn.read_only = True
            with cn.cursor() as cur:
                cur.execute(sql)
                cols = [d.name for d in cur.description] if cur.description else []
                rows = cur.fetchmany(max_rows + 1) if cur.description else []
    except Exception as e:                                       # noqa: BLE001
        # The server's own message, which is the useful half of a failed query. Trimmed, because a
        # DuckDB parse error quotes the statement back with a caret diagram.
        return {"error": "query failed", "detail": str(e).strip()[:2000]}
    more = len(rows) > max_rows
    rows = [["" if v is None else str(v) for v in r] for r in rows[:max_rows]]
    out = {"columns": cols, "rows": rows, "row_count": len(rows)}
    if more:
        out["truncated_rows"] = True
        out["note"] = f"stopped at {max_rows} rows; add LIMIT or raise max_rows"
    size = sum(len(v) for r in rows for v in r)
    if size > max_chars:
        keep, used = [], 0
        for r in rows:
            used += sum(len(v) for v in r)
            if used > max_chars:
                break
            keep.append(r)
        out["rows"], out["row_count"] = keep, len(keep)
        out["truncated_chars"] = True
        out["note"] = (f"{size} characters over the {max_chars} limit; kept the first "
                       f"{len(keep)} of {len(rows)} rows")
    return out


def sql_status(cfg):
    """Why SQL is unavailable, or None when it is. Separated so panels can say which of the two
    reasons applies rather than rendering an empty box for both."""
    if pg_driver() is None:
        return ("no driver", "pip install -r requirements.txt  (psycopg)")
    if not cfg.get("password"):
        return ("no credentials",
                "set a password: PGPASSWORD, `password`, or `password_command` in the config file")
    if query(cfg, ["select 1"], timeout=8) is None:
        return ("cannot connect",
                f"{cfg.get('host')}:{cfg.get('port')} as {cfg.get('user')} — for a container, the "
                f"port has to be published (docker port {cfg.get('container')})")
    return None


def sample(cfg, query_head=200):
    """One docker exec per tick. `query_head` is how much statement text to fetch.

    Fetch what will be displayed, not what exists. The main panel gives each session one row, so it
    can never show more than the terminal's width — while the statements themselves run to ~185 KB
    on this deployment, which is 1.84 MB moved through a docker exec every five seconds for a panel
    that then clips each row to ~90 columns. Callers that will show more (the `a` view, an MCP
    request asking for it) pass a larger value or re-fetch in full; the tick pays for the head only.
    """
    b = query(cfg, [
        "select database_name, database_size, wal_size, memory_usage, memory_limit, "
        "  total_blocks, used_blocks, free_blocks, block_size "
        "from pragma_database_size() where database_name not in ('memory','postgres')",
        # temporary_storage_bytes alongside the usage: which POOL is spilling, not just that
        # something is. The panel showed usage only and threw the spill column away.
        "select tag, memory_usage_bytes, temporary_storage_bytes from duckdb_memory() "
        "order by memory_usage_bytes desc",
        # Excluding this very session. It is `active` by construction — it is the one running this
        # query — so counting it made the panel report "1 active" in green directly above "nothing
        # running", which is the same contradiction read two ways. The query list already dropped it;
        # the count did not, and the two disagreeing is worse than either being wrong alone.
        "select coalesce(state,'?'), count(*) from pg_stat_activity "
        "where pid <> pg_backend_pid() group by 1",
        # No `query <> ''` filter: with it the header counted every session and the list showed only
        # the ones carrying statement text, so `6 sessions` sat above four rows with nothing saying
        # where the other two went. A session with no statement is a row that says so.
        #
        # Truncated in SQL, not on arrival — see the docstring. length() comes back alongside the
        # head so a truncated statement is never mistaken for a short one.
        f"select coalesce(state,'?'), replace(replace(left(coalesce(query,''),{int(query_head)}),"
        "chr(10),' '),chr(13),' '), length(coalesce(query,'')) "
        "from pg_stat_activity where pid <> pg_backend_pid() order by state",
        # Every setting the HAZARDS table has an opinion about, in one go. The panel used to hard-code
        # three of them in the query and show two, so a table built from measured incidents was
        # mostly invisible on the screen that exists to surface those incidents.
        "select name, value from duckdb_settings() where name in ("
        + ", ".join(f"'{n}'" for n in sorted(HAZARDS)) + ")",
    ])
    if b is None or not b[0]:
        return None
    r = b[0][0] if b[0] and len(b[0][0]) >= 9 else [""] * 9
    return {
        "db": r[0], "size": to_bytes(r[1]), "wal": to_bytes(r[2]),
        "mem": to_bytes(r[3]), "memlimit": to_bytes(r[4]),
        "blocks": (int(r[5] or 0), int(r[6] or 0), int(r[7] or 0), int(r[8] or 0)),
        "memtags": [(x[0], int(x[1])) for x in b[1] if len(x) >= 2 and x[1].isdigit()],
        # {tag: bytes spilled}. Only the tags that actually spilled, so an empty dict is the
        # normal case and a non-empty one is the finding.
        "memspill": {x[0]: int(x[2]) for x in b[1]
                     if len(x) >= 3 and x[2].isdigit() and int(x[2]) > 0},
        "states": {x[0]: int(x[1]) for x in b[2] if len(x) == 2 and x[1].isdigit()},
        # (state, statement head, full statement length). The length is carried so a truncated
        # statement is never mistaken for a short one.
        "queries": [(x[0], x[1], int(x[2] or 0)) for x in b[3] if len(x) == 3],
        "settings": {x[0]: x[1] for x in b[4] if len(x) == 2},
        "t": time.time(),
    }


def search(cfg):
    """`sdb_metrics`, split into server-wide counters and per-index rows. None if unavailable.

    This is the engine SereneDB actually is, and no panel had a number from it. The table is one
    long (metric, value, description, relation_id) list: rows with an empty relation_id are
    server-wide, the rest repeat per index. Reshaped here so a renderer does not have to know that.

    Kept separate from `sample` because it is one more round trip for a panel that not every
    deployment has an index for, and because `sample` is on the 5s tick path.
    """
    # relation_id is INT64, not text - coalescing it to '' is a cast error, not an empty string.
    b = query(cfg, ["select metric, value, coalesce(relation_id::VARCHAR, '') from sdb_metrics"])
    if b is None:
        return None
    server, per = {}, {}
    for row in b[0] or []:
        if len(row) != 3:
            continue
        name, val, rel = row
        try:
            val = int(val)
        except ValueError:
            pass
        (server if not rel else per.setdefault(rel, {}))[name] = val
    return {"server": server, "indexes": per}


def progress(cfg):
    """`sdb_progress` for backends that are running something. [] when nothing is.

    `pg_stat_activity` says a statement is running. This says how far in and which phase, which is
    the difference between waiting for it and killing it.
    """
    b = query(cfg, ["select pid, coalesce(state,''), coalesce(command,''), coalesce(phase,''), "
                    "coalesce(percent,0), coalesce(rows_processed,0), coalesce(rows_total,0), "
                    "coalesce(bytes_processed,0), coalesce(bytes_total,0) "
                    "from sdb_progress where state = 'active'"])
    if b is None:
        return []
    out = []
    for r in b[0] or []:
        if len(r) != 9:
            continue
        nums = [float(x or 0) for x in r[4:]]
        out.append({"pid": r[0], "state": r[1], "command": r[2], "phase": r[3],
                    "percent": nums[0], "rows_done": nums[1], "rows_total": nums[2],
                    "bytes_done": nums[3], "bytes_total": nums[4]})
    return out


def temp_files_held(cfg):
    """(file count, bytes) the SERVER currently holds open in temp_directory. None if unavailable.

    The orphaned/live spill split is otherwise inferred from file mtimes against the process start
    time, which is sound but circumstantial. This is the server's own answer, and on this deployment
    it returns nothing at all while 72.6 GiB of duckdb_temp_storage_*.tmp sit on disk - which is the
    orphan claim proving itself from the inside.
    """
    b = query(cfg, ["select count(*), coalesce(sum(size), 0) from duckdb_temporary_files()"])
    if b is None or not b[0]:
        return None
    r = b[0][0]
    return (int(r[0] or 0), int(r[1] or 0)) if len(r) == 2 else None


def full_queries(cfg):
    """Untruncated statement text, for the one view that shows it. See `sample`."""
    b = query(cfg,
              ["select coalesce(state,'?'), "
              "replace(replace(coalesce(query,''),chr(10),' '),chr(13),' '), "
              "length(coalesce(query,'')) from pg_stat_activity "
              "where pid <> pg_backend_pid() order by state"])
    return [(x[0], x[1], int(x[2] or 0)) for x in (b[0] if b else []) if len(x) == 3]


def apply_setting(cfg, name, value):
    """SET GLOBAL one setting. Returns (ok, message).

    Quoting: values go through a single-quoted literal with '' escaping, and the NAME is validated
    against an identifier pattern rather than quoted — it comes from the server's own settings list,
    but a dashboard that can be talked into running arbitrary SQL by a setting name is a dashboard
    with an injection bug, and the check costs nothing.
    """
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name or ""):
        return False, f"refusing: {name!r} is not a plain identifier"
    lit = str(value).replace("'", "''")
    out = query(dict(cfg, _write=True), [f"SET GLOBAL {name} = '{lit}'"])
    if out is None:
        return False, "the server rejected it (or is unreachable) — value unchanged"
    return True, f"SET GLOBAL {name} = '{value}' — applied, NOT persisted"

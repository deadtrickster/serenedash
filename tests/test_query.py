"""The read-only SQL surface.

An agent gets to run statements against someone's database through this. The tests that matter are
the refusals, and they are written against the check that runs BEFORE any connection is opened —
the read-only connection behind it is a second line, not a substitute for reading this one.
"""
from serenedash.db import READ_ONLY, read_query, statement_kind

CFG = {"port": "7890", "password": "x"}                          # never connected to, see below


def test_the_kind_is_the_leading_keyword():
    assert statement_kind("SELECT 1") == "select"
    assert statement_kind("  with x as (select 1) select * from x") == "with"
    assert statement_kind("") == ""
    assert statement_kind("   ") == ""


def test_comments_and_brackets_do_not_hide_the_keyword():
    # The obvious way past a naive prefix check.
    assert statement_kind("-- harmless\nDELETE FROM t") == "delete"
    assert statement_kind("/* x */ drop table t") == "drop"
    assert statement_kind("/* a */ -- b\n  (select 1)") == "select"
    assert statement_kind("((select 1))") == "select"


def test_writes_are_refused_before_a_connection_is_opened():
    # CFG points at nothing; a refusal that needed to connect first would hang or error instead.
    for sql in ("delete from t", "DROP TABLE t", "update t set x=1", "insert into t values (1)",
                "create table t (a int)", "attach 'x.db'", "copy t to 'f'", "truncate t",
                "-- c\nDELETE FROM t", "/**/ alter table t add column c int"):
        out = read_query(CFG, sql)
        assert out.get("error", "").startswith("refused"), (sql, out)


def test_a_batch_is_refused_whatever_it_starts_with():
    # `select 1; drop table t` has a read-only leading keyword and a write behind it.
    out = read_query(CFG, "select 1; drop table t")
    assert out["error"] == "one statement at a time"
    # A single trailing semicolon is ordinary, not a batch.
    assert read_query(CFG, "select 1;").get("error") != "one statement at a time"


def test_an_unrecognised_statement_is_refused_rather_than_tried():
    # An allowlist, so anything the parser does not name is out — including whatever statement kind
    # the server grows next.
    assert read_query(CFG, "wibble foo")["error"].startswith("refused")
    assert read_query(CFG, "")["error"].startswith("refused")


def test_the_allowlist_is_only_kinds_that_cannot_write():
    assert "delete" not in READ_ONLY and "insert" not in READ_ONLY and "update" not in READ_ONLY
    assert "create" not in READ_ONLY and "drop" not in READ_ONLY and "attach" not in READ_ONLY
    assert "copy" not in READ_ONLY, "copy ... to writes a file"
    assert "select" in READ_ONLY and "with" in READ_ONLY


def test_missing_credentials_are_reported_not_raised():
    out = read_query({"port": "7890", "password": ""}, "select 1")
    assert out["error"] == "no credentials"
    assert "PGPASSWORD" in out["fix"]


def test_a_bound_parameter_is_not_part_of_the_statement():
    # The MCP `config` tool built `where name = '{name}'` from a caller-supplied argument. The
    # connection is read-only, so this could not write - but it could read anything, and "bounded
    # damage" is not the same as "correct query".
    import inspect

    from serenedash import mcp_server
    src = inspect.getsource(mcp_server.config.__wrapped__
                            if hasattr(mcp_server.config, "__wrapped__") else mcp_server.config)
    assert "where name = %s" in src
    # The quoted form specifically: `{name}` on its own also appears in an error message, which is
    # not SQL and not the bug.
    assert "'{name}'" not in src, "the setting name must be bound, not interpolated"


def test_no_caller_value_reaches_sql_through_an_f_string():
    # A whole-file check rather than one case: this is the class of bug, and the next one will be
    # somewhere else in the file. Only the two vetted constructions are allowed to interpolate.
    import re

    from serenedash import db as dbmod
    src = open(dbmod.__file__).read()
    for m in re.finditer(r'f"[^"]*\{[^}]+\}[^"]*"', src):
        text = m.group(0)
        if not re.search(r"select|from|where|set |insert|update|delete", text, re.I):
            continue
        # `left(query, {int(query_head)})` casts to int, and the HAZARDS name list is this file's
        # own table. Everything else has to be bound.
        assert "int(" in text or "'{n}'" in text or "SET GLOBAL" in text, text

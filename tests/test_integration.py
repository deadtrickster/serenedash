"""Against a real serened.

Skipped unless SERENEDASH_IT=1, so the default suite stays hermetic. CI sets it with a container
running; locally, point the usual config at any server and set the variable.

These assert the shapes and invariants that unit tests cannot: that the SQL actually parses against
the server's dialect, that a rate arrives with its base, and that the panels a live server produces
still fit their box. Nearly every bug this dashboard has had was of that kind — the code ran fine
and said something untrue.
"""
import json
import os
import time

import pytest

from serenedash.config import load_config
from serenedash.db import query, sample, sql_status
from serenedash.fmt import strip
from serenedash.system import host_pid, hostinfo, slow, threads
from serenedash.views import frame

pytestmark = pytest.mark.skipif(os.environ.get("SERENEDASH_IT") != "1",
                                reason="integration: set SERENEDASH_IT=1 with a server running")


@pytest.fixture(scope="module")
def cfg():
    c, _ = load_config()
    why = sql_status(c)
    if why:
        pytest.fail(f"cannot reach the server: {why[0]} - {why[1]}")
    return c


def test_driver_reaches_the_server(cfg):
    assert query(cfg, ["select 1"])[0][0][0] == "1"


def test_values_come_back_as_strings(cfg):
    # Every parser downstream was written against psql's text output. A driver that returns ints
    # for counts and str for settings would break them one panel at a time.
    rows = query(cfg, ["select 42, 'x', null"])[0]
    assert rows[0] == ["42", "x", ""]


def test_sample_fills_every_key_the_panels_read(cfg):
    s = sample(cfg)
    assert s is not None
    for key in ("size", "wal", "mem", "memlimit", "blocks", "memtags", "states", "queries",
                "settings"):
        assert key in s, f"sample() lost {key}"
    assert len(s["blocks"]) == 4
    assert all(len(q) == 3 for q in s["queries"]), "queries carry (state, head, full length)"


def test_statement_text_is_bounded_at_the_source(cfg):
    # The bug: 185 KB statements were fetched whole, 1.84 MB per tick, for a panel that shows one
    # clipped row each. The head is fetched; the full length rides alongside.
    s = sample(cfg, query_head=64)
    for _state, head, full_len in s["queries"]:
        assert len(head) <= 64
        assert full_len >= len(head)


def test_hazard_settings_are_all_present(cfg):
    from serenedash.hazards import HAZARDS
    s = sample(cfg)
    assert set(HAZARDS) <= set(s["settings"]), "a hazard the panel evaluates was not fetched"


def test_thread_percentages_are_shares_of_one_core(cfg):
    pid = host_pid(cfg)
    if not pid:
        pytest.skip("no host pid for this target")
    _, _, prev, t = threads(pid, {}, time.time())
    time.sleep(1.0)
    rows, total, _, _ = threads(pid, prev, t)
    cores = (hostinfo(pid, cfg["container"]).get("cores") or 1)
    for pct, _name, _st, _tid in rows:
        assert 0 < pct <= 100.0, "a thread cannot exceed one core"
    assert total <= cores * 100 + 1, "the total cannot exceed the machine"


def test_storage_shares_add_up(cfg):
    sz = slow(cfg, cfg["data"])
    if sz.get("total") is None:
        pytest.skip("no filesystem access for this target")
    parts = sum(v for k, v in sz.items() if k in ("duck", "index", "temp") and v)
    assert parts <= sz["total"] * 1.05, "the directories cannot exceed the total they divide"


@pytest.mark.parametrize(("w", "h"), [(200, 60), (120, 45), (100, 40), (80, 30)])
def test_a_live_frame_fits_its_terminal(cfg, w, h):
    s = sample(cfg)
    sz = slow(cfg, cfg["data"])
    pid = host_pid(cfg)
    host = hostinfo(pid, cfg["container"])
    _, _, prev, t = threads(pid, {}, time.time()) if pid else ([], 0, {}, time.time())
    thr, tcpu, _, _ = threads(pid, prev, t) if pid else ([], 0.0, {}, 0)
    lines = frame(s, None, sz, {"mem": [s["mem"]]}, (None, [], {}), thr, tcpu, host, False, w, h)
    assert len(lines) <= h
    assert max(len(strip(x)) for x in lines) <= w


def test_mcp_status_is_small_enough_to_return(cfg):
    # It returned 1.66 MB once, over the tool-result limit, because statement text was unbounded.
    mcp = pytest.importorskip("serenedash.mcp_server")
    payload = json.dumps(mcp.status(thread_window=0.3))
    assert len(payload) < 200_000, f"status() is {len(payload)} chars"
    assert "findings" in json.loads(payload)


def test_a_read_only_query_returns_columns_and_rows(cfg):
    from serenedash.db import read_query
    out = read_query(cfg, "select 1 as one, 'x' as two")
    assert out["columns"] == ["one", "two"]
    assert out["rows"] == [["1", "x"]]


def test_the_server_itself_refuses_a_write_on_this_connection(cfg):
    # The allowlist is checked first, so this goes round it with a kind that IS allowed and asks
    # the server to write. read_only on the connection is the line being tested here.
    from serenedash.db import read_query
    out = read_query(cfg, "with x as (select 1) select * from x")
    assert "rows" in out, "a genuine read must still work"
    out = read_query(cfg, "create table serenedash_should_not_exist (a int)")
    assert out["error"].startswith("refused")


def test_read_query_bounds_what_it_returns(cfg):
    from serenedash.db import read_query
    out = read_query(cfg, "select * from duckdb_settings()", max_rows=3)
    assert out["row_count"] == 3
    assert out["truncated_rows"] is True
    out = read_query(cfg, "select repeat('a', 400) from range(100)", max_chars=1000)
    assert out["truncated_chars"] is True
    assert sum(len(v) for r in out["rows"] for v in r) <= 1000 + 400


def test_the_snapshot_carries_every_panel_and_stays_small(cfg):
    import json

    from serenedash.snapshot import collect
    d = collect(cfg, thread_window=0.3)
    assert d["sql"]["available"] is True
    for key in ("findings", "storage", "memory", "activity", "threads", "profile", "host"):
        assert key in d, key
    assert len(json.dumps(d)) < 200_000


def test_a_snapshot_without_credentials_still_reports_the_host(cfg):
    # The half that does not need the server has to survive losing it - that is the whole point of
    # collecting /proc, du and perf separately from SQL.
    from serenedash.snapshot import collect
    d = collect(dict(cfg, password="", password_command=""), thread_window=0.3)
    assert d["sql"]["available"] is False
    assert d["sql"]["reason"] == "no credentials"
    assert "threads" in d and "host" in d and "storage" not in d

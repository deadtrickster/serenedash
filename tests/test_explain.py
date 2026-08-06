"""EXPLAIN, from the collector up to the key that reaches it.

The point of this feature is planning a statement that is ALREADY RUNNING - the text is in
pg_stat_activity, it is 68 KB, and nobody is going to copy it out mid-incident. So the tests that
matter are not "does EXPLAIN work" but "does `e` plan the row the cursor is on": the cursor is an
index into a filtered, sorted list, and the frame and the key each used to compute that list
themselves. Two copies of an ordering is one refactor away from explaining a different statement
than the one on screen, with nothing on screen to say so.
"""
import ast
import inspect

import pytest

from serenedash import db, tui
from serenedash.views import activity_frame, activity_rows, view_hint

SAMPLE = {"states": {"active": 2},
          "queries": [("idle", "SELECT 1", 8, 3, 900, "11", "", ""),
                      ("active", "SELECT count(*) FROM pg_stat_activity", 37, 5, 60, "12", "", ""),
                      ("active", "SELECT slow FROM t", 18, 165862, 257242, "13", "", "")]}


def test_the_row_order_has_one_definition():
    # The regression this whole file exists for. `e` must not re-derive the sort.
    src = inspect.getsource(tui)
    assert "activity_rows(" in src, "the terminal must use the shared ordering"
    assert 'r[0] != "active"' not in src, "tui re-implements the activity sort; it will drift"


def test_the_cursor_index_means_the_same_row_in_both():
    rows = activity_rows(SAMPLE)
    # Oldest active first - the 46-hour statement, not whatever the server returned first.
    assert rows[0][5] == "13"
    assert rows[0][3] == 165862
    # pg_stat_activity's own row is never offered.
    assert all("pg_stat_activity" not in r[1] for r in rows)
    out = activity_frame(SAMPLE, False, 100, 0, sel=0, open_=True)
    assert any("SELECT slow FROM t" in ln for ln in out), "the frame opened a different row"


def test_full_text_wins_over_the_sampled_head():
    full = [("active", "SELECT the whole thing", 22, 9, 9, "99", "", "")]
    rows = activity_rows(SAMPLE, full)
    assert [r[5] for r in rows] == ["99"]


def test_ended_statements_arrive_in_the_same_shape():
    ended = [{"statement": "SELECT gone", "chars": 11, "ran_for_s": 4, "pid": "77"}]
    rows = activity_rows(SAMPLE, None, ended)
    assert all(len(r) >= 8 for r in rows), "an ended row must be indexable like a live one"
    assert [r for r in rows if r[5] == "77"][0][0] == "ended"


def test_a_plan_replaces_the_statement_rather_than_following_it():
    plan = {"plan": ["TOP_N", "IRESEARCH_SCAN"], "chars": 68217}
    out = activity_frame(SAMPLE, False, 100, 0, sel=0, open_=True, plan=plan)
    body = "\n".join(out)
    assert "IRESEARCH_SCAN" in body
    assert "SELECT slow FROM t" not in body, "both are long; showing both buries the plan"
    assert "68,217 chars planned" in body


def test_a_failed_plan_says_why_on_screen():
    out = activity_frame(SAMPLE, False, 100, 0, sel=0, open_=True,
                         plan={"error": "explain failed", "detail": "Parser Error: nope"})
    body = "\n".join(out)
    assert "explain failed" in body and "Parser Error" in body


def test_the_plan_is_not_wrapped():
    # DuckDB's plan is box-drawing art whose alignment IS the tree. Wrapping it makes confetti.
    wide = "|" + "-" * 400 + "|"
    out = activity_frame(SAMPLE, False, 100, 0, sel=0, open_=True, plan={"plan": [wide]})
    assert sum(1 for ln in out if "---" in ln) == 1, "the plan row was wrapped into several"
    assert any("not wrapped" in ln for ln in out), "and it has to say the row was cut"


def test_the_hint_names_e_only_where_it_does_something():
    assert " e " not in view_hint("activity", {})            # the list: nothing is open to plan
    assert " e " in view_hint("activity", {"open": True})
    assert "explains it" in view_hint("activity", {"open": True})
    assert "shows the statement" in view_hint("activity", {"open": True, "plan": {"plan": []}})
    assert " e " not in view_hint("findings", {"open": True})
    assert " e " not in view_hint("mcp", {"open": 1})


@pytest.mark.parametrize("sql", ["", "   ", ";"])
def test_nothing_to_explain_is_an_error_not_a_crash(sql):
    assert db.explain({"port": 1, "password": "x"}, sql)["error"] == "nothing to explain"


def test_a_write_is_refused_before_it_reaches_the_server():
    out = db.explain({"port": 1, "password": "x"}, "DELETE FROM m WHERE id = 7")
    assert "refused" in out["error"] and "delete" in out["error"]


def test_explaining_an_explain_is_refused():
    out = db.explain({"port": 1, "password": "x"}, "EXPLAIN SELECT 1")
    assert "already an EXPLAIN" in out["error"]


def test_credentials_are_checked_before_a_connection_is_attempted():
    # Port 1 would refuse instantly, but the message has to name the real problem.
    assert db.explain({"port": 1}, "SELECT 1")["error"] == "no credentials"


class _Cur:
    def __init__(self, rows):
        self.rows, self.description = rows, [("plan",)]
        self.executed = ""

    def execute(self, sql):
        self.executed = sql

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, rows):
        self.cur, self.read_only = _Cur(rows), False

    def cursor(self):
        return self.cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _driver(rows, seen):
    class D:
        @staticmethod
        def connect(**kw):
            seen.append(kw)
            cn = _Conn(rows)
            seen.append(cn)
            return cn
    return D


def test_the_statement_is_planned_read_only_and_without_its_semicolon(monkeypatch):
    seen = []
    monkeypatch.setattr(db, "pg_driver", lambda: _driver([("TOP_N",)], seen))
    out = db.explain({"port": 7890, "password": "x"}, "  SELECT 1 ;  ")
    assert out["plan"] == ["TOP_N"]
    conn = [x for x in seen if isinstance(x, _Conn)][0]
    assert conn.read_only is True, "EXPLAIN must not be the one path that connects writable"
    assert conn.cur.executed == "EXPLAIN SELECT 1"


def test_a_two_column_answer_takes_the_plan_not_the_label(monkeypatch):
    # DuckDB answers (explain_key, explain_value). Taking the first column gives "physical_plan".
    monkeypatch.setattr(db, "pg_driver", lambda: _driver([("physical_plan", "TOP_N\nSEQ_SCAN")], []))
    assert db.explain({"port": 7890, "password": "x"}, "SELECT 1")["plan"] == ["TOP_N", "SEQ_SCAN"]


def test_a_server_error_is_returned_not_raised(monkeypatch):
    class D:
        @staticmethod
        def connect(**kw):
            raise RuntimeError("Parser Error: syntax error at or near \"FROM\"")
    monkeypatch.setattr(db, "pg_driver", lambda: D)
    out = db.explain({"port": 7890, "password": "x"}, "SELECT FROM")
    assert out["error"] == "explain failed" and "Parser Error" in out["detail"]


def _branch_source(fn_name, needle):
    """The `e` handling out of tui's source - it lives inside main's loop and cannot be imported."""
    src = inspect.getsource(tui)
    ast.parse(src)                                     # it has to be valid before it is searched
    return [ln for ln in src.splitlines() if needle in ln]


def test_the_terminal_binds_e_only_while_a_statement_is_open():
    hit = [ln for ln in _branch_source("main", 'k == "e"') if "activity" in ln]
    assert hit, "no branch binds e on the activity view"
    assert 'anav.get("open")' in hit[0], "e on the list would plan whatever the cursor last touched"


def test_e_is_claimed_by_exactly_one_view_at_a_time():
    # `e` was already taken: the config view uses it. That is fine - they are different views - but
    # every branch that claims it has to say which view it is for, or the first one wins everywhere.
    for ln in _branch_source("main", 'k == "e"'):
        assert 'view == "config"' in ln or 'view == "activity"' in ln, (
            f"an unguarded `e` branch swallows the key for every view: {ln.strip()}")


def test_moving_the_cursor_drops_the_previous_plan():
    src = inspect.getsource(tui)
    assert src.count('"plan"] = None') >= 2, (
        "both front ends must clear the plan when the open statement changes; a stale plan under a "
        "different statement is worse than no plan")


def test_the_page_forwards_e_and_only_on_activity():
    from serenedash import serve
    assert "view === 'activity' && e.key === 'e'" in serve.PAGE
    # The page is a plain Python string. A control character here becomes a real byte in the JS.
    js = serve.PAGE[serve.PAGE.index("keydown"):]
    assert "\r" not in js and "\x1b" not in js


def test_the_served_panel_draws_the_plan_it_is_given():
    # view_lines is the dispatch the page and the exporter share. The terminal has its own, so a
    # feature wired into one and not the other is the `logs` bug again: the view loads and is wrong.
    from .test_timing import _args  # noqa: TID252

    st, _prev, sz, hist, perf, thr, tcpu, hinfo, _c, _w, _h = _args(120, 44)
    nav = {"scroll": 0, "sel": 0, "open": True, "plan": {"plan": ["IRESEARCH_SCAN"], "chars": 66}}
    out = tui.view_lines("activity", {}, None, ["MAIN"], SAMPLE, sz, hist, perf, thr, tcpu, hinfo,
                         {"server": {}, "indexes": []}, False, 120, nav=nav)
    assert any("IRESEARCH_SCAN" in ln for ln in out), "the served activity view ignores the plan"


def _nav(monkeypatch, plan=None):
    monkeypatch.setattr(tui, "full_queries", lambda _cfg: SAMPLE["queries"])
    monkeypatch.setattr(tui, "explain", lambda _cfg, sql: plan or {"plan": ["TOP_N"],
                                                                  "chars": len(sql)})


def test_e_reaches_the_reducer_only_with_a_statement_open(monkeypatch):
    _nav(monkeypatch)
    closed = {"view": "activity", "sel": 0, "open": None}
    assert tui._web_nav(closed, "e", "", {}) == closed, "e on the list must be inert"
    opened = tui._web_nav({"view": "activity", "sel": 0, "open": True}, "e", "", {})
    assert opened["plan"]["plan"] == ["TOP_N"]


def test_e_plans_the_row_under_the_cursor_not_the_first_one(monkeypatch):
    # The cursor is an index into the SORTED list, so sel=0 is the 46-hour statement, not the idle
    # one the server happened to return first. Getting this wrong plans a statement nobody asked
    # about and says nothing.
    seen = []
    monkeypatch.setattr(tui, "full_queries", lambda _cfg: SAMPLE["queries"])
    monkeypatch.setattr(tui, "explain", lambda _cfg, sql: seen.append(sql) or {"plan": []})
    tui._web_nav({"view": "activity", "sel": 0, "open": True}, "e", "", {})
    assert seen == ["SELECT slow FROM t"]


def test_e_toggles_rather_than_replanning(monkeypatch):
    calls = []
    monkeypatch.setattr(tui, "full_queries", lambda _cfg: SAMPLE["queries"])
    monkeypatch.setattr(tui, "explain", lambda _cfg, sql: calls.append(sql) or {"plan": ["X"]})
    st = tui._web_nav({"view": "activity", "sel": 0, "open": True}, "e", "", {})
    st = tui._web_nav(st, "e", "", {})
    assert st["plan"] is None and len(calls) == 1, "the second press must not hit the server again"


def test_the_cursor_moving_clears_the_plan_on_the_page(monkeypatch):
    _nav(monkeypatch)
    st = tui._web_nav({"view": "activity", "sel": 0, "open": True, "_n": 2}, "e", "", {})
    assert st["plan"]
    assert tui._web_nav(st, "esc", "", {}).get("plan") is None


def test_e_is_ignored_on_views_that_cannot_plan(monkeypatch):
    _nav(monkeypatch)
    for view in ("mcp", "findings", "logs"):
        st = {"view": view, "sel": 0, "open": True}
        assert tui._web_nav(st, "e", "", {}) == st, f"e did something on {view}"

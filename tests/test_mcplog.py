"""The other side of the pipe.

An MCP server has no window and no log of its own, so an agent reading this deployment wrong is
invisible from here. It was invisible in exactly that way once: a local model read `status()` and
reported three conclusions the findings do not support, and the only reason anyone found out is
that the answer was pasted into a chat by hand.

Both halves are recorded for that reason. The call says what an agent looked at; only the reply
says what it was told, and the gap between them is the thing worth seeing.
"""
import json
import os

from serenedash import mcplog
from serenedash.fmt import strip
from serenedash.views import DETAIL, KEYS, mcp_frame, mcp_nav


def call(perf, tool="status", **kw):
    return mcplog.record(perf, tool, kw.pop("args", {}), kw.pop("ms", 10.0),
                         kw.pop("reply", {"ok": True}), **kw)


def test_a_call_and_its_reply_are_both_recorded(tmp_path):
    perf = str(tmp_path)
    call(perf, "query", args={"sql": "select 1"}, reply={"rows": [[1]]})
    r = mcplog.tail(perf)[0]
    assert r["tool"] == "query" and "select 1" in r["args"]
    assert json.loads(r["reply"]) == {"rows": [[1]]}
    assert r["bytes"] > 0 and r["ok"] is True


def test_a_failed_call_is_kept_with_its_error(tmp_path):
    # The call worth seeing most. A tool that raised is not absent from the log; it is in it,
    # marked, with what it said.
    perf = str(tmp_path)
    mcplog.record(perf, "query", {"sql": "delete from t"}, 3.0, "", ok=False,
                  err="refused: delete is not a read-only statement")
    r = mcplog.tail(perf)[0]
    assert r["ok"] is False and "read-only" in r["error"]


def test_a_huge_reply_is_truncated_rather_than_dropped(tmp_path):
    # status() is tens of kilobytes. Keeping it whole would make the file hundreds of megabytes;
    # dropping it would lose the only record of what an agent was actually told.
    perf = str(tmp_path)
    call(perf, reply={"big": "x" * 50_000})
    r = mcplog.tail(perf)[0]
    assert len(r["reply"]) <= mcplog.REPLY_CHARS + 1
    assert r["bytes"] > 50_000, "the true size has to survive the truncation"


def test_the_log_is_bounded(tmp_path):
    perf = str(tmp_path)
    for i in range(mcplog.KEEP + 40):
        call(perf, "status", reply={"i": i})
    rows = mcplog.tail(perf)
    assert len(rows) == mcplog.KEEP
    assert json.loads(rows[-1]["reply"])["i"] == mcplog.KEEP + 39, "it keeps the TAIL"


def test_a_torn_line_does_not_take_the_view_down(tmp_path):
    # Several servers append to one file, and a reader can arrive mid-write.
    perf = str(tmp_path)
    call(perf)
    with open(mcplog.path(perf), "a") as f:
        f.write('{"t": 1, "tool": "sta')
    assert len(mcplog.tail(perf)) == 1


def test_recording_never_breaks_the_tool_it_records(tmp_path):
    # A dashboard's log must not be able to fail a tool call. Unwritable directory, no exception.
    d = tmp_path / "ro"
    d.mkdir()
    os.chmod(d, 0o500)
    try:
        assert mcplog.record(str(d / "sub" / "deeper"), "status", {}, 1.0, {}) in (True, False)
    finally:
        os.chmod(d, 0o700)


def test_the_client_is_the_parent_process_because_the_protocol_has_no_name(tmp_path):
    # MCP over stdio carries no client identity. The process tree does, and it was enough to tell
    # `claude --continue` from yesterday apart from a local qwen.
    perf = str(tmp_path)
    call(perf)
    assert mcplog.tail(perf)[0]["client"], "no client attribution at all"


def test_live_servers_are_read_from_proc_not_from_the_log():
    # A process that has connected and answered nothing appears nowhere else, and "an agent is
    # connected but has asked nothing" is a real state.
    for p in mcplog.live():
        assert {"pid", "client", "uptime_s"} <= set(p)
        assert p["uptime_s"] >= 0


def rows(n=3):
    return [{"t": 1785000000 + i * 60, "tool": t, "ms": ms, "ok": ok, "client": "claude --continue",
             "pid": 7, "args": ar, "bytes": by, "reply": rp}
            for i, (t, ms, ok, ar, by, rp) in enumerate([
                ("status", 812.0, True, "", 18422, '{"findings": []}'),
                ("search", 120.0, True, "", 3011, '{"indexes": {}}'),
                ("query", 41.0, True, "sql=select 1", 88, '{"rows": [[1]]}')][:n])]


def test_the_view_opens_at_the_session_list():
    # Level 1. Which agents are talking to this deployment is the question you have before you have
    # any other question, and it is the only one that fits on a screen.
    out = [strip(x) for x in mcp_frame(rows(), [], False, 120, 0, 0, 30)]
    assert any("session" in ln for ln in out)
    assert any("enter opens the session" in ln for ln in out)


def test_a_session_expands_to_its_own_calls():
    # Level 2. Grouped by pid, because four `claude` sessions look identical from /proc and are
    # four different conversations.
    out = [strip(x) for x in mcp_frame(rows(), [], False, 120, 0, 0, 30, open_pid=7)]
    assert any("pid 7" in ln for ln in out)
    assert any("enter shows the whole call" in ln for ln in out)
    assert sum(1 for ln in out if "status" in ln or "search" in ln or "query" in ln) >= 3


def test_a_call_opens_in_full_with_its_reply():
    # Level 3. The reply is the point of the whole view and it is thousands of characters, so it
    # gets a frame rather than a corner of one.
    out = [strip(x) for x in mcp_frame(rows(), [], False, 120, 0, 0, 30, open_pid=7, call_sel=2,
                                       popup=True)]
    assert out[0].startswith("┌─ query"), out[0]
    assert any('"rows"' in ln for ln in out)
    assert any("esc closes" in ln for ln in out)


def test_a_truncated_reply_is_still_readable():
    # Every big reply is STORED truncated, so it does not parse - and the one that matters most,
    # status(), rendered as a single 12000-character line. Indented structurally instead.
    r = {"reply": '{"findings": [{"what": "orphaned temp files", "detail": "72.6G {not a brace}"'}
    lines = mcplog.pretty(r, 100)
    assert lines[0] == "{" and lines[1].strip() == '"findings": ['
    assert any(ln.strip() == '"what": "orphaned temp files",' for ln in lines)
    # A brace inside a message must not change the depth.
    assert any('{not a brace}' in ln for ln in lines)


def test_every_level_stays_inside_the_height_it_was_given():
    many = [{"t": 1785000000 + i, "tool": "status", "ms": 10, "ok": True, "client": "c", "pid": 7,
             "args": "", "bytes": 10, "reply": '{"a": ' + '"x",' * 900} for i in range(400)]
    for h in (12, 24, 44):
        for kw in ({}, {"open_pid": 7}, {"open_pid": 7, "popup": True}):
            assert len(mcp_frame(many, [], False, 100, 0, 0, h, **kw)) <= h, (h, kw)


def test_minus_one_keeps_selecting_the_newest_as_calls_arrive():
    # The same rule as the log tailer: an absolute index slides onto a different call every time an
    # agent asks something, so the row under the cursor is not the one you were looking at.
    a = [strip(x) for x in mcp_frame(rows(2), [], False, 120, 0, 0, 30, open_pid=7, call_sel=-1,
                                     popup=True)]
    b = [strip(x) for x in mcp_frame(rows(3), [], False, 120, 0, 0, 30, open_pid=7, call_sel=-1,
                                     popup=True)]
    assert a[0].startswith("┌─ search") and b[0].startswith("┌─ query")


def test_a_session_that_asked_nothing_is_shown_as_such():
    # An agent that connected and has asked nothing appears in /proc and nowhere in the log, and
    # "asked nothing" is not "0 calls" - one is a state, the other reads like a measurement.
    out = [strip(x) for x in mcp_frame([], [{"pid": 9, "client": "claude", "uptime_s": 90000}],
                                       False, 120, 0, 0, 20)]
    assert any("asked nothing" in ln and "pid 9" in ln for ln in out)


def test_an_empty_log_explains_itself_instead_of_rendering_nothing():
    out = [strip(x) for x in mcp_frame([], [], False, 100, 0, 0, 20)]
    assert any("No agent has connected" in ln for ln in out)
    assert "exits with the session" in " ".join(ln.strip() for ln in out)


def test_the_view_has_a_key_and_appears_in_the_bar():
    assert DETAIL["mcp"] == "n"
    assert ("n", "mcp") in KEYS, "a view with no key on the bar is a view nobody finds"


def test_a_row_says_what_came_back_not_how_many_bytes(tmp_path):
    # The screenshot that prompted this: every row said "18.0K" and the reply pane showed two
    # wrapped lines of braces. Bytes are not something an agent could have acted on.
    perf = str(tmp_path)
    mcplog.record(perf, "status", {}, 900.0,
                  {"findings": [{"what": "orphaned temp files"}, {"what": "WAL is 27.7G"}]})
    assert mcplog.digest(mcplog.tail(perf)[0]) == "2 findings: orphaned temp files, WAL is 27.7G"


def test_the_summary_survives_a_reply_too_big_to_store(tmp_path):
    # status() is tens of kilobytes, so the stored copy is truncated JSON that will not parse - the
    # one reply most worth summarising was the one that fell back to showing raw braces. Summarised
    # at record time, with the whole object still in hand.
    perf = str(tmp_path)
    mcplog.record(perf, "status", {}, 900.0,
                  {"findings": [{"what": "orphaned temp files", "detail": "x" * 50_000}]})
    r = mcplog.tail(perf)[0]
    assert r["reply"].endswith("…"), "this test is pointless if the reply was not truncated"
    assert mcplog.digest(r) == "1 findings: orphaned temp files"


def test_a_refusal_is_not_prefixed_twice(tmp_path):
    perf = str(tmp_path)
    mcplog.record(perf, "query", {"sql": "delete from t"}, 1.0,
                  {"error": "refused: delete is not a read-only statement"})
    assert mcplog.digest(mcplog.tail(perf)[0]).count("refused") == 1


def test_the_reply_is_indented_rather_than_wrapped(tmp_path):
    # Wrapped JSON is a wall of punctuation; the structure is the readable part and a reflow
    # destroys exactly that. Long lines are cut instead, which keeps the keys.
    perf = str(tmp_path)
    mcplog.record(perf, "host", {}, 1.0, {"cores": 24, "load": ["1", "2"]})
    lines = mcplog.pretty(mcplog.tail(perf)[0])
    assert lines[0] == "{" and any(ln.startswith('  "cores"') for ln in lines)


def test_arguments_left_at_their_default_are_not_recorded():
    # `max_rows=200, max_chars=20000` on every query() pushed the SQL off the end of the line.
    from serenedash import mcp_server

    seen = {}
    mcp_server.mcplog.record = lambda perf, tool, args, *a, **kw: seen.update(args=args)
    mcp_server._SERVING = True
    try:
        @mcp_server.stamped
        def query(sql: str, max_rows: int = 200):
            return {"rows": []}
        query("select 1")
        assert seen["args"] == {"sql": "select 1"}
        query("select 1", max_rows=5)
        assert seen["args"] == {"sql": "select 1", "max_rows": 5}
    finally:
        mcp_server._SERVING = False
        import importlib
        importlib.reload(mcp_server.mcplog)


def test_nothing_is_recorded_unless_a_client_is_on_the_other_end():
    # A test calling stamped(lambda: [1, 2]) put a `<lambda>` row in the real log. Recording starts
    # when the server starts serving, which is the only moment a call means an agent asked.
    from serenedash import mcp_server

    assert mcp_server._SERVING is False, "importing this module must not turn recording on"


# ---- the navigation reducer, which BOTH front ends drive ---------------------------------------
# It exists because the page answered to none of these keys: j and k worked in the terminal and did
# nothing in a browser, on the one view built around moving through a list.

def nav_rows(pid=7, n=4):
    return [{"t": 1785000000 + i, "tool": "status", "ms": 1, "ok": True, "pid": pid,
             "client": "claude", "args": "", "bytes": 9, "reply": "{}"} for i in range(n)]


def start():
    return {"scroll": 0, "sel": 0, "open": None, "call": -1, "popup": False}


def test_j_and_k_move_the_session_cursor():
    rows = nav_rows(7) + nav_rows(9)
    n = mcp_nav(start(), "j", rows)
    assert n["sel"] == 1
    assert mcp_nav(n, "k", rows)["sel"] == 0


def test_the_cursor_stops_at_both_ends_rather_than_wrapping():
    rows = nav_rows(7) + nav_rows(9)
    n = start()
    for _ in range(10):
        n = mcp_nav(n, "j", rows)
    assert n["sel"] == 1, "two sessions, so 1 is the last one"
    for _ in range(10):
        n = mcp_nav(n, "k", rows)
    assert n["sel"] == 0


def test_enter_descends_and_escape_climbs_back():
    rows = nav_rows(7)
    opened = mcp_nav(start(), "\r", rows)
    assert opened["open"] == 7
    popped = mcp_nav(opened, "\r", rows)
    assert popped["popup"] is True
    assert mcp_nav(popped, "\x1b", rows)["popup"] is False
    assert mcp_nav(opened, "\x1b", rows)["open"] is None


def test_escape_with_nothing_open_is_not_ours():
    # None is how the caller tells "I handled it" from "this Esc is yours", which is what keeps Esc
    # leaving the view when nothing is open.
    assert mcp_nav(start(), "\x1b", nav_rows()) is None


def test_inside_the_box_j_scrolls_the_reply_and_enter_does_nothing():
    # A key that looks like it should do something and does not is worse than one that is not bound.
    rows = nav_rows(7)
    box = mcp_nav(mcp_nav(start(), "\r", rows), "\r", rows)
    scrolled = mcp_nav(box, "j", rows)
    assert scrolled["scroll"] == 1 and scrolled["popup"] is True
    assert mcp_nav(scrolled, "\r", rows)["scroll"] == 1, "enter must not descend or reset"
    assert mcp_nav(scrolled, "k", rows)["scroll"] == 0
    assert mcp_nav(start() | {"popup": True}, "k", rows)["scroll"] == 0, "no scrolling past the top"


def test_the_call_cursor_follows_the_newest_until_you_move_it():
    rows = nav_rows(7, 4)
    opened = mcp_nav(start(), "\r", rows)
    assert opened["call"] == -1, "-1 means the newest and keeps meaning it as calls arrive"
    up = mcp_nav(opened, "k", rows)
    assert up["call"] == 2
    assert mcp_nav(up, "j", rows)["call"] == -1, "back on the newest resumes following it"
    assert mcp_nav(up, "end", rows)["call"] == -1


def test_the_reducer_never_mutates_what_it_was_given():
    # Both front ends hold their own copy; a reducer that mutated would move a terminal's cursor
    # when a browser pressed a key.
    n = start()
    mcp_nav(n, "j", nav_rows())
    assert n == start()


def test_a_page_key_goes_through_the_same_reducer_as_a_terminal_key(tmp_path):
    # The actual bug: two front ends, one meaning per key. The page's copy was an empty set.
    from serenedash.tui import NAV_KEYS as TUI_NAV, _web_nav

    perf = str(tmp_path)
    mcplog.record(perf, "status", {}, 1.0, {"findings": []})
    st = {"view": "mcp", "id": "s1", **start()}
    # Not asserting WHICH session opens: this machine has real serenedash-mcp processes running and
    # they are sessions too. What must hold is that enter descends and esc climbs back.
    opened = _web_nav(st, "\r", perf)
    assert opened["open"] is not None
    assert _web_nav(opened, "\x1b", perf)["open"] is None
    assert _web_nav(opened, "j", perf)["open"] == opened["open"], "j inside a session stays in it"
    # Esc with nothing left open leaves the view, which is what it does in the terminal too.
    assert _web_nav(st, "\x1b", perf)["view"] == "main"
    assert "j" in TUI_NAV and "\x1b" in TUI_NAV


def test_a_view_with_nothing_to_move_does_not_swallow_the_keys(tmp_path):
    # `j` is not bound on the storage panel, and a page that took it to do nothing would be worse
    # than one that let it fall through.
    from serenedash.tui import NAVIGABLE, _web_nav

    st = {"view": "storage", "id": "s1", **start()}
    assert _web_nav(st, "j", str(tmp_path)) == st
    assert "storage" not in NAVIGABLE and "mcp" in NAVIGABLE


# ---- failures have to be impossible to miss ----------------------------------------------------
# Found on a real session: nine calls, three failed SQL, and nothing anywhere said so.

FAILED_REPLY = {"error": "query failed",
                "detail": 'Table with name pg_compression does not exist!\nDid you mean "pg_conversion"?'}


def test_a_tool_that_returns_an_error_did_not_succeed(tmp_path):
    # `ok` cannot come from "did this raise": a tool that cannot answer returns {"error": ...}, so
    # the pipe worked and the call did not. It said True on three failed queries in a row.
    perf = str(tmp_path)
    mcplog.record(perf, "query", {"sql": "select bogus"}, 40.0, FAILED_REPLY)
    r = mcplog.tail(perf)[0]
    assert r["ok"] is False and mcplog.failed(r)


def test_the_digest_carries_the_message_not_the_category(tmp_path):
    # "query failed" told a reader nothing. The server's own message is in `detail`.
    perf = str(tmp_path)
    mcplog.record(perf, "query", {"sql": "select bogus"}, 40.0, FAILED_REPLY)
    assert mcplog.digest(mcplog.tail(perf)[0]) == (
        "query failed: Table with name pg_compression does not exist!")


def test_a_row_written_before_ok_was_trustworthy_still_reads_as_failed(tmp_path):
    # The log outlives the rule that wrote it: these rows are in the real file right now, marked
    # ok=True with a summary of "query failed".
    perf = str(tmp_path)
    with open(mcplog.path(perf), "w") as f:
        f.write(json.dumps({"t": 1785000000, "tool": "query", "ms": 40.0, "ok": True, "pid": 7,
                            "client": "claude", "args": "sql=select bogus", "bytes": 240,
                            "summary": "query failed",
                            "reply": json.dumps(FAILED_REPLY)}) + "\n")
    r = mcplog.tail(perf)[0]
    assert mcplog.failed(r), "an older row must not read as a success"
    assert "does not exist" in mcplog.digest(r), "and it must still say why"


def test_a_failure_is_marked_in_the_list_and_counted_in_both_headers():
    good = {"t": 1785000000, "tool": "query", "ms": 5, "ok": True, "pid": 7, "client": "c",
            "args": "sql=select 1", "bytes": 9, "reply": '{"rows": [[1]]}', "summary": "1 rows"}
    bad = {**good, "t": 1785000060, "ok": False, "reply": json.dumps(FAILED_REPLY),
           "args": "sql=select bogus", "summary": "query failed"}
    calls = [strip(x) for x in mcp_frame([good, bad], [], False, 150, 0, 0, 20, open_pid=7)]
    marked = [ln for ln in calls if "✗" in ln]
    assert len(marked) == 1, "a glyph, not only a colour - colour is gone in a screenshot"
    assert "does not exist" in marked[0], "the reason has to be ON the row"
    assert any("1 failed" in ln for ln in calls), "the session header has to carry the count"
    sess = [strip(x) for x in mcp_frame([good, bad], [], False, 150, 0, 0, 20)]
    assert any("1 failed" in ln for ln in sess), "and so does the level you look at first"


def test_the_reason_comes_before_the_sql_on_a_failed_row():
    # A 120-character SELECT pushed the message off the end of the line, which is the whole content
    # of a failure. Argument-then-result is the right order only for a call that worked.
    bad = {"t": 1785000000, "tool": "query", "ms": 5, "ok": False, "pid": 7, "client": "c",
           "args": "sql=" + "SELECT table_name, column_name FROM information_schema.columns " * 3,
           "bytes": 9, "reply": json.dumps(FAILED_REPLY), "summary": "query failed"}
    row = next(strip(x) for x in mcp_frame([bad], [], False, 150, 0, 0, 20, open_pid=7)
               if "✗" in strip(x))
    assert row.index("does not exist") < row.index("SELECT")


# ---- the findings screen ------------------------------------------------------------------------

FOUND = [{"kind": "storage", "what": "orphaned temp files", "detail": "24 files, 72.6G. Older.",
          "bytes_reclaimable": 77975027712, "fix": "delete the files",
          "verify": "select count(*) from duckdb_temporary_files()"},
         {"kind": "memory", "what": "process memory paged out", "detail": "70.9G in swap."},
         {"kind": "setting", "what": "setting: checkpoint_threshold", "detail": "WAL is 27.7G."}]


def test_the_summary_line_counts_by_kind():
    from serenedash.views import findings_frame

    head = strip(findings_frame(FOUND, False, 140, 0, 0, 20)[0])
    assert "Status summary" in head and "3 findings" in head
    assert "1 storage" in head and "1 memory" in head and "1 setting" in head


def test_nothing_tripped_is_stated_rather_than_left_blank():
    from serenedash.views import findings_frame

    out = [strip(x) for x in findings_frame([], False, 100, 0, 0, 20)]
    assert "nothing tripped" in out[0]
    assert any("not an absence of one" in ln for ln in out)


def test_opening_a_finding_shows_the_numbers_and_the_fix():
    # A finding is an argument and the reader is meant to be able to disagree with it, which needs
    # the operands rather than a summary of them.
    from serenedash.views import findings_frame

    out = [strip(x) for x in findings_frame(FOUND, False, 140, 0, 0, 30, True)]
    assert any("orphaned temp files" in ln for ln in out)
    assert any("bytes_reclaimable" in ln for ln in out)
    assert any("delete the files" in ln for ln in out)
    assert any("duckdb_temporary_files" in ln for ln in out)


def test_the_findings_screen_reports_where_it_put_each_row():
    # Clicking is not scanning: a row is whatever the data says, with no shape to match, so the
    # frame reports the line it drew each item on and the server draws a box there.
    from serenedash.views import findings_frame

    anchors = []
    lines = findings_frame(FOUND, False, 140, 0, 0, 20, anchors=anchors)
    assert len(anchors) == 3
    for row, item in anchors:
        assert FOUND[item]["what"][:20] in strip(lines[row])


def test_a_click_selects_and_opens_that_row():
    from serenedash.views import findings_nav

    n = findings_nav({"scroll": 0, "sel": 0, "open": False}, "sel:2", FOUND)
    assert n["sel"] == 2 and n["open"] is True


def test_findings_has_a_key_and_follow_moved_off_it():
    # `f` was the logs follow toggle. A letter that means one thing globally and another inside one
    # panel means two things; follow is on space now, which is what a pager uses anyway.
    assert DETAIL["findings"] == "f" and ("f", "findings") in KEYS

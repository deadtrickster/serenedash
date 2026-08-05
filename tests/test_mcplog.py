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
from serenedash.views import DETAIL, KEYS, mcp_frame


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
             "args": ar, "bytes": by, "reply": rp}
            for i, (t, ms, ok, ar, by, rp) in enumerate([
                ("status", 812.0, True, "", 18422, '{"findings": []}'),
                ("search", 120.0, True, "", 3011, '{"indexes": {}}'),
                ("query", 41.0, True, "sql=select 1", 88, '{"rows": [[1]]}')][:n])]


def test_the_view_shows_the_reply_of_the_selected_call():
    out = [strip(x) for x in mcp_frame(rows(), [], False, 120, 0, -1, 30)]
    assert any("reply to query" in ln for ln in out)
    # Indented, so the value is on its own line under its key rather than inside a wrapped blob.
    assert any(ln.strip() == '"rows": [' for ln in out)
    assert any(ln.strip() == "1" for ln in out)


def test_minus_one_keeps_selecting_the_newest_as_calls_arrive():
    # The same rule as the log tailer: an absolute index slides onto a different call every time an
    # agent asks something.
    a = [strip(x) for x in mcp_frame(rows(2), [], False, 120, 0, -1, 30)]
    b = [strip(x) for x in mcp_frame(rows(3), [], False, 120, 0, -1, 30)]
    assert any("reply to search" in ln for ln in a)
    assert any("reply to query" in ln for ln in b)


def test_an_empty_log_explains_itself_instead_of_rendering_nothing():
    out = [strip(x) for x in mcp_frame([], [], False, 100, 0, -1, 20)]
    assert any("nothing has called" in ln for ln in out)
    assert any("recorded by the MCP server itself" in ln for ln in out)


def test_the_frame_never_exceeds_the_height_it_was_given():
    many = [{"t": 1785000000 + i, "tool": "status", "ms": 10, "ok": True, "client": "c",
             "args": "", "bytes": 10, "reply": "x" * 4000} for i in range(400)]
    for h in (10, 24, 44):
        assert len(mcp_frame(many, [], False, 100, 0, -1, h)) <= h


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

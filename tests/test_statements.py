"""What ran, kept from what was running.

`pg_stat_activity` is present-tense. This server has no `pg_stat_statements`, `log_query_path` is
empty and profiling is off, so once a statement ends there is no trace it existed: "why was the
server slow an hour ago" is unanswerable while "why is it slow now" was answerable all along.

The dashboard samples that view every tick anyway, so recording what it saw costs the server
nothing. What it CANNOT say has to be said rather than implied by silence - it is sampled, so a
statement shorter than one tick is invisible and every duration is a lower bound.
"""
import json
import time

from serenedash import statements as st


def row(pid="7", age=100, head="select 1", n=8, conn=100, addr="", app=""):
    return ("active", head, n, age, conn, pid, addr, app)


def sample(rows, t=1785000000.0):
    return {"t": t, "queries": list(rows)}


def test_a_statement_seen_twice_is_one_row_not_two():
    # Identity is (pid, start), and start is derived: the server gives an age, so `t - age` is when
    # it began. Without that every tick would record the same statement again.
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        st.observe(d, sample([row(age=100)], t=1000.0))
        st.observe(d, sample([row(age=105)], t=1005.0))
        got = st.recent(d)
        assert len(got) == 1
        assert got[0]["samples"] == 2
        assert got[0]["ran_for_s"] == 105, "the duration is the last age seen, not the first"


def test_two_statements_on_one_pooled_connection_are_two_rows(tmp_path):
    d = str(tmp_path)
    st.observe(d, sample([row(pid="7", age=100, head="select a")], t=1000.0))
    st.observe(d, sample([row(pid="7", age=5, head="select b")], t=1200.0))
    got = {r["statement"] for r in st.recent(d)}
    assert got == {"select a", "select b"}, "the same pid ran two different statements"


def test_a_statement_that_stops_being_seen_is_reported_as_ended(tmp_path):
    d = str(tmp_path)
    st.observe(d, sample([row(pid="7", age=900)], t=1000.0))
    live, ended = st.running(st.recent(d), now=1002.0, interval=5.0)
    assert len(live) == 1 and not ended
    # Two intervals later it has not been seen again.
    live, ended = st.running(st.recent(d), now=1020.0, interval=5.0)
    assert not live and len(ended) == 1
    assert ended[0]["ran_for_s"] == 900, "the duration survives the statement"


def test_one_slow_tick_does_not_declare_everything_finished(tmp_path):
    # A tick that ran long - doctor shells out, activity refetches - would otherwise bury every
    # statement and then resurrect it on the next pass.
    d = str(tmp_path)
    st.observe(d, sample([row()], t=1000.0))
    live, ended = st.running(st.recent(d), now=1006.0, interval=5.0)
    assert len(live) == 1 and not ended


def test_only_active_statements_are_recorded(tmp_path):
    d = str(tmp_path)
    st.observe(d, sample([("idle", "select 1", 8, 900, 900, "7", "", ""),
                          ("active", "select 2", 8, 900, 900, "8", "", "")]))
    assert [r["pid"] for r in st.recent(d)] == ["8"]


def test_the_dashboards_own_query_is_not_recorded(tmp_path):
    d = str(tmp_path)
    st.observe(d, sample([row(head="select * from pg_stat_activity where pid <> 1")]))
    assert st.recent(d) == []


def test_a_row_with_no_age_is_skipped_rather_than_recorded_as_new_every_tick(tmp_path):
    # -1 means the server did not say. Treating it as 0 would make `t - age` the current second,
    # so every tick would be a different key and the file would fill with duplicates.
    d = str(tmp_path)
    st.observe(d, sample([row(age=-1)], t=1000.0))
    st.observe(d, sample([row(age=-1)], t=1005.0))
    assert st.recent(d) == []


def test_the_record_is_bounded(tmp_path):
    d = str(tmp_path)
    for i in range(st.KEEP + 50):
        st.observe(d, sample([row(pid=str(i), age=10)], t=1000.0 + i))
    assert len(st.recent(d)) == st.KEEP


def test_the_longest_running_is_first(tmp_path):
    d = str(tmp_path)
    st.observe(d, sample([row(pid="1", age=10), row(pid="2", age=155068), row(pid="3", age=900)]))
    assert [r["ran_for_s"] for r in st.recent(d)] == [155068, 900, 10]


def test_a_torn_line_does_not_lose_the_rest_of_the_file(tmp_path):
    d = str(tmp_path)
    st.observe(d, sample([row(pid="7")]))
    with open(st.path(d), "a") as f:
        f.write('{"key": "8:900", "pid": "8"')
    assert len(st.recent(d)) == 1


def test_recording_never_breaks_the_tick(tmp_path):
    # Silent on failure: a dashboard's notebook must not be able to break the dashboard.
    assert st.observe(str(tmp_path), None) == 0
    assert st.observe(str(tmp_path), {"queries": []}) == 0
    assert st.observe("/proc/nonexistent/nope", sample([row()])) == 1   # recorded, save failed


def test_the_head_is_kept_at_its_longest(tmp_path):
    # The same statement can arrive truncated at different lengths on different ticks; being seen
    # again must never make a row less informative.
    d = str(tmp_path)
    st.observe(d, sample([row(age=100, head="select a, b")], t=1000.0))
    st.observe(d, sample([row(age=105, head="select a")], t=1005.0))
    assert st.recent(d)[0]["statement"] == "select a, b"


def test_the_file_is_replaced_rather_than_appended_so_a_reader_sees_whole_lines(tmp_path):
    d = str(tmp_path)
    st.observe(d, sample([row(pid=str(i)) for i in range(5)]))
    with open(st.path(d)) as f:
        for ln in f:
            json.loads(ln)          # every line parses, or this raises


def test_it_records_what_the_live_view_would_have_forgotten(tmp_path):
    # The whole point, end to end: a 43-hour statement observed once, then gone, still has its
    # duration and its text afterwards.
    d = str(tmp_path)
    now = time.time()
    st.observe(d, sample([row(pid="1265771991", age=155068, head="WITH lex AS (SELECT id, BM25(",
                              n=68209)], t=now))
    _live, ended = st.running(st.recent(d), now=now + 60, interval=5.0)
    assert len(ended) == 1
    assert ended[0]["ran_for_s"] == 155068 and ended[0]["chars"] == 68209
    assert ended[0]["statement"].startswith("WITH lex")


def test_a_wobbling_start_estimate_does_not_mint_a_new_record(tmp_path):
    # The start is derived: the age is evaluated inside the query and `now` is stamped when the
    # sample returns, so a tick that took 2.7s and one that took 40ms disagree about when the same
    # statement began. Seen live: one 45-hour statement recorded twice, 31 seconds apart.
    d = str(tmp_path)
    st.observe(d, sample([row(pid="7", age=100_000)], t=1_000_000.0))
    st.observe(d, sample([row(pid="7", age=100_003)], t=1_000_035.0))   # 32s of drift
    assert len(st.recent(d)) == 1


def test_two_runs_of_the_same_statement_are_two_records(tmp_path):
    # The other direction. Merging them would report two five-minute runs as one hour-long one.
    d = str(tmp_path)
    st.observe(d, sample([row(pid="7", age=300, head="select a")], t=1_000_000.0))
    st.observe(d, sample([row(pid="7", age=10, head="select a")], t=1_001_000.0))
    got = sorted(r["ran_for_s"] for r in st.recent(d))
    assert got == [10, 300], "one record per run"


def test_two_different_statements_started_seconds_apart_are_two_records(tmp_path):
    # A pool can start two statements on one connection inside the tolerance window, so the start
    # alone cannot separate them - the statement text has to as well.
    d = str(tmp_path)
    st.observe(d, sample([row(pid="7", age=10, head="select a")], t=1_000_000.0))
    st.observe(d, sample([row(pid="7", age=8, head="delete from b")], t=1_000_000.0))
    assert len(st.recent(d)) == 2

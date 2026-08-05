"""The search panel: sdb_metrics on screen, and the statement/temp-file facts that go with it.

Every case here is a way this could say something the data does not support, or a way it could move
the frame under the eye. The numbers in the fixtures are this deployment's, read off the live server
on 2026-08-04: two indexes, 11,216,808 live documents of 11,216,820 in 15 segments over 54.1 GiB,
avg_consolidation_time_ms at 16,893, and a RAGFlow statement of 68,209 characters carrying a
21,684-character embedding literal.
"""
import pytest

from serenedash.fmt import strip
from serenedash.views import (
    DETAIL,
    KEYS,
    LEGEND,
    activity_frame,
    biggest_literal,
    frame,
    memory_frame,
    qty,
    search_frame,
    storage_frame,
)

SIZES = [(200, 60), (150, 46), (120, 45), (100, 40), (96, 40), (80, 30)]


def metrics(indexes=2, compaction=0, pending=0):
    idx = {
        "2000801": {"num_docs": 11216820, "num_live_docs": 11216808, "num_buffered_docs": 0,
                    "num_segments": 15, "num_files": 91, "index_size": 58080860093,
                    "num_failed_commits": 0, "num_failed_cleanups": 0,
                    "num_failed_consolidations": 0, "avg_commit_time_ms": 227,
                    "avg_cleanup_time_ms": 0, "avg_consolidation_time_ms": 16893},
        "2000422": {"num_docs": 247665, "num_live_docs": 247665, "num_buffered_docs": 0,
                    "num_segments": 1, "num_files": 7, "index_size": 1267615750,
                    "num_failed_commits": 0, "num_failed_cleanups": 0,
                    "num_failed_consolidations": 0, "avg_commit_time_ms": 0,
                    "avg_cleanup_time_ms": 0, "avg_consolidation_time_ms": 0},
    }
    per = {f"{2000000 + i}": dict(idx["2000801"]) for i in range(indexes - 2)}
    per.update(dict(list(idx.items())[:indexes]))
    return {"server": {"pg_connections": 8, "http_connections": 0,
                       "refresh_active": 0, "refresh_pending": 0,
                       "compaction_active": compaction, "compaction_pending": pending,
                       "cleanup_active": 0, "cleanup_pending": 0},
            "indexes": per}


def state(sessions=3, tags=3, spill=None):
    return {
        "db": "d", "size": 80 * 2**30, "wal": 2**20, "mem": 34 * 2**30, "memlimit": 100 * 2**30,
        "blocks": (1000, 900, 100, 262144),
        "memtags": [(f"POOL_{i}", (10 - i) * 2**30) for i in range(tags)],
        "memspill": spill or {},
        "states": {"active": sessions, "idle": 2},
        "queries": [("active", f"SELECT {i}", 9) for i in range(sessions)],
        "settings": {"memory_limit": "100.0 GiB", "threads": "24"},
        "t": 0.0,
    }


def render(w, h, sea=metrics(), held=(0, 0), **kw):
    sz = {"duck": 2**35, "index": 2**34, "temp": 2**30, "total": 2**36,
          "temp_files": [(0.0, 2**30, "duckdb_temp_storage_S128K-0.tmp")], "temp_d": 0, "dt": 60}
    host = {"cores": 24, "load": ["1", "2", "3"], "threads": 100, "rss": 2**33, "swap": 0,
            "peak": 2**34, "uptime": 3600, "ram_total": 128 * 2**30, "pid": 1, "container": "c"}
    return frame(state(**kw), None, sz, {"mem": [34 * 2**30] * 10}, (None, [], {}),
                 [(50.0, "tid 1", "R", "1")], 60.0, host, False, w, h, None, sea, held)


def boxes(lines):
    return tuple(strip(ln).count("┌─") for ln in lines)


# ── the view itself ─────────────────────────────────────────────────────────────────────────────

def test_the_view_names_every_index_and_its_own_numbers():
    flat = strip("\n".join(search_frame(metrics(), False, 120, 0)))
    assert "2000801" in flat and "2000422" in flat
    assert "11,216,808" in flat, "live documents, exactly, where there is room for exactly"
    assert "12 deleted" in flat, "num_docs - num_live_docs, which is the row's whole point"
    assert "16.9s" in flat, "avg_consolidation_time_ms is why this view exists"
    assert "54.1G" in flat


def test_every_row_carries_the_denominator_it_divides_by():
    # A bar with an unnamed denominator is the mistake this repo keeps a rules file about: the
    # storage shares once divided by a total that appeared nowhere on the screen.
    flat = strip("\n".join(search_frame(metrics(), False, 120, 0)))
    assert "of 11,216,820 in the index" in flat, "the live-docs bar divides by num_docs"
    assert "across 2 indexes" in flat, "the size bar divides by every index sdb_metrics reports"


def test_a_recent_average_is_not_presented_as_a_lifetime_one():
    # The server's own description is "average time of the last few commits". A panel that prints
    # 16.9s with no window attached invites reading it as this index's average consolidation, which
    # is a different and much stronger claim - the same sample read 672 ms an hour earlier.
    flat = strip("\n".join(search_frame(metrics(), False, 120, 0)))
    assert flat.count("of the last few") == 6, "three time rows per index, each saying its window"


def test_the_queue_counters_are_on_screen_with_both_halves():
    # refresh-versus-compaction was the open question about this server's CPU spikes for a week.
    # Active alone cannot answer it: a queue of pending work with nothing running is the other half.
    flat = strip("\n".join(search_frame(metrics(compaction=1, pending=3), False, 120, 0)))
    assert "3 pending" in flat
    for kind in ("refresh", "compaction", "cleanup"):
        assert kind in flat


def test_no_metrics_is_not_a_panel_full_of_zeros():
    # `sessions 0` over `nothing running`, drawn off an empty result, is the shape being avoided:
    # every figure in this view is one row of one table, so an unanswered table has nothing to
    # degrade to and must say so instead of reporting an index count of 0.
    flat = strip("\n".join(search_frame(None, False, 120, 0)))
    assert "did not answer" in flat
    assert "0 indexes" not in flat


def test_a_server_with_no_index_says_so_rather_than_drawing_nothing():
    sea = {"server": metrics()["server"], "indexes": {}}
    flat = strip("\n".join(search_frame(sea, False, 120, 0)))
    assert "no per-index rows" in flat
    assert "refresh" in flat, "the server-wide counters are still measured"


@pytest.mark.parametrize(("w", "h"), SIZES)
def test_the_view_never_runs_past_the_terminal(w, h):
    # Not one row of it: every row here is full of escapes, so a byte slice would count colour as
    # columns, and a row one column over wraps and pushes the whole view down a line.
    lines = search_frame(metrics(), True, w, 0)
    assert max(len(strip(ln)) for ln in lines) <= max(70, w)


def test_scrolling_starts_from_the_row_asked_for():
    full = search_frame(metrics(), False, 120, 0)
    assert search_frame(metrics(), False, 120, 5) == full[5:]


def test_the_key_opens_it_and_the_bar_carries_the_key():
    # A binding that is not in the bar is a binding nobody has. And every view is a toggle: the same
    # key returns to the main frame, which is what DETAIL is read for.
    assert DETAIL["search"] == "i"
    assert ("i", "search") in KEYS
    assert len(set(DETAIL.values())) == len(DETAIL), "two views cannot share one key"


def test_the_legend_carries_the_vocabulary_this_view_introduces():
    # The tooltip has no prose of its own - it looks up LEGEND. A label on screen that is not in
    # here is a label the pointer cannot explain.
    terms = {t for name, items in LEGEND if name == "search" for t, _ in items}
    assert terms, "the search section is missing from LEGEND"
    flat = " ".join(terms)
    for label in ("live docs", "buffered", "segments", "index size", "avg consolidation",
                  "compaction", "connections"):
        assert label in flat, label


# ── counts are not bytes ────────────────────────────────────────────────────────────────────────

def test_a_document_count_is_base_1000():
    # human() is base 1024 and would print 11,216,808 documents as 10.7M - the right glyph over the
    # wrong arithmetic, on a quantity that is not bytes.
    assert qty(11216808) == "11.2M"
    assert qty(247665) == "247.7k"
    assert qty(12) == "12"


# ── the main frame ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(("w", "h"), SIZES)
def test_the_frame_still_fits_with_the_search_rows(w, h):
    lines = render(w, h)
    assert len(lines) <= h
    assert max(len(strip(ln)) for ln in lines) <= w


@pytest.mark.parametrize(("w", "h"), SIZES)
def test_the_frame_does_not_move_when_the_engine_does(w, h):
    # An index appearing, a compaction starting, or sdb_metrics failing for one tick must not
    # change the height of anything: a panel's height comes from the budget, not from the data.
    shapes = {(len(render(w, h, sea=sea)), boxes(render(w, h, sea=sea)))
              for sea in (metrics(), metrics(indexes=1), metrics(indexes=5),
                          metrics(compaction=1, pending=9), None)}
    assert len(shapes) == 1, f"the frame moved with the engine's state: {shapes}"


def test_the_search_rows_say_only_what_sdb_metrics_answered():
    flat = strip("\n".join(render(200, 60)))
    assert "indexes" in flat and "engine tasks" in flat
    assert "11.5M live docs" in flat, "the two indexes' live documents, base 1000"
    # Nothing running is a reading, and the row still has to say what it is a count of.
    assert "refresh, compaction, cleanup" in flat
    flat = strip("\n".join(render(200, 60, sea=metrics(compaction=1, pending=3))))
    assert "compaction 1+3" in flat, "which queue, once there is a which"


def test_the_search_rows_are_dropped_by_the_budget_and_never_half_drawn():
    # They are two pinned rows in a panel that shares its height with memory, so on a narrow screen
    # they cost four. The rule is all or nothing: one row of them would be a panel that changes
    # height between terminal sizes for no visible reason.
    for w, h in SIZES:
        flat = strip("\n".join(render(w, h)))
        assert ("indexes" in flat) == ("engine tasks" in flat), f"half-drawn at {w}x{h}"


def test_a_frame_without_a_connection_does_not_draw_the_search_rows_from_nothing():
    sz = {"duck": 2**35, "index": 2**34, "temp": 2**30, "total": 2**36, "temp_files": [],
          "temp_d": 0, "dt": 60}
    host = {"cores": 24, "load": ["1"], "threads": 100, "rss": 2**33, "swap": 0, "peak": 2**34,
            "uptime": 3600, "ram_total": 128 * 2**30, "pid": 1, "container": "c"}
    lines = frame(None, None, sz, {}, (None, [], {}), [], 0.0, host, False, 150, 46,
                  ("no credentials", "set a password"), None, None)
    flat = strip("\n".join(lines))
    assert "engine tasks" not in flat and "indexes" not in flat
    assert "no credentials" in flat


# ── the statement, and how much of it is one literal ────────────────────────────────────────────

STMT = "SELECT * FROM t WHERE v <#> ARRAY[" + ",".join(["-0.011719927341571243"] * 990) + "]"


def test_the_largest_literal_is_found_by_span():
    n, head = biggest_literal("select 'ab', '" + "x" * 100 + "', 3")
    assert n == 102, "the longest quoted literal, quotes included"
    assert head.startswith("'xxx")
    assert biggest_literal("select 1") == (0, "")


def test_a_bracketed_vector_counts_as_one_literal():
    n, _ = biggest_literal(STMT)
    assert n > len(STMT) * 0.9, "the array is nearly all of this statement"


def test_the_activity_view_measures_the_literal_against_the_whole_statement():
    # The finding this exists for: 68,209 characters, 21,684 of them (31.8%) a single 1024-dim
    # embedding sent as text. It took a vendor reading a screenshot to spot it.
    #
    # The collapsed row carries the size and the share, because those are what decide whether a
    # statement is worth opening. The arithmetic behind them is inside.
    rows = [("active", STMT, len(STMT))]
    row = strip("\n".join(activity_frame(state(), False, 160, 0, full=rows)))
    assert f"{len(STMT):,} chars" in row
    assert "one literal" in row
    opened = strip("\n".join(activity_frame(state(), False, 160, 0, full=rows, open_=True)))
    assert "in one literal" in opened and "over a quarter" in opened
    assert "that literal starts" in opened


def test_activity_is_collapsed_until_you_open_a_statement():
    # A 68 KB statement wrapped in full is a wall of float literals, and three of them buried the
    # session list they belonged to. The list answers "what is running" without answering "what is
    # in it" first.
    rows = [("active", STMT, len(STMT))] * 3
    collapsed = activity_frame(state(), False, 160, 0, full=rows, height=30)
    flat = strip("\n".join(collapsed))
    assert len(collapsed) < 12, f"{len(collapsed)} lines for three sessions is not a list"
    # One clipped line per session, not 3 x 21,814 characters wrapped. The row DOES show the head
    # of the statement - that is what makes the list readable - so the claim is about the whole.
    # 569 characters for three 21,814-character statements. The row shows a clipped head - that is
    # what makes the list readable - so the claim is about the whole frame, not about the preview.
    assert len(flat) < 12 * 170, f"{len(flat)} characters is still a flood"
    assert len(flat) < sum(len(q) for _, q, _ in rows) / 50
    opened = strip("\n".join(activity_frame(state(), False, 160, 0, full=rows, open_=True,
                                             height=30)))
    assert "-0.011719927341571243" in opened, "opening it has to show the statement itself"


def test_a_small_literal_is_reported_without_the_comparison_firing():
    q = "SELECT * FROM t WHERE id = 'abc'" + " -- " + "y" * 4000
    rows = [("active", q, len(q))]
    flat = strip("\n".join(activity_frame(state(), False, 160, 0, full=rows)))
    assert "chars" in flat
    assert "over a quarter" not in flat, "the comparison is named, so it must not fire below it"


def test_a_truncated_statement_gets_no_literal_share_at_all():
    # The tick fetches a 200-character head. A share measured against that is a share of the head,
    # which is a different question wearing the same words - so it is not measured at all.
    s = state()
    s["queries"] = [("active", STMT[:200], len(STMT))]
    flat = strip("\n".join(activity_frame(s, False, 160, 0)))
    assert f"{len(STMT):,} chars" in flat, "the full length is known and still worth saying"
    assert "one literal" not in flat


def test_the_main_frame_carries_the_statement_size_but_not_the_share():
    # One row per session there, and the head is all it was given - so the length goes in front of
    # the statement and the literal split stays behind `a`, which is the view that pays to fetch
    # whole statements.
    sz = {"duck": 2**35, "index": 2**34, "temp": 2**30, "total": 2**36, "temp_files": [],
          "temp_d": 0, "dt": 60}
    host = {"cores": 24, "load": ["1"], "threads": 100, "rss": 2**33, "swap": 0, "peak": 2**34,
            "uptime": 3600, "ram_total": 128 * 2**30, "pid": 1, "container": "c"}
    s = state()
    s["queries"] = [("active", STMT[:200], len(STMT))]
    flat = strip("\n".join(frame(s, None, sz, {}, (None, [], {}), [], 0.0, host, False, 200, 60,
                                 None, metrics(), (0, 0))))
    assert qty(len(STMT)) in flat, "the length the row cannot show by being one row"
    assert "in one literal" not in flat


def test_the_size_only_appears_once_a_statement_is_long_enough_to_hide_something():
    rows = [("active", "SELECT 1", 8)]
    flat = strip("\n".join(activity_frame(state(), False, 160, 0, full=rows)))
    assert "chars" not in flat, "a statement the row shows whole needs no length beside it"


# ── the two measurements of the temp directory ──────────────────────────────────────────────────

def temp_dir_state():
    sz = {"duck": 2**35, "index": 2**34, "temp": 72 * 2**30, "total": 2**38,
          "temp_files": [(0.0, 24 * 2**30, f"duckdb_temp_storage_S128K-{i}.tmp") for i in range(24)],
          "temp_d": 0, "dt": 60}
    host = {"uptime": 3600, "cores": 24}
    return sz, host


def test_the_storage_view_puts_the_server_s_own_count_next_to_du_s():
    # "24 files on disk, the server holds 0 of them" is the orphan claim proving itself from the
    # inside; the mtime comparison above it is sound but circumstantial on its own.
    sz, host = temp_dir_state()
    flat = strip("\n".join(storage_frame(state(), sz, host, False, 140, 0, held=(0, 0))))
    assert "24 files" in flat
    assert "the server holds 0 of them open" in flat


def test_nothing_is_claimed_about_held_files_when_the_server_was_not_asked():
    sz, host = temp_dir_state()
    flat = strip("\n".join(storage_frame(state(), sz, host, False, 140, 0, held=None)))
    assert "server holds" not in flat


def test_the_main_frame_orphan_row_carries_the_same_figure():
    flat = strip("\n".join(render(200, 60, held=(0, 0))))
    assert "server holds 0" in flat
    assert "old temp files" in flat


# ── which pool spilled ──────────────────────────────────────────────────────────────────────────

def test_the_memory_view_lists_spill_per_pool():
    s = state(spill={"ORDER_BY": 3 * 2**30, "HASH_TABLE": 2**30})
    flat = strip("\n".join(memory_frame(s, {}, {"rss": 2**33}, False, 140, 0)))
    assert "ORDER_BY" in flat and "3.0G" in flat
    assert "of the 4.0G spilled" in flat, "the share names the total it divides by"


def test_the_spill_section_is_drawn_even_when_nothing_spilled():
    # Otherwise "nothing spilled" and "this dashboard never looked" are the same picture, which is
    # the absence-of-evidence rule one level down.
    flat = strip("\n".join(memory_frame(state(), {}, {"rss": 2**33}, False, 140, 0)))
    assert "temporary_storage_bytes" in flat
    assert "no pool reports any" in flat


def test_a_sample_without_the_spill_column_does_not_take_the_view_down():
    s = state()
    del s["memspill"]
    assert memory_frame(s, {}, {"rss": 2**33}, False, 140, 0)

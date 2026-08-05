"""The log tailer, and the one property that decides whether it is usable.

A tailer that is following should show the newest line. A tailer you have scrolled up in should show
the SAME lines on the next tick - not the same offset from the end, which is a different thing:
holding an offset means the lines you were reading slide past while you read them, at whatever rate
the server happens to be logging. That is the bug that makes people give up and run `docker logs`
in another window, so it gets a test rather than a comment.
"""
from serenedash.fmt import strip
from serenedash.logs import counts, matching, parse
from serenedash.views import logs_frame


def rows(n, first=0):
    return [(f"08-03 10:00:{i:02d}", "Storage", "INFO", f"line {i}") for i in range(first, first + n)]


def body(out):
    """The rendered log lines only, without the header, the blank, or the more/less hints."""
    return [strip(ln).strip() for ln in out if " line " in strip(ln)]


def test_following_shows_the_newest_line():
    out = body(logs_frame(rows(200), "src", None, "", False, 100, 0, 20, True))
    assert out[-1].endswith("line 199")


def test_scrolling_up_holds_the_same_lines_when_new_ones_arrive():
    # The whole point. Scroll up by 5, then let 30 lines arrive, and the window must not move.
    before = body(logs_frame(rows(100), "src", None, "", False, 100, 5, 20, False))
    after = body(logs_frame(rows(130), "src", None, "", False, 100, 5 + 30, 20, False))
    assert before == after, "the window moved under the reader when new lines arrived"


def test_following_does_move_when_new_lines_arrive():
    # The other half: a follower that does not move is just as broken, in the opposite direction.
    before = body(logs_frame(rows(100), "src", None, "", False, 100, 0, 20, True))
    after = body(logs_frame(rows(130), "src", None, "", False, 100, 0, 20, True))
    assert before != after and after[-1].endswith("line 129")


def test_the_header_says_which_of_the_two_it_is_doing():
    on = strip(logs_frame(rows(20), "src", None, "", False, 100, 0, 20, True)[0])
    off = strip(logs_frame(rows(20), "src", None, "", False, 100, 3, 20, False)[0])
    assert "following" in on and "paused" in off


def test_a_paused_reader_is_told_how_far_behind_the_newest_line_they_are():
    out = [strip(ln) for ln in logs_frame(rows(200), "src", None, "", False, 100, 40, 20, False)]
    assert any("newer below" in ln and "40" in ln for ln in out)


def test_scrolling_past_the_start_stops_at_the_start():
    # A scroll offset larger than the buffer must clamp, not produce an empty window - a tailer that
    # goes blank when you hold the key down reads as a crash.
    out = body(logs_frame(rows(30), "src", None, "", False, 100, 9999, 20, False))
    assert out and out[0].endswith("line 0")


def test_the_source_is_always_named():
    # A quiet log because logging is off and a quiet log because nothing happened look identical,
    # and want opposite reactions.
    head = strip(logs_frame(rows(5), "docker logs oracle-serenedb", None, "", False, 120, 0, 20,
                            True)[0])
    assert "docker logs oracle-serenedb" in head


def test_an_empty_buffer_explains_itself_instead_of_rendering_nothing():
    out = [strip(ln) for ln in logs_frame([], "", "duckdb_logs() is empty - enable_logging", "",
                                          False, 100, 0, 20, True)]
    assert any("enable_logging" in ln for ln in out)


def test_the_frame_never_exceeds_the_height_it_was_given():
    for h in (8, 20, 44):
        assert len(logs_frame(rows(500), "src", None, "", False, 100, 0, h, True)) <= h


def test_a_filter_narrows_without_lying_about_the_source():
    got = matching(rows(20) + [("t", "Search", "ERROR", "index build failed")], "index")
    assert len(got) == 1 and got[0][2] == "ERROR"


def test_an_unparseable_line_is_kept_rather_than_dropped():
    # A crash dump or a library writing straight to stderr is exactly what you are tailing for, and
    # it will not be in the server's TSV format.
    assert parse("Segmentation fault (core dumped)")[3] == "Segmentation fault (core dumped)"
    assert parse("   \n") is None


def test_counts_summarise_the_buffer_that_is_actually_shown():
    lv, ty = counts(rows(3) + [("t", "Search", "WARN", "slow")])
    assert lv == {"INFO": 3, "WARN": 1} and ty == {"Storage": 3, "Search": 1}


def test_the_header_says_how_to_filter_rather_than_just_saying_search():
    # `i` is the search VIEW, and pressing it in the log switched away from the log - which is not
    # what a header advertising "Search" led anyone to expect. It says the keys now.
    #
    # Follow is on SPACE, not f: f is the findings view, and a letter that means one thing globally
    # and another inside one panel is a letter that means two things.
    head = strip(logs_frame(rows(5), "src", None, "", False, 120, 0, 20, True)[0])
    assert "/ filter" in head and "space follow" in head


def test_a_filter_being_typed_is_shown_as_it_is_typed():
    # Including the empty one: `/` with nothing after it has to look different from no filter at
    # all, or the first keystroke goes into what looks like a dead terminal.
    mid = [strip(ln) for ln in logs_frame(rows(5), "src", None, "err", False, 120, 0, 20, True,
                                          typing=True)]
    assert any("/err" in ln and "esc drops it" in ln for ln in mid)
    empty = [strip(ln) for ln in logs_frame(rows(5), "src", None, "", False, 120, 0, 20, True,
                                            typing=True)]
    assert any(ln.strip().startswith("/") for ln in empty)


def test_a_filter_that_matches_nothing_says_which_filter():
    out = [strip(ln) for ln in logs_frame([], "src", None, "zzz", False, 120, 0, 20, True)]
    assert any("nothing matched /zzz" in ln for ln in out)

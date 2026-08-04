"""What the pointer says.

Every case here is a wrong answer the first version gave against a real frame. The failure mode is
specific to this feature: a tooltip is never blank and never crashes, it just confidently explains
the wrong thing, and only reading it next to the row it points at shows that up.
"""
from serenedash.fmt import COL_BAR, COL_LABEL, COL_VALUE, C, NOCOLOR, bar, line
from serenedash.hover import describe, panel_at, place, segment_at, tip_box

c = NOCOLOR


def box(title, rows, w=90):
    """Same shape mkbox draws: a wall, a space, the row, padding, a wall."""
    assert all(len(r) < w for r in rows), "widen the fixture rather than letting a row run over"
    return ([f"┌─{title} " + "─" * (w - len(title) - 3) + "┐"]
            + [f"│ {r}" + " " * (w - len(r) - 1) + "│" for r in rows]
            + ["└" + "─" * w + "┘"])


STORAGE = box("storage", [
    line(c, "database", "80.8G", " " * COL_BAR, "wal 1.2G  0.02x wal/db"),
    line(c, "spill", "72.6G", bar(0.4, COL_BAR, ""), "37% of 194G on disk  flat 60s"),
    line(c, "orphaned", "72.6G", bar(0.4, COL_BAR, ""), "24 old temp files  37% reclaimable"),
], w=90)


def test_a_row_label_is_explained():
    title, body = describe(STORAGE, 1, 4, "main")
    assert title == "storage · database"
    assert "logical size" in body


def test_pointing_at_a_bar_still_identifies_its_row():
    # The bar is the reason to hover at all: it is the widest thing on the row and the least
    # self-describing. Landing on it must not answer "a bar" with no idea which one.
    col = 2 + COL_LABEL + COL_VALUE + 3
    title, body = describe(STORAGE, 2, col, "main")
    assert title.startswith("storage")
    assert "spill" in title or "spill" in body


def test_a_word_in_the_tail_is_explained_from_its_own_panel():
    row = STORAGE[3]
    col = row.index("old temp files")
    title, body = describe(STORAGE, 3, col, "main")
    assert title == "storage · orphaned"
    assert "TemporaryDirectoryHandle" in body


def test_the_heading_explains_the_panel_not_a_word_in_it():
    # `┌─threads` answered with `os threads` out of the HOST section, because the title happens to
    # be a word another panel's legend documents.
    thr = box("threads", [line(c, "cpu", "104%", " " * COL_BAR, "of 2400% across 24 cores")])
    title, body = describe(thr, 0, 4, "main")
    assert title == "threads"
    assert "ONE core" in body


def test_a_symbol_is_not_read_as_a_storage_term():
    # `flat_map` in a profile row matched `flat T` from the storage legend, one panel over.
    prof = box("profile", [line(c, "columnar", "6.8%", bar(0.9, COL_BAR, ""),
                                "duckdb::flat_map_probe(duckdb::Vector&)")], w=100)
    title, body = describe(prof, 1, prof[1].index("flat_map"), "main")
    # It may answer for the panel or for the row's engine; what it may not do is reach into another
    # section because the server's own text happened to contain one of its words.
    assert title.startswith("profile")
    assert "temp" not in body and "du " not in body


def test_multi_word_labels_beat_their_first_word():
    thr = box("threads", [line(c, "tid 554361", "12.0%", bar(0.12, COL_BAR, ""), "io wait")])
    title, body = describe(thr, 1, thr[1].index("io wait") + 1, "main")
    assert title == "threads · io wait"
    assert "uninterruptible" in body


def test_side_by_side_panels_answer_for_the_half_pointed_at():
    # Wide mode joins two boxes on one line. Column, not row, decides which panel a point is in;
    # scanning the line from the left answered every right-hand row with the left-hand panel.
    left = box("storage", [line(c, "database", "80.8G", " " * COL_BAR, "")], w=80)
    right = box("memory", [line(c, "in use", "34.0G", " " * COL_BAR, "33.9% of memory_limit")], w=80)
    pair = [a + " " + b for a, b in zip(left, right, strict=True)]
    assert panel_at(pair, 1, 4) == "storage"
    assert panel_at(pair, 1, 90) == "memory"
    title, _ = describe(pair, 1, pair[1].index("in use"), "main")
    assert title == "memory · in use"
    title, _ = describe(pair, 1, pair[1].index("database"), "main")
    assert title == "storage · database"


def test_a_detail_view_knows_its_own_panel_without_a_box():
    # Detail views draw no border, so there is no heading to scan up to; the view's name is it.
    rows = [line(c, "swapped", "29.7G", bar(0.8, COL_BAR, ""), "paged out")]
    title, body = describe(rows, 0, 2, "memory")
    assert title == "memory · swapped"
    assert "paged out" in body


def test_every_cell_of_a_frame_gets_an_answer():
    # A tooltip that is sometimes nothing is worse than none: you cannot tell "no explanation" from
    # "the pointer is not where you think".
    for r, ln in enumerate(STORAGE):
        for col in range(len(ln)):
            if ln[col] != " ":
                assert describe(STORAGE, r, col, "main"), (r, col, ln[col])


def test_a_point_outside_the_frame_is_not_an_answer():
    assert describe(STORAGE, 99, 0, "main") is None
    assert describe([], 0, 0, "main") is None


def test_segment_at_splits_on_the_box_wall():
    text = "│ left  │ │ right │"
    assert segment_at(text, 3) == (1, " left  ")
    assert segment_at(text, 13)[1].strip() == "right"


def test_the_box_never_hangs_off_the_screen():
    tip = ("storage · orphaned", "temp files older than the serened process holding them. " * 4)
    lines, w = tip_box(tip, NOCOLOR, 100)
    assert all(len(x) == w for x in lines)
    # Bottom-right corner: it has to flip up and left rather than draw past the edge.
    top, left = place(w, len(lines), 98, 45, 100, 46)
    assert top >= 0 and top + len(lines) <= 46
    assert left >= 0 and left + w <= 100


def test_the_box_is_coloured_only_when_colour_is_on():
    tip = ("memory · in use", "duckdb_memory() total against memory_limit")
    plain, _ = tip_box(tip, NOCOLOR, 80)
    assert not any("\033" in x for x in plain)
    colour, _ = tip_box(tip, C, 80)
    assert all("\033" in x for x in colour)


def test_mouse_reports_are_parsed_by_kind():
    # SGR (1006), not the original encoding: that one packs each coordinate into a byte offset by
    # 32, so it cannot name a column past 223 and reports the wrong cell on a wide terminal.
    from serenedash.tui import mouse_event
    assert mouse_event("<0;4;6", "M") == ("mouse", 3, 5, "press")
    assert mouse_event("<0;4;6", "m") == ("mouse", 3, 5, "release")
    assert mouse_event("<35;4;6", "M") == ("mouse", 3, 5, "move")     # bit 32: motion
    assert mouse_event("<64;4;6", "M") == ("mouse", 3, 5, "wheelup")  # bit 64: wheel
    assert mouse_event("<65;4;6", "M") == ("mouse", 3, 5, "wheeldn")
    # Past column 223, which is the whole reason for this encoding.
    assert mouse_event("<35;418;7", "M") == ("mouse", 417, 6, "move")


def test_a_malformed_report_is_not_a_keystroke():
    from serenedash.tui import mouse_event
    assert mouse_event("<35;4", "M") == ""
    assert mouse_event("<a;b;c", "M") == ""

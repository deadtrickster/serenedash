"""Where things are on screen, and that they stay there.

Every rule in here was learned by something moving. A dashboard is read by glancing at it, and a
glance is aimed - you look at the row where the number was last time. So the invariants are about
position and size rather than content:

  - a frame is exactly as tall as the window, at every size
  - a panel's height comes from a budget, so a busy server draws the same shape as an idle one
  - every view starts its content on the same row, and ends with the key bar on the same row
  - nothing prints a key hint into its content, where it would move with the content

The failures these caught, in order: an 80x24 terminal handed 27 lines; the findings screen a line
lower than every other view because it kept the blank its old header sat on; "esc goes back" sliding
up and down with the number of sessions; the served frame resizing on every view switch, which made
the whole page reflow; and the key bar itself moving up a row on any view whose bar did not wrap.
"""
import pytest

from serenedash.fmt import NOCOLOR, strip
from serenedash.tui import WEB_ROWS, _withbar
from serenedash.views import (
    DETAIL,
    activity_frame,
    findings_frame,
    frame,
    host_frame,
    legend_frame,
    logs_frame,
    mcp_frame,
    status,
    storage_frame,
    summary_line,
    view_hint,
)

from .test_views import sample_state

SIZES = [(80, 24), (100, 30), (120, 40), (168, 44), (200, 60), (300, 80)]

HOST = {"cores": 24, "load": ["1", "2", "3"], "threads": 100, "rss": 2**33, "swap": 0,
        "peak": 2**34, "uptime": 3600, "ram_total": 128 * 2**30, "pid": 1, "container": "c"}
SZ = {"duck": 2**35, "index": 2**34, "temp": 2**30, "total": 2**36, "temp_files": [],
      "temp_d": 0, "dt": 60}
CALLS = [{"t": 1785000000 + i, "tool": "status", "ms": 1, "ok": True, "pid": 7, "client": "c",
          "args": "", "bytes": 9, "reply": "{}"} for i in range(6)]
FOUND = [{"kind": "memory", "what": f"finding {i}", "detail": "a detail sentence."}
         for i in range(6)]


def main_frame(w, h, **kw):
    return frame(sample_state(**kw), None, SZ, {"mem": [34 * 2**30] * 40}, (None, [], {}),
                 [(50.0, f"tid {i}", "R", str(i)) for i in range(30)], 60.0, HOST, True, w, h)


def detail_frames(w, height=20):
    """One of every detail view, at the same size, built from the same fixtures."""
    return {
        "storage": storage_frame(sample_state(), SZ, HOST, False, w, 0),
        "host": host_frame(HOST, sample_state(), False, w, 0),
        "legend": legend_frame(False, w, 0),
        "logs": logs_frame([("t", "S", "INFO", "a line")] * 4, "src", None, "", False, w, 0,
                           height),
        "mcp": mcp_frame(CALLS, [], False, w, 0, 0, height),
        "findings": findings_frame(FOUND, False, w, 0, 0, height),
        "activity": activity_frame(sample_state(), False, w, 0, height=height),
    }


# ---- the terminal ------------------------------------------------------------------------------

@pytest.mark.parametrize(("w", "h"), SIZES)
def test_the_frame_is_exactly_as_tall_as_the_window(w, h):
    # An 80x24 terminal was once handed 27 lines: the plan ladder built a frame and nothing
    # enforced the height after it.
    assert len(main_frame(w, h)) == h


@pytest.mark.parametrize(("w", "h"), SIZES)
def test_no_line_is_wider_than_the_window(w, h):
    # Measured on the VISIBLE text. Every row is full of escapes, so len() on the raw string is a
    # count of bytes and says nothing about columns.
    over = [(i, len(strip(ln))) for i, ln in enumerate(main_frame(w, h)) if len(strip(ln)) > w]
    assert not over, f"rows past {w} columns: {over[:3]}"


@pytest.mark.parametrize(("w", "h"), SIZES)
def test_a_busy_server_draws_the_same_shape_as_an_idle_one(w, h):
    # Heights come from a budget, not from how many rows there happen to be. A frame that grows
    # when a query starts is one you have to re-read from the top on every refresh.
    idle, busy = main_frame(w, h, sessions=1, tags=1), main_frame(w, h, sessions=40, tags=16)
    assert len(idle) == len(busy) == h
    bar = [i for i, ln in enumerate(idle) if "quit" in strip(ln)]
    assert bar and bar == [i for i, ln in enumerate(busy) if "quit" in strip(ln)]


@pytest.mark.parametrize("w", [80, 120, 168, 300])
def test_every_detail_view_starts_its_content_on_the_first_row(w):
    # One view sitting a line lower than the rest reads as the frame having shifted rather than as
    # a different panel. The findings screen did, because it kept the blank row its old header sat
    # on after the header was removed as a duplicate of the pinned summary.
    blank = [n for n, f in detail_frames(w).items() if not f or not strip(f[0]).strip()]
    assert not blank, f"these start one row lower than the rest: {blank}"


@pytest.mark.parametrize("height", [10, 20, 44])
def test_a_view_that_owns_its_height_stays_inside_it(height):
    # Two kinds of view. The older ones (storage, host, legend) build their whole content and the
    # caller slices - they scroll. The ones with a cursor take a height and lay out inside it,
    # because a cursor has to stay on screen and a slice cannot know where it is. Only the second
    # kind can overflow, so only the second kind is asserted here; the first is covered by the
    # window-height test above and by `_withbar` for the page.
    owns = {n: f for n, f in detail_frames(120, height).items()
            if n in ("logs", "mcp", "findings", "activity")}
    over = {n: len(f) for n, f in owns.items() if len(f) > height}
    assert not over, f"taller than the {height} rows they were given: {over}"


@pytest.mark.parametrize("height", [10, 20, 44])
def test_a_scrolling_view_is_sliced_by_the_caller_rather_than_cut_by_itself(height):
    # The contract for the other kind: it returns everything and the frame that hosts it takes the
    # window's worth. Asserted so a future height parameter is not quietly added to one of them and
    # then ignored by the caller that is still slicing.
    for name in ("storage", "host", "legend"):
        f = detail_frames(120, height)[name]
        assert f, name
        assert len(f[:height]) <= height


def test_no_frame_prints_a_key_hint_into_its_own_content():
    # Hints live on the pinned bar. At the foot of a panel they moved whenever the content changed
    # height - down a line when a session appeared, up two when a filter narrowed the log - and a
    # control that moves is a control you have to look for.
    frames = [*detail_frames(120).values(),
              mcp_frame(CALLS, [], False, 120, 0, 0, 20, open_pid=7),
              mcp_frame(CALLS, [], False, 120, 0, 0, 20, open_pid=7, popup=True),
              findings_frame(FOUND, False, 120, 0, 0, 20, True),
              activity_frame(sample_state(), False, 120, 0, open_=True, height=20)]
    for f in frames:
        flat = strip("\n".join(f))
        for phrase in ("esc goes back", "esc closes", "j/k moves", "enter opens the"):
            assert phrase not in flat, f"still printing {phrase!r} into the content"


def test_the_hint_says_what_the_view_answers_to_at_that_depth():
    # `enter` opens a session on the mcp list and does nothing inside the call box, so a hint that
    # named it in both would be wrong in one of them.
    assert "opens the session" in strip(view_hint("mcp", {}, NOCOLOR))
    assert "shows the call" in strip(view_hint("mcp", {"open": 7}, NOCOLOR))
    assert "closes" in strip(view_hint("mcp", {"open": 7, "popup": True}, NOCOLOR))
    assert "enter" not in strip(view_hint("mcp", {"open": 7, "popup": True}, NOCOLOR))


# ---- the summary rule --------------------------------------------------------------------------

@pytest.mark.parametrize("w", [60, 80, 100, 168, 300])
def test_the_summary_rule_is_one_line_that_spans_the_frame(w):
    # A rule that only pads runs past the frame on a narrow terminal and wraps, which leaves a
    # stray half-rule under the top of every redraw.
    for found in ([], FOUND, FOUND * 4):
        line = strip(summary_line(found, NOCOLOR, w))
        assert "\n" not in line
        assert len(line) <= w, f"{len(line)} columns at width {w}"
        assert len(line) >= w - 2, f"{len(line)} does not reach the edge at width {w}"


@pytest.mark.parametrize("w", [80, 168, 300])
def test_the_summary_rule_is_centred(w):
    line = strip(summary_line(FOUND, NOCOLOR, w))
    left = len(line) - len(line.lstrip("─"))
    right = len(line) - len(line.rstrip("─"))
    assert abs(left - right) <= 1, f"{left} vs {right} at width {w}"


# ---- the page ------------------------------------------------------------------------------------

@pytest.mark.parametrize("w", [80, 100, 168, 260])
def test_a_served_frame_is_the_same_size_and_shape_on_every_view(w):
    # On the page the frame is an SVG sized to its own content, so a short panel pulled the key bar
    # up and a long one pushed it down. Two things have to hold at once: the frame height, or the
    # page reflows on every switch, and the row `q quit` lands on, or the one element that must not
    # move, moves.
    shapes = {}
    for view in ("main", *DETAIL):
        lines = main_frame(w, 44) if view == "main" else ["one short panel"]
        out, _off = _withbar(lines, w, [], view, {})
        shapes[view] = (len(out), next(i for i, ln in enumerate(out) if "quit" in strip(ln)))
    assert len(set(shapes.values())) == 1, f"the frame moved between views at {w}: {shapes}"


def test_a_served_frame_does_not_grow_with_its_content():
    # The same panel with four rows and with four hundred.
    short, _ = _withbar(["a row"], 168, [], "mcp", {})
    long, _ = _withbar(["a row"] * 400, 168, [], "mcp", {})
    assert len(short) == len(long)


def test_a_served_frame_carries_exactly_one_key_bar():
    # The guard that decides whether a panel needs one looked for the literal "q quit", which never
    # appears in a COLOURED bar - the escapes sit between the words - so it matched only under
    # --no-color and the browser got a second bar under the first.
    main, off = _withbar(main_frame(120, 44), 120, [], "main", {})
    panel, off2 = _withbar(["a panel", "with no bar"], 120, [], "storage", {})
    assert sum(1 for ln in main if "quit" in strip(ln)) == 1
    assert sum(1 for ln in panel if "quit" in strip(ln)) == 1
    # The rule and the blank under it: the rows inserted above the panel, which every click anchor
    # moves down by.
    assert off == off2 == 2


def test_the_served_body_is_the_rows_it_says_it_is():
    out, off = _withbar(["x"] * 5, 168, [], "storage", {})
    bar = next(i for i, ln in enumerate(out) if "quit" in strip(ln))
    assert bar - off <= WEB_ROWS + 2, "the body must not exceed the rows reserved for it"
    assert out[-1] == "" or strip(out[-1]).strip(), "no ragged tail past the bar"


def test_the_bar_reserves_room_for_the_longest_hint_any_view_can_produce():
    # Reserved from the widest hint at this width, not from this view's - otherwise the reservation
    # itself changes with the view, which is the thing it exists to stop.
    widest = max(len(status(NOCOLOR, 168, view_hint(v, {}, NOCOLOR))) for v in ("main", *DETAIL))
    out, _ = _withbar(["x"], 168, [], "main", {})
    bar_at = next(i for i, ln in enumerate(out) if "quit" in strip(ln))
    assert len(out) - bar_at >= widest

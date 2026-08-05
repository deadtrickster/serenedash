"""What has to be fast, and why.

Switching a view is pure formatting. The data is already in memory - the terminal proves it, since
pressing `t` redraws without re-querying anything. The web page did not: `/view` set a variable and
left the browser to wait for the next data tick, so a switch cost up to a whole refresh interval and
looked like the dashboard hanging.

These are budgets, not benchmarks. They are deliberately loose enough not to fail on a loaded
machine and tight enough to catch a redraw that has started doing I/O, which is the regression that
actually happens here - `full_queries` inside a render lambda once turned every keypress into a
185 KB fetch.
"""
import time

import pytest

from serenedash.export import runs, svg
from serenedash.fmt import strip
from serenedash.views import frame

from .test_views import render  # noqa: TID252  - the same fixture, one source of truth


def timed(fn, n=20):
    """Median of n runs, in milliseconds. Median because one scheduling hiccup should not decide."""
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t0) * 1000)
    ts.sort()
    return ts[len(ts) // 2]


def test_a_redraw_is_pure_formatting():
    # 25 ms at 200x60 with everything on. If this fails, something in the frame path started
    # touching the network or the filesystem, which is the bug that keeps recurring.
    ms = timed(lambda: render(200, 60))
    assert ms < 25, f"a full frame took {ms:.1f} ms - a redraw should not be doing I/O"


def test_a_redraw_does_not_get_slower_with_more_content():
    # Panel heights come from a budget, so a busy server must cost the same as an idle one. A
    # renderer that slows down under load is one that stops updating exactly when you need it.
    idle = timed(lambda: render(200, 60, sessions=1, tags=1))
    busy = timed(lambda: render(200, 60, sessions=40, tags=16))
    assert busy < idle * 3 + 5, f"idle {idle:.1f} ms vs busy {busy:.1f} ms - height is budgeted, cost should be too"


def test_switching_views_is_cheaper_than_one_refresh_interval():
    # The point of the whole exercise: a switch must be imperceptible next to the 5s default tick.
    # 50 ms is two orders of magnitude of headroom and still catches a switch that re-queries.
    ms = timed(lambda: render(168, 44))
    assert ms < 50, f"{ms:.1f} ms to render a view - the browser should never wait a tick for this"


def test_the_svg_export_is_not_slower_than_the_frame_it_renders():
    # It runs on every published frame with --serve, so if it costs more than the render it becomes
    # the reason the dashboard cannot keep its interval.
    lines = render(168, 44)
    fr = timed(lambda: render(168, 44))
    ex = timed(lambda: svg(lines))
    assert ex < fr * 4 + 10, f"frame {fr:.1f} ms, svg {ex:.1f} ms - the exporter is the bottleneck"


def test_the_ansi_parse_is_linear_in_line_length():
    # runs() is called per line per published frame. Quadratic behaviour here would only show up on
    # a wide terminal with a lot of colour, which is exactly the case worth having.
    short = "\033[36m" + "x" * 100 + "\033[0m"
    long = "\033[36m" + "x" * 1000 + "\033[0m"
    t_short = timed(lambda: runs(short), n=200)
    t_long = timed(lambda: runs(long), n=200)
    assert t_long < t_short * 40 + 1, f"10x the input cost {t_long / max(t_short, 1e-6):.0f}x the time"


@pytest.mark.parametrize("w", [80, 168, 400])
def test_the_export_reproduces_every_line_exactly(w):
    # Not timing, but it belongs beside it: the fast path must still be the correct one. Rebuild
    # each line from the runs the SVG is built from and compare against the stripped original.
    for line in render(w, 44):
        rebuilt = [" "] * (len(strip(line)) + 4)
        for col, text, *_ in runs(line):
            for i, ch in enumerate(text):
                rebuilt[col + i] = ch
        assert "".join(rebuilt).rstrip() == strip(line).rstrip()


def test_an_empty_frame_still_produces_a_document():
    # A server that answers nothing must not produce an SVG with no viewBox, which renders as an
    # invisible element rather than as an empty dashboard.
    out = svg([])
    assert out.startswith("<svg") and "viewBox" in out and out.endswith("</svg>")


def test_frame_cost_is_reported_for_the_record(capsys):
    # Not an assertion - a number in the test log, so a future slowdown has something to be
    # compared against rather than a bare "feels slower".
    with capsys.disabled():
        for w, h in ((80, 30), (168, 44), (300, 80)):
            lines = frame(*_args(w, h))
            print(f"    {w}x{h}: frame {timed(lambda: frame(*_args(w, h))):5.1f} ms   "
                  f"svg {timed(lambda: svg(lines)):5.1f} ms   {len(lines)} lines")


def _args(w, h):
    from .test_views import sample_state
    sz = {"duck": 2**35, "index": 2**34, "temp": 2**30, "total": 2**36, "temp_files": [],
          "temp_d": 0, "dt": 60}
    host = {"cores": 24, "load": ["1", "2", "3"], "threads": 100, "rss": 2**33, "swap": 0,
            "peak": 2**34, "uptime": 3600, "ram_total": 128 * 2**30, "pid": 1, "container": "c"}
    thr = [(50.0, f"tid {i}", "R", str(i)) for i in range(30)]
    return (sample_state(), None, sz, {"mem": [34 * 2**30] * 40}, (None, [], {}), thr, 60.0,
            host, True, w, h)

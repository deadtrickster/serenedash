"""Layout invariants.

These are the properties a dashboard has to hold to be readable at all, and each was broken at
least once: a frame that outgrows its terminal loses the top, a frame whose height depends on
content makes you re-read it every refresh, and a row wider than the box corrupts the border.
"""
import pytest

from serenedash.fmt import strip
from serenedash.views import KEYS, LEGEND, frame, status

SIZES = [(200, 60), (150, 46), (120, 45), (100, 40), (96, 40), (80, 30)]


def sample_state(sessions=3, tags=3, memlimit=100 * 2**30):
    return {
        "db": "d", "size": 80 * 2**30, "wal": 2**20, "mem": 34 * 2**30, "memlimit": memlimit,
        "blocks": (1000, 900, 100, 262144),
        "memtags": [(f"POOL_{i}", (10 - i) * 2**30) for i in range(tags)],
        "states": {"active": sessions, "idle": 2},
        "queries": [("active", f"SELECT {i}", 9) for i in range(sessions)],
        "settings": {"memory_limit": "100.0 GiB", "threads": "24"},
        "t": 0.0,
    }


def render(w, h, **kw):
    s = sample_state(**kw)
    sz = {"duck": 2**35, "index": 2**34, "temp": 2**30, "total": 2**36, "temp_files": [],
          "temp_d": 0, "dt": 60}
    host = {"cores": 24, "load": ["1", "2", "3"], "threads": 100, "rss": 2**33, "swap": 0,
            "peak": 2**34, "uptime": 3600, "ram_total": 128 * 2**30, "pid": 1, "container": "c"}
    hist = {"mem": [34 * 2**30] * 10}
    thr = [(50.0, "tid 1", "R", "1"), (10.0, "tid 2", "S", "2")]
    return frame(s, None, sz, hist, (None, [], {}), thr, 60.0, host, False, w, h)


@pytest.mark.parametrize(("w", "h"), SIZES)
def test_frame_fits_the_terminal(w, h):
    lines = render(w, h)
    assert len(lines) <= h, f"{len(lines)} lines in a {h}-line terminal - the top scrolls away"
    widest = max(len(strip(ln)) for ln in lines)
    assert widest <= w, f"{widest} columns in a {w}-column terminal - the border corrupts"


@pytest.mark.parametrize(("w", "h"), SIZES)
def test_height_does_not_depend_on_content(w, h):
    # A panel that resizes when a query ends or a pool drains makes everything below it jump, and
    # you have to re-read the frame from the top every refresh.
    heights = {len(render(w, h, sessions=n, tags=t)) for n, t in ((1, 1), (3, 3), (12, 8))}
    assert len(heights) == 1, f"height varies with content: {heights}"


@pytest.mark.parametrize(("w", "h"), SIZES)
def test_box_borders_line_up(w, h):
    edges = {len(strip(ln)) for ln in render(w, h) if strip(ln).endswith(("┐", "┘"))}
    assert len(edges) == 1, f"ragged right border at {w}x{h}: {sorted(edges)}"


@pytest.mark.parametrize("w", [92, 100, 120, 200])
def test_status_bar_keeps_every_key_and_fits(w):
    lines = status(dict.fromkeys(("r", "dim", "b", "grn", "yel", "red", "cyn", "mag", "blu"), ""), w)
    assert max(len(ln) for ln in lines) <= w
    joined = " ".join(lines)
    for key, label in KEYS:
        assert f"{key} {label}" in joined, f"{key} dropped from the bar at width {w}"


def test_legend_covers_every_panel():
    # The legend is the answer to "what is this number", so a panel missing from it is a panel
    # nobody can check.
    sections = {name for name, _ in LEGEND}
    assert {"storage", "memory", "activity", "threads", "profile", "host", "config"} <= sections

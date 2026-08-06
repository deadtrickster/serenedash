"""The key bar is the navigation, so the hit areas over it have to land on it.

There used to be a row of buttons above the frame saying the same thing the bar at the foot already
said, and the selected one rendered bold, which reflowed the row under the pointer. Removing it
means the bar has to be clickable, and the coordinates for that are computed from the rendered text
rather than placed by hand - the bar re-justifies and wraps with the width, so anything hand-placed
would be right at one size and wrong at the next. This checks the arithmetic at several widths.
"""
import json

import pytest

from serenedash.export import CW, LH, PAL
from serenedash.fmt import NOCOLOR, strip
from serenedash.serve import PAGE, frame_payload, hits
from serenedash.tui import NEEDS_SQL
from serenedash.views import BINDINGS, DETAIL, KEYS, NOT_ON_THE_PAGE, key_to_view

from .test_views import render  # noqa: TID252  - the same fixture, one source of truth

# The map, from its one producer. This file used to build its own from DETAIL, which is exactly the
# copy that let `d` work in one front end and not the other.
KEYMAP = key_to_view()


@pytest.mark.parametrize("w", [80, 120, 168, 260])
def test_every_key_gets_a_hit_area_at_every_width(w):
    got = hits(render(w, 44), KEYMAP)
    assert {h["view"] for h in got} == set(KEYMAP.values()), f"missing bindings at {w} columns"


@pytest.mark.parametrize("w", [80, 168])
def test_a_hit_area_sits_on_the_text_it_claims(w):
    # The real check: convert each box back to a row and column and read what is printed there.
    lines = render(w, 44)
    for h in hits(lines, KEYMAP):
        row = round((h["y"] - 8) / LH)
        col = round((h["x"] - 8) / CW + 0.5)
        text = strip(lines[row])[col:col + len(h["key"]) + 1 + len(h["view"])]
        assert text == f"{h['key']} {h['view']}", f"box for {h['view']} covers {text!r}"


def test_hit_areas_do_not_overlap():
    # Overlapping boxes make one binding unclickable, which is worse than not being clickable at
    # all: the affordance is there and does the wrong thing.
    boxes = sorted((h["y"], h["x"], h["x"] + h["w"], h["view"])
                   for h in hits(render(168, 44), KEYMAP))
    for (y1, _, end, a), (y2, start, _, b) in zip(boxes, boxes[1:], strict=False):
        if y1 == y2:
            assert start >= end - 0.01, f"{a} and {b} overlap"


def test_only_the_key_bar_is_clickable():
    # "s storage" could plausibly appear in a panel. Scanning the whole frame would put a hit area
    # over a line of data, and clicking a number would navigate.
    lines = [*["s storage  m memory"] * 30, *render(168, 44)[-4:]]
    assert all(h["y"] > 8 + 25 * LH for h in hits(lines, KEYMAP))


def test_the_payload_carries_the_boxes_with_the_frame():
    # They have to arrive together. Hit areas from one frame over the SVG of another would drift
    # exactly when the bar rewrapped, which is the case they exist to survive.
    p = json.loads(frame_payload("main", render(168, 44), cols=168, keys=KEYMAP))
    assert p["hits"] and p["svg"].startswith("<svg") and p["view"] == "main"


def test_a_frame_with_no_key_bar_produces_no_boxes():
    assert hits(["nothing here", "or here"], KEYMAP) == []


def test_the_page_has_no_second_row_of_buttons():
    # The regression this replaces: two sets of controls saying the same thing, the top one
    # reflowing under the pointer as the selection changed its font weight.
    assert "<nav" not in PAGE and "createElement('a')" not in PAGE
    assert ".hit:hover" in PAGE, "the bar has to say it is clickable"


def test_the_export_palette_is_brighter_than_the_terminals():
    # A terminal is a dark room; a browser is next to page chrome at whatever the display's
    # brightness is, and the One Dark foreground read as muddy grey there.
    r, g, b = (int(PAL["37"][i:i + 2], 16) for i in (1, 3, 5))
    assert min(r, g, b) > 0xC0, f"foreground {PAL['37']} is too dim for a page"


def test_every_view_the_page_offers_has_a_branch_in_the_dispatch():
    # The bug this catches: `logs` was in DETAIL, so it appeared in the served view list and got a
    # hit area on the bar, but `view_lines` had no branch for it and fell through to `return lines`
    # - clicking it served the main frame back and looked like the view failing to load.
    from serenedash.tui import view_lines

    from .test_timing import _args  # noqa: TID252  - the same fixture data as the timing budgets

    st, _prev, sz, hist, perf, thr, tcpu, hinfo, _c, _w, _h = _args(100, 44)
    sea = {"server": {}, "indexes": {}}
    marker = ["THE MAIN FRAME"]       # returned verbatim by the fall-through, so identity finds it
    served = {}
    for name in sorted(DETAIL):
        try:
            served[name] = view_lines(name, {}, None, marker, st, sz, hist, perf, thr, tcpu, hinfo,
                                      sea, True, 100)
        except Exception:                                        # noqa: BLE001, PERF203
            served[name] = ["raised"]                            # a branch exists; it just needs data
    fell_through = [n for n, out in served.items() if out is marker]
    assert not fell_through, f"no branch in view_lines for: {fell_through}"


def test_the_served_javascript_carries_no_control_characters():
    # PAGE is an ordinary Python string, so an escape written into the JS - `\r` for Enter, `\x1b`
    # for Escape - becomes a REAL control byte in the served text, inside the string literal it
    # sits in. The page then throws "Invalid or unexpected token" before drawing anything, and
    # every browser test errors at the fixture with no hint of why. Keys travel by name instead.
    bad = [(i, repr(ln)) for i, ln in enumerate(PAGE.splitlines(), 1)
           if any(c in ln for c in ("\r", "\x1b", "\x00", "\b", "\f", "\v"))]
    assert not bad, f"control characters in the served page: {bad}"


def test_the_served_javascript_parses():
    # A syntax error in the page is invisible from Python and fatal in a browser. node is in mise
    # here and on the runner; where it is not, this skips rather than pretending to have checked.
    import json
    import re
    import shutil
    import subprocess
    import tempfile

    node = shutil.which("node")
    if not node:
        pytest.skip("no node to parse the page with")
    page = (PAGE.replace("__VIEWS__", json.dumps(["main", "mcp"]))
                .replace("__KEYS__", json.dumps({"n": "mcp"})))
    js = re.search(r"<script>(.*)</script>", page, re.S).group(1)
    with tempfile.NamedTemporaryFile("w", suffix=".js") as f:
        f.write(js)
        f.flush()
        out = subprocess.run([node, "--check", f.name], capture_output=True, text=True)
    assert out.returncode == 0, f"the served page does not parse:\n{out.stderr}"

# ---- every binding, from the map rather than from a list typed out beside it -------------------

@pytest.mark.parametrize(("key", "view", "label"), BINDINGS, ids=lambda x: str(x))
def test_every_binding_is_on_the_bar_with_its_label(key, view, label):
    assert (key, label) in KEYS
    if view:
        assert key_to_view()[key] == view
        assert DETAIL[view] == key


@pytest.mark.parametrize(("key", "view", "label"), BINDINGS, ids=lambda x: str(x))
def test_every_binding_gets_a_clickable_box_at_every_width(key, view, label):
    # A key printed on the bar with no box under it is a key the page documents and does not offer.
    # `g` and `c` were both on the served bar and in neither the map nor the view list.
    from serenedash.views import WEB_KEYS, status

    if key in NOT_ON_THE_PAGE:
        assert (key, label) not in WEB_KEYS, "a browser cannot do this, so it must not say it can"
        return
    for w in (80, 120, 168, 260):
        bar = status(NOCOLOR, w, "", WEB_KEYS)
        boxes = {h["key"] for h in hits(bar, key_to_view())}
        assert key in boxes, f"{key} {label} has no clickable box at {w} columns"


@pytest.mark.parametrize(("key", "view", "label"), BINDINGS, ids=lambda x: str(x))
def test_every_binding_the_page_offers_reaches_a_view_it_has(key, view, label):
    from serenedash.tui import view_lines

    served = ["main", *sorted(DETAIL)]
    if key in NOT_ON_THE_PAGE:
        return
    assert view in served, f"{key} opens {view}, which the page is not offered"
    # And the dispatch has a branch for it: a name in the list with no branch falls through to the
    # main frame, which reads as the view failing to load.
    marker = ["THE MAIN FRAME"]
    try:
        out = view_lines(view, {}, None, marker, None, None, None, None, None, None, None, None,
                         True, 100)
    except Exception:                                            # noqa: BLE001
        return                                                   # a branch exists; it wants data
    assert out is not marker or view in NEEDS_SQL, f"no branch in view_lines for {view}"


def test_the_alias_is_not_on_the_bar_but_still_resolves():
    # Two rows for one screen is not documentation, it is noise.
    assert "d" not in [k for k, _ in KEYS]
    assert key_to_view()["d"] == "findings"

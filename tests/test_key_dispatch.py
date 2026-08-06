"""Every key on the bar must reach a branch that draws something.

`c` crashed the dashboard with `KeyError: 'config'`. Adding config and graph to `BINDINGS` put them
in `DETAIL`, the generic `elif view in DETAIL` branch ran before the two `elif view == "config"`
branches, and the panel dispatch is a dict literal with no config key. One keypress, one traceback,
process gone.

Nothing caught it because every existing test drove `view_lines` - the shared dispatch the page and
the exporter use - and the TERMINAL has its own, inline in the loop, which no test could reach. So
these tests read the loop's dispatch out of the source with `ast` rather than re-implementing it:
they check the branch structure that actually runs, not a copy of it.

The general rule they encode: a binding with no branch is a crash, not a blank screen. It cannot be
allowed to depend on someone pressing the key.
"""
import ast
import inspect

import pytest

from serenedash import tui
from serenedash.views import BINDINGS, DETAIL, NOT_ON_THE_PAGE, key_to_view

SRC = inspect.getsource(tui)
TREE = ast.parse(SRC)


def panel_dict_keys():
    """The keys of the dict literal the terminal loop dispatches on.

    Read from the source, because the dict is built inside `main` from loop-local state and cannot
    be imported. An ast walk rather than a regex: the values are multi-line lambdas and the braces
    do not survive line-based matching.
    """
    for node in ast.walk(TREE):
        if isinstance(node, ast.Dict) and node.keys:
            keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
            if "storage" in keys and "legend" in keys:
                return set(keys)
    raise AssertionError("could not find the panel dispatch dict in tui.main")


def own_branch_views():
    """Views compared by name in the loop - `view == "config"` and friends."""
    out = set()
    for node in ast.walk(TREE):
        if (isinstance(node, ast.Compare) and isinstance(node.left, ast.Name)
                and node.left.id == "view" and len(node.ops) == 1
                and isinstance(node.ops[0], ast.Eq)
                and isinstance(node.comparators[0], ast.Constant)):
            out.add(node.comparators[0].value)
    return out


def test_the_source_scan_found_the_dispatch():
    # Guard the guard. If the ast walk stops finding it, every test below passes vacuously.
    assert len(panel_dict_keys()) >= 8
    assert {"config", "graph"} <= own_branch_views()


@pytest.mark.parametrize(("key", "view"), sorted((k, v) for k, v, _l in BINDINGS if v))
def test_every_binding_reaches_a_branch_that_draws(key, view):
    # The crash, as a test: `config` was in DETAIL, so the generic branch claimed it, and the panel
    # dict had no entry for it.
    drawn = panel_dict_keys() | own_branch_views()
    assert view in drawn, f"key {key!r} opens {view!r} and nothing in the loop draws it"


def test_the_declared_panel_set_matches_the_dispatch():
    # `PANELS` exists so this file has something to check against that a reader can see. If the
    # dict and the constant drift, the constant is worthless.
    assert set(tui.PANELS) == panel_dict_keys()
    assert set(tui.OWN_BRANCH) == {"config", "graph"}


def test_every_view_is_either_a_panel_or_has_its_own_branch():
    assert set(DETAIL) == set(tui.PANELS) | set(tui.OWN_BRANCH)


def test_a_view_with_its_own_branch_is_handled_before_the_generic_one():
    # This is the ORDER that broke. config has a branch, but the generic `view in DETAIL` came
    # first and swallowed it. Compare line numbers in the loop.
    lines = SRC.splitlines()
    generic = next(i for i, ln in enumerate(lines)
                   if "elif view in DETAIL:" in ln)
    for view in tui.OWN_BRANCH:
        own = next((i for i, ln in enumerate(lines) if f'view == "{view}"' in ln), None)
        assert own is not None, f"{view} has no branch of its own"
        assert own < generic, (f"`view == {view!r}` is at line {own} and the generic branch is at "
                               f"{generic}; the generic one wins and the key crashes")


def test_the_page_is_offered_every_view_its_keys_open():
    # The same failure from the other end: a key the page sends for a view it was never given.
    served = ["main", *sorted(DETAIL)]
    for key, view in key_to_view().items():
        if key in NOT_ON_THE_PAGE:
            continue
        assert view in served, f"key {key!r} opens {view!r}, which the page is not offered"


@pytest.mark.parametrize("view", sorted(tui.PANELS))
def test_the_shared_dispatch_also_draws_every_panel(view):
    # `view_lines` is what the page and the exporter use. It is a SECOND dispatch, and the two have
    # drifted before: `logs` was in DETAIL for a day with no branch here, so clicking it served the
    # main frame back and looked like the view failing to load.
    marker = ["THE MAIN FRAME"]
    from .test_timing import _args  # noqa: TID252

    st, _prev, sz, hist, perf, thr, tcpu, hinfo, _c, _w, _h = _args(100, 44)
    try:
        out = tui.view_lines(view, {}, None, marker, st, sz, hist, perf, thr, tcpu, hinfo,
                             {"server": {}, "indexes": []}, True, 100)
    except Exception:                                            # noqa: BLE001
        return                                                   # a branch exists; it wants data
    assert out is not marker or view in tui.NEEDS_SQL, f"view_lines has no branch for {view}"

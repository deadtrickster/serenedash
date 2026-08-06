"""The dashboard in a real browser.

Every test here is a bug that shipped. The unit tests cover the Hub and the exporter and were all
green while the page was unusable, because what broke was the half that only exists in a browser:
an EventSource that reconnects to a server which has forgotten what the tab was showing, a key bar
that is only navigation if it can be clicked, and a filter box that must not let its keystrokes
fall through to the view shortcuts.
"""
import re

import pytest

from serenedash.views import BINDINGS, NOT_ON_THE_PAGE

pytestmark = pytest.mark.browser


# SVG text is not innerText, and the exporter emits one <text> per styled run with no whitespace
# between them - `q quit` is a bold "q" and a dim "quit" and concatenates to "qquit". Joined with a
# space, which puts the reading back the way it appears on screen.
TEXT = "[...document.querySelectorAll('#f svg text')].map(e => e.textContent).join(' ')"


def frame(page):
    return page.evaluate(f"() => {TEXT}")


def wait_for_frame(page, contains=None, without=None, timeout=15000):
    """Wait for a frame that says one thing and, when asked, has stopped saying another.

    `without` matters more than it looks: the unfiltered log already contains the error line the
    filter selects, so waiting only for what should appear matches the frame that was already on
    screen and the test passes without the filter having done anything.
    """
    page.wait_for_selector("#f svg", timeout=timeout)
    if contains:
        page.wait_for_function(f"t => {TEXT}.includes(t)", arg=contains, timeout=timeout)
    if without:
        page.wait_for_function(f"t => !{TEXT}.includes(t)", arg=without, timeout=timeout)


def test_the_page_draws_a_frame(dash, page):
    page.goto(dash.url)
    wait_for_frame(page)
    assert page.locator("#f svg rect").count() > 0
    assert "f findings" in frame(page), "the key bar is the navigation and has to be on the frame"


def test_there_is_no_second_row_of_buttons(dash, page):
    # Removed deliberately: it repeated the key bar the frame already draws, and the selected one
    # went bold, which reflowed the row under the pointer.
    page.goto(dash.url)
    wait_for_frame(page)
    assert page.locator("nav").count() == 0
    assert page.locator("#f svg rect.hit").count() >= 8, "the bar has to be the navigation instead"


def test_the_key_bar_is_clickable_and_switches_the_view(dash, page):
    page.goto(dash.url)
    wait_for_frame(page)
    page.locator("#f svg rect.hit").filter(has=page.locator("title", has_text="key: o")).click()
    wait_for_frame(page, "checkpoint")
    assert "log" in frame(page)
    assert page.url.endswith("/logs"), "the URL has to describe what the tab is showing"


def test_a_hit_area_lights_up_under_the_pointer(dash, page):
    # The affordance. It is an invisible rect, so hover is the only thing that says it is clickable
    # - and it must not move anything, which is what the old buttons did.
    page.goto(dash.url)
    wait_for_frame(page)
    hit = page.locator("#f svg rect.hit").first
    before = hit.evaluate("e => getComputedStyle(e).opacity")
    hit.hover()
    page.wait_for_function("e => getComputedStyle(e).opacity !== '0'", arg=hit.element_handle(),
                           timeout=3000)
    assert float(before) == 0 and float(hit.evaluate("e => getComputedStyle(e).opacity")) > 0


def test_a_keypress_switches_the_view(dash, page):
    page.goto(dash.url)
    wait_for_frame(page)
    page.keyboard.press("o")
    wait_for_frame(page, "checkpoint")
    assert page.url.endswith("/logs")


def test_escape_goes_back_to_main(dash, page):
    page.goto(dash.url + "/logs")
    wait_for_frame(page, "checkpoint")
    page.keyboard.press("Escape")
    wait_for_frame(page, "f findings")
    assert page.url.rstrip("/").endswith("/main")


def test_a_view_link_opened_directly_shows_that_view(dash, page):
    # It used to show main: the page knew it wanted `logs`, the server did not, and every frame it
    # sent was for `main` and was discarded. Only `main` ever loaded from a link.
    page.goto(dash.url + "/storage")
    wait_for_frame(page)
    assert "storage" in frame(page).lower()


def test_a_tab_survives_the_dashboard_restarting(dash, page):
    # The reported bug, end to end: kill the dashboard under an open tab, start it again, and the
    # tab has to come back to the panel it was on without being reloaded.
    page.goto(dash.url + "/logs")
    wait_for_frame(page, "checkpoint")
    dash.stop()
    page.wait_for_function("() => document.getElementById('s').className.includes('off')",
                           timeout=15000)
    page.evaluate("document.querySelector('#f svg').dataset.stale = '1'")
    dash.start()
    page.wait_for_function(
        "() => { const s = document.querySelector('#f svg'); return s && !s.dataset.stale; }",
        timeout=30000)
    assert "checkpoint" in frame(page), "came back to a different view than the tab was showing"
    assert page.url.endswith("/logs")


def test_two_tabs_can_watch_different_views(dash, page, context):
    # One shared server-side view meant the second tab silently blanked the first.
    page.goto(dash.url + "/logs")
    wait_for_frame(page, "checkpoint")
    other = context.new_page()
    other.goto(dash.url + "/storage")
    other.wait_for_selector("#f svg")
    page.wait_for_timeout(1000)                  # a few ticks with both connected
    assert "checkpoint" in frame(page) and "storage" in frame(other).lower()
    other.close()


def test_slash_focuses_the_log_filter(dash, page):
    page.goto(dash.url + "/logs")
    wait_for_frame(page, "checkpoint")
    page.keyboard.press("/")
    assert page.evaluate("document.activeElement.id") == "q"


def test_the_filter_narrows_the_log_and_survives_a_reload(dash, page):
    page.goto(dash.url + "/logs")
    wait_for_frame(page, "checkpoint")
    page.fill("#q", "index build")
    wait_for_frame(page, "index build failed", without="checkpoint")
    page.wait_for_url(re.compile(r"/logs\?q=index"), timeout=5000)
    page.reload()                                 # the filter is in the URL, so it comes back
    wait_for_frame(page, "index build failed", without="checkpoint")
    assert page.input_value("#q") == "index build"


def test_typing_a_filter_does_not_fire_the_view_shortcuts(dash, page):
    # 's' is the storage view. Typing "search" into the filter box must not navigate away twice
    # mid-word, which is what happens when a global keydown handler sees the box's keystrokes.
    page.goto(dash.url + "/logs")
    wait_for_frame(page, "checkpoint")
    page.click("#q")
    page.keyboard.type("disk")
    page.wait_for_timeout(600)
    # Still on the log, now with the filter as its parameter - which is the point of keeping the
    # view in the path and the filter in the query.
    assert page.url.rstrip("/").split("?")[0].endswith("/logs")
    assert page.input_value("#q") == "disk"


def test_the_filter_box_only_appears_where_it_does_something(dash, page):
    page.goto(dash.url)
    wait_for_frame(page)
    assert not page.locator("#q").is_visible()
    page.keyboard.press("o")
    wait_for_frame(page, "checkpoint")
    assert page.locator("#q").is_visible()


def test_the_frame_scales_to_the_window_rather_than_sitting_at_a_fixed_width(dash, page):
    # It was fixed at 1229px with the rest of a wide window empty beside it, and each view sized
    # its own grid, so the storage panel rendered in a visibly bigger font than the main frame.
    page.set_viewport_size({"width": 1600, "height": 900})
    page.goto(dash.url)
    wait_for_frame(page)
    wide = page.locator("#f svg").bounding_box()["width"]
    page.keyboard.press("s")
    page.wait_for_timeout(700)
    assert abs(page.locator("#f svg").bounding_box()["width"] - wide) < 2, "views scale differently"
    page.set_viewport_size({"width": 800, "height": 900})
    page.wait_for_timeout(300)
    assert page.locator("#f svg").bounding_box()["width"] < wide
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")


def test_the_page_says_so_when_the_dashboard_goes_away(dash, page):
    # A dashboard that quietly stops updating is worse than one that says it is disconnected -
    # stale numbers read as current ones.
    page.goto(dash.url)
    wait_for_frame(page)
    dash.stop()
    page.wait_for_function("() => document.getElementById('s').textContent.includes('disconnected')",
                           timeout=15000)


def test_j_and_k_move_the_selection_in_the_browser(dash, page):
    # The report: "in web version j k do not move anything anywhere". The page knew only how to
    # switch views, so every navigation key was silently dropped on the one view built around
    # moving through a list. SSE has no channel back, so they go as a GET and the server pushes the
    # new frame to that subscriber alone.
    page.goto(dash.url + "/mcp")
    wait_for_frame(page, "sessions")
    first = frame(page)
    page.keyboard.press("j")
    page.wait_for_function(f"t => {TEXT} !== t", arg=first, timeout=8000)
    assert frame(page) != first, "j moved nothing"
    page.keyboard.press("k")
    page.wait_for_function(f"t => {TEXT} === t", arg=first, timeout=8000)


def test_enter_descends_and_escape_climbs_back_in_the_browser(dash, page):
    page.goto(dash.url + "/mcp")
    wait_for_frame(page, "sessions")
    page.keyboard.press("Enter")
    wait_for_frame(page, "enter shows the call")     # level 2, that session's calls
    page.keyboard.press("Enter")
    wait_for_frame(page, "scrolls the reply")                     # level 3, the call in full
    page.keyboard.press("Escape")
    wait_for_frame(page, "enter shows the call", without="scrolls the reply")
    page.keyboard.press("Escape")
    wait_for_frame(page, "enter opens the session")        # back at level 1
    assert page.url.endswith("/mcp"), "escape must unwind the levels before leaving the view"


def test_escape_with_nothing_open_still_leaves_the_view(dash, page):
    page.goto(dash.url + "/mcp")
    wait_for_frame(page, "enter opens the session")
    page.keyboard.press("Escape")
    wait_for_frame(page, "f findings")
    page.wait_for_url(re.compile(r"/main$"), timeout=8000)


def test_two_tabs_scroll_independently(dash, page, context):
    # Position is per subscriber, like the view and the filter. One shared cursor would mean two
    # people reading the same log move each other's screen.
    page.goto(dash.url + "/mcp")
    wait_for_frame(page, "sessions")
    other = context.new_page()
    other.goto(dash.url + "/mcp")
    other.wait_for_selector("#f svg")
    before = frame(other)
    page.keyboard.press("Enter")
    wait_for_frame(page, "enter shows the call")
    other.wait_for_timeout(800)                            # a few ticks with both connected
    assert frame(other) == before, "one tab's keypress moved another tab"
    other.close()


def test_navigation_keys_are_left_alone_where_they_do_nothing(dash, page):
    # `j` is not bound on the storage panel. A page that took it to do nothing would be worse than
    # one that let it fall through to the browser.
    page.goto(dash.url + "/storage")
    wait_for_frame(page)
    before = frame(page)
    page.keyboard.press("j")
    page.wait_for_timeout(700)
    assert frame(page) == before


def test_the_key_that_doctor_used_to_have_still_opens_the_screen(dash, page):
    # Reported as "d works in tui but not in web": each front end built its own {key: view} map and
    # the page was handed DETAIL alone, which no longer has doctor in it.
    page.goto(dash.url)
    wait_for_frame(page)
    page.keyboard.press("d")
    page.wait_for_url(re.compile(r"/findings$"), timeout=8000)


def test_a_view_key_pressed_on_its_own_view_goes_back(dash, page):
    # The terminal toggles - the key that opens a view closes it - and the page did not, so a key
    # pressed while already on its own view did nothing, which reads exactly like an unbound key.
    page.goto(dash.url + "/logs")
    wait_for_frame(page, "checkpoint")
    page.keyboard.press("o")
    page.wait_for_url(re.compile(r"/main$"), timeout=8000)


def test_a_view_is_a_path_not_a_query_string(dash, page):
    # `/?view=findings` was a query string doing a path's job. Each panel is a page: it has a name,
    # you link to it, you go back to the one before.
    page.goto(dash.url + "/findings")
    wait_for_frame(page)
    assert page.url.endswith("/findings")
    page.keyboard.press("o")
    page.wait_for_url(re.compile(r"/logs$"), timeout=8000)
    assert "?" not in page.url, "the view is the path; the query is for parameters OF the page"


def test_back_returns_to_the_panel_you_were_on(dash, page):
    # replaceState overwrote the entry you came from, so Back left the dashboard entirely rather
    # than going back one panel.
    page.goto(dash.url + "/main")
    wait_for_frame(page)
    page.keyboard.press("o")
    wait_for_frame(page, "checkpoint")
    page.keyboard.press("s")
    page.wait_for_url(re.compile(r"/storage$"), timeout=8000)
    page.go_back()
    page.wait_for_url(re.compile(r"/logs$"), timeout=8000)
    wait_for_frame(page, "checkpoint")
    page.go_forward()
    page.wait_for_url(re.compile(r"/storage$"), timeout=8000)


def test_the_filter_refines_the_page_rather_than_navigating(dash, page):
    # Typing is not navigating: a history entry per keystroke is not history. One Back from a
    # filtered log leaves the log, it does not walk the word back a letter at a time.
    page.goto(dash.url + "/main")
    wait_for_frame(page)
    page.keyboard.press("o")
    wait_for_frame(page, "checkpoint")
    page.fill("#q", "index build")
    page.wait_for_url(re.compile(r"/logs\?q=index"), timeout=8000)
    page.go_back()
    page.wait_for_url(re.compile(r"/main$"), timeout=8000)


def test_a_deep_link_with_a_filter_opens_filtered(dash, page):
    page.goto(dash.url + "/logs?q=index+build")
    wait_for_frame(page, "index build failed", without="checkpoint")
    assert page.input_value("#q") == "index build"


def test_the_key_bar_does_not_move_between_views(dash, page):
    # "things shouldn't jump". On the page the frame is an SVG sized to its own content, so a short
    # panel pulled the bar up and a long one pushed it down - the terminal never did that, because
    # it pads to the window.
    page.goto(dash.url + "/main")
    wait_for_frame(page)
    rows = []
    for view in ("main", "logs", "mcp", "storage"):
        page.goto(dash.url + "/" + view)
        page.wait_for_selector("#f svg")
        page.wait_for_timeout(400)
        # The bottom-most text on the frame IS the bar - it is the last thing drawn on every view -
        # so this needs no marker word and cannot be fooled by a label that also appears in the
        # content of one panel.
        rows.append(page.evaluate(
            "() => Math.round(Math.max(...[...document.querySelectorAll('#f svg text')]"
            ".map(e => +e.getAttribute('y'))))"))
    assert len(set(rows)) == 1, f"the key bar sat at {rows} across four views"
    assert rows[0] > 0, "no text on the frame at all"


def test_the_frame_is_the_same_height_on_every_view(dash, page):
    heights = []
    for view in ("main", "logs", "mcp", "findings"):
        page.goto(dash.url + "/" + view)
        page.wait_for_selector("#f svg")
        page.wait_for_timeout(400)
        heights.append(page.evaluate(
            "() => document.querySelector('#f svg').getAttribute('viewBox')").split()[3])
    assert len(set(heights)) == 1, f"the frame resized between views: {heights}"


def test_the_key_bar_is_clickable_after_the_frame_is_scaled_to_fit(dash, page):
    # The frame is capped at the viewport height now, so on a short window the whole SVG is scaled
    # down. A hit area is a rect inside that SVG, and a click has to land through the scale.
    page.set_viewport_size({"width": 1400, "height": 500})
    page.goto(dash.url + "/main")
    wait_for_frame(page)
    box = page.locator("#f svg rect.hit").filter(has=page.locator("title", has_text="key: o"))
    assert box.count() == 1, "the key bar has no clickable box at all"
    box.click()
    page.wait_for_url(re.compile(r"/logs$"), timeout=8000)


def test_the_key_bar_is_clickable_on_a_detail_view_too(dash, page):
    # Detail views get their bar appended rather than built in, and the boxes are computed from the
    # bar's own text - so "is there a bar" and "is it clickable" are two different questions.
    page.goto(dash.url + "/storage")
    wait_for_frame(page)
    page.locator("#f svg rect.hit").filter(has=page.locator("title", has_text="key: m")).click()
    page.wait_for_url(re.compile(r"/memory$"), timeout=8000)


def test_the_page_does_not_scroll(dash, page):
    # A dashboard you have to scroll is two dashboards. The frame is a fixed 50 rows now, which at
    # width:100% was taller than the window.
    for h in (500, 800, 1200):
        page.set_viewport_size({"width": 1400, "height": h})
        page.goto(dash.url + "/main")
        wait_for_frame(page)
        page.wait_for_timeout(200)
        assert page.evaluate(
            "() => document.documentElement.scrollHeight <= window.innerHeight + 2"), f"at {h}px"


# ---- every key on the bar, from the map rather than from a list typed out here ------------------
# `g` and `c` were printed on the served bar and were in neither the map nor the list of views the
# page had been given, so one was unclickable and the other did nothing.

@pytest.mark.parametrize(("key", "view"), sorted(
    (k, v) for k, v, _l in BINDINGS if v and k not in NOT_ON_THE_PAGE))
def test_every_key_on_the_bar_opens_its_view(dash, page, key, view):
    page.goto(dash.url + "/main")
    wait_for_frame(page)
    page.keyboard.press(key)
    page.wait_for_url(re.compile(rf"/{view}$"), timeout=8000)


@pytest.mark.parametrize(("key", "view"), sorted(
    (k, v) for k, v, _l in BINDINGS if v and k not in NOT_ON_THE_PAGE))
def test_every_key_on_the_bar_is_clickable(dash, page, key, view):
    page.goto(dash.url + "/main")
    wait_for_frame(page)
    box = page.locator("#f svg rect.hit").filter(
        has=page.locator("title", has_text=f"key: {key}"))
    assert box.count() == 1, f"{key} is printed on the bar with no box under it"
    box.click()
    page.wait_for_url(re.compile(rf"/{view}$"), timeout=8000)


def test_the_bar_does_not_offer_what_a_browser_cannot_do(dash, page):
    # `q quit` and `x mouse` were on the served bar. A browser cannot quit and has no terminal
    # mouse tracking to toggle, and documentation that lists a key which does nothing is worse than
    # a shorter bar.
    page.goto(dash.url + "/main")
    wait_for_frame(page)
    for key in NOT_ON_THE_PAGE:
        assert page.locator("#f svg rect.hit").filter(
            has=page.locator("title", has_text=f"key: {key}")).count() == 0
    assert not any(lbl in frame(page) for lbl in ("q quit", "x mouse"))


def test_the_alias_still_opens_the_screen_it_used_to(dash, page):
    page.goto(dash.url + "/main")
    wait_for_frame(page)
    page.keyboard.press("d")
    page.wait_for_url(re.compile(r"/findings$"), timeout=8000)


def test_e_plans_the_open_statement_in_the_browser(dash, page):
    # `e` is not a navigation key - it moves no cursor - so the page dropped it silently, the way
    # it dropped j and k before them. The plan and the statement are both long, so the plan takes
    # the statement's place rather than following it down the panel.
    page.goto(dash.url + "/activity")
    wait_for_frame(page, "enter opens the statement")
    page.keyboard.press("Enter")
    wait_for_frame(page, "explains it")               # a statement is open; `e` now means something
    assert "m_idx" in frame(page), "the statement should be on screen before it is planned"
    page.keyboard.press("e")
    wait_for_frame(page, "IRESEARCH_SCAN")
    body = frame(page)
    assert "bm25(k1=1.2, b=0.75)" in body
    assert "shows the statement" in body, "the hint has to offer the way back"


def test_e_toggles_back_to_the_statement(dash, page):
    page.goto(dash.url + "/activity")
    wait_for_frame(page, "enter opens the statement")
    page.keyboard.press("Enter")
    wait_for_frame(page, "explains it")
    page.keyboard.press("e")
    wait_for_frame(page, "IRESEARCH_SCAN")
    page.keyboard.press("e")
    wait_for_frame(page, "explains it", without="IRESEARCH_SCAN")


def test_moving_off_a_statement_drops_its_plan(dash, page):
    # The plan belongs to ONE statement. Left on screen after the cursor moves it is not stale
    # decoration, it is the wrong answer under the right heading.
    page.goto(dash.url + "/activity")
    wait_for_frame(page, "enter opens the statement")
    page.keyboard.press("Enter")
    page.keyboard.press("e")
    wait_for_frame(page, "IRESEARCH_SCAN")
    page.keyboard.press("Escape")                                  # back to the list
    wait_for_frame(page, "enter opens the statement", without="IRESEARCH_SCAN")
    page.keyboard.press("Enter")
    wait_for_frame(page, "explains it", without="IRESEARCH_SCAN")


def test_e_does_nothing_on_a_view_that_cannot_plan(dash, page):
    # It must not reach the server as a view switch either: `e` opens no view, and a key that does
    # nothing has to do nothing rather than something surprising.
    page.goto(dash.url + "/mcp")
    wait_for_frame(page, "enter opens the session")
    before = frame(page)
    page.keyboard.press("e")
    page.wait_for_timeout(600)
    assert frame(page) == before, "e changed the mcp view"
    assert page.url.endswith("/mcp")

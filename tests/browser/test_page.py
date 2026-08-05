"""The dashboard in a real browser.

Every test here is a bug that shipped. The unit tests cover the Hub and the exporter and were all
green while the page was unusable, because what broke was the half that only exists in a browser:
an EventSource that reconnects to a server which has forgotten what the tab was showing, a key bar
that is only navigation if it can be clicked, and a filter box that must not let its keystrokes
fall through to the view shortcuts.
"""
import re

import pytest

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
    assert "q quit" in frame(page)


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
    assert "view=logs" in page.url, "the URL has to describe what the tab is showing"


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
    assert "view=logs" in page.url


def test_escape_goes_back_to_main(dash, page):
    page.goto(dash.url + "/?view=logs")
    wait_for_frame(page, "checkpoint")
    page.keyboard.press("Escape")
    wait_for_frame(page, "q quit")
    assert "view=main" in page.url


def test_a_view_link_opened_directly_shows_that_view(dash, page):
    # It used to show main: the page knew it wanted `logs`, the server did not, and every frame it
    # sent was for `main` and was discarded. Only `main` ever loaded from a link.
    page.goto(dash.url + "/?view=storage")
    wait_for_frame(page)
    assert "storage" in frame(page).lower()


def test_a_tab_survives_the_dashboard_restarting(dash, page):
    # The reported bug, end to end: kill the dashboard under an open tab, start it again, and the
    # tab has to come back to the panel it was on without being reloaded.
    page.goto(dash.url + "/?view=logs")
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
    assert "view=logs" in page.url


def test_two_tabs_can_watch_different_views(dash, page, context):
    # One shared server-side view meant the second tab silently blanked the first.
    page.goto(dash.url + "/?view=logs")
    wait_for_frame(page, "checkpoint")
    other = context.new_page()
    other.goto(dash.url + "/?view=storage")
    other.wait_for_selector("#f svg")
    page.wait_for_timeout(1000)                  # a few ticks with both connected
    assert "checkpoint" in frame(page) and "storage" in frame(other).lower()
    other.close()


def test_slash_focuses_the_log_filter(dash, page):
    page.goto(dash.url + "/?view=logs")
    wait_for_frame(page, "checkpoint")
    page.keyboard.press("/")
    assert page.evaluate("document.activeElement.id") == "q"


def test_the_filter_narrows_the_log_and_survives_a_reload(dash, page):
    page.goto(dash.url + "/?view=logs")
    wait_for_frame(page, "checkpoint")
    page.fill("#q", "index build")
    wait_for_frame(page, "index build failed", without="checkpoint")
    page.wait_for_url(re.compile(r"q=index"), timeout=5000)
    page.reload()                                 # the filter is in the URL, so it comes back
    wait_for_frame(page, "index build failed", without="checkpoint")
    assert page.input_value("#q") == "index build"


def test_typing_a_filter_does_not_fire_the_view_shortcuts(dash, page):
    # 's' is the storage view. Typing "search" into the filter box must not navigate away twice
    # mid-word, which is what happens when a global keydown handler sees the box's keystrokes.
    page.goto(dash.url + "/?view=logs")
    wait_for_frame(page, "checkpoint")
    page.click("#q")
    page.keyboard.type("disk")
    page.wait_for_timeout(600)
    assert "view=logs" in page.url and page.input_value("#q") == "disk"


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

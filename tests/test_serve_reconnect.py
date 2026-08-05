"""A tab must come back after the dashboard is restarted, showing what it was showing.

The failure: which view to draw was ONE variable on the server, shared by every browser. The page
discards frames that are not for the view it is showing, so any tab whose choice the server did not
know about threw every frame away and sat empty - after a restart, on a fresh load of a `?view=`
link, and whenever another tab switched. It looked like a connection problem and was a state
problem: reloading "fixed" it only because the reload happened to re-send the choice.

The view is now the subscriber's, carried in the stream URL, which also means EventSource's own
reconnect asks for the right panel with no code. These tests are on the Hub rather than over a
socket: the property is about who owns the view, and a real HTTP round trip would only make the
same assertions slower and flakier.
"""
import json

import pytest

from serenedash.serve import PAGE, Hub, frame_payload
from serenedash.views import DETAIL


def render(name, needle="", st=None):
    return frame_payload(name, [f"frame for {name}{' /' + needle if needle else ''}",
                                "q quit  s storage"],
                         cols=40, keys={k: v for v, k in DETAIL.items()})


def view_of(payload):
    return json.loads(payload)["view"]


def test_a_subscriber_gets_the_view_it_asked_for_immediately():
    # Not after one tick. A tab that draws nothing for five seconds reads as broken.
    hub = Hub()
    assert view_of(hub.subscribe("logs", render).get_nowait()) == "logs"


def test_two_tabs_can_watch_different_views():
    # The old shared variable made this impossible: the second tab's choice silently blanked the
    # first, because the first then discarded every frame it was sent.
    hub = Hub()
    a, b = hub.subscribe("logs", render), hub.subscribe("storage", render)
    a.get_nowait(), b.get_nowait()                       # the immediate frames
    hub.publish_tick(render)
    assert view_of(a.get_nowait()) == "logs" and view_of(b.get_nowait()) == "storage"


def test_a_reconnecting_tab_resumes_its_own_view():
    # The reported bug, in one test: dashboard restarts, EventSource retries by itself, and the
    # frames that arrive have to be for the panel the tab was on - not for whatever the server
    # happens to consider current.
    hub = Hub()                                          # a NEW hub: the restarted dashboard
    q = hub.subscribe("logs", render)                    # the browser's automatic retry
    assert view_of(q.get_nowait()) == "logs"
    hub.publish_tick(render)
    assert view_of(q.get_nowait()) == "logs"


def test_nothing_is_rendered_for_a_view_nobody_is_watching():
    hub = Hub()
    hub.subscribe("logs", render)
    asked = []
    hub.publish_tick(lambda name, q="", st=None: asked.append(name) or render(name, q))
    assert asked == ["logs"]


def test_no_browser_connected_means_no_render_at_all():
    hub = Hub()
    hub.publish_tick(lambda name, q="", st=None: pytest.fail(f"rendered {name}, nobody watching"))


def test_a_view_that_raises_does_not_stop_the_others():
    hub = Hub()
    good = hub.subscribe("storage", render)
    hub.subscribe("logs", render)
    good.get_nowait()
    def half_broken(name, needle="", st=None):
        if name == "logs":
            raise RuntimeError("the log source went away")
        return render(name)
    hub.publish_tick(half_broken)
    assert view_of(good.get_nowait()) == "storage"


def test_a_slow_reader_is_dropped_rather_than_grown():
    # A backgrounded tab that stops reading must not grow a queue until the dashboard runs out of
    # memory. Losing frames is the correct loss here - only the newest was ever interesting.
    hub = Hub()
    q = hub.subscribe("storage", render)
    for _ in range(50):
        hub.publish_tick(render)
    assert q.qsize() <= 4


def test_unsubscribing_stops_the_work():
    hub = Hub()
    q = hub.subscribe("logs", render)
    hub.unsubscribe(q)
    hub.publish_tick(lambda name, q="", st=None: pytest.fail(f"rendered {name} after close"))


def test_the_filter_travels_with_the_view():
    # Two tabs on the same panel with different filters are two subscribers, not one. And a tab
    # that reconnects mid-search should come back filtered rather than to the whole log.
    hub = Hub()
    a, b = hub.subscribe("logs", render, "ERROR"), hub.subscribe("logs", render)
    assert json.loads(a.get_nowait())["svg"] != json.loads(b.get_nowait())["svg"]
    seen = []
    hub.publish_tick(lambda name, q="", st=None: seen.append((name, q)) or render(name, q))
    assert sorted(seen) == [("logs", ""), ("logs", "ERROR")]


def test_the_page_offers_a_box_for_the_views_that_filter():
    # The log header advertises a search; `i` is the search VIEW and switching away from the log
    # was not what anyone meant by it. `/` focuses the box, which is where that muscle memory is.
    assert "id=q" in PAGE and "FILTERS = ['logs']" in PAGE
    assert "e.key === '/'" in PAGE
    assert "e.stopPropagation()" in PAGE, "view keys must not fire while typing a filter"


def test_the_page_reconnects_by_reopening_the_stream_not_by_asking_the_server_to_remember():
    assert "/stream?view=" in PAGE
    assert "/view?name=" not in PAGE, "the shared server-side view is what broke reconnects"
    assert "history.replaceState" in PAGE, "the URL has to describe what the tab is showing"


def test_a_tab_that_connects_before_the_first_tick_is_sent_nothing_rather_than_an_empty_frame():
    # An empty frame blanks whatever the tab was showing. "Still connecting" and "here is nothing"
    # look identical on screen and are not the same thing.
    hub = Hub()
    assert hub.subscribe("logs").empty()


def test_the_wait_actually_waits_when_there_is_no_terminal():
    # `--serve` under systemd or nohup has no tty. wait_key used to return instantly there, so the
    # caller counted every spin as an elapsed interval and re-ran the whole collection path flat
    # out - a dashboard pinning a core and hammering the server it is watching.
    import time

    from serenedash.tui import wait_key

    t0 = time.monotonic()
    wait_key(0.4)
    assert time.monotonic() - t0 >= 0.35, "wait_key returned early with no tty"

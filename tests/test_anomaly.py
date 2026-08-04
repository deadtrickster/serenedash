"""The rules, and — more importantly — what they must stay quiet about.

A detector on a dashboard is judged by its false positives. One that fires on ordinary variation
teaches its reader to ignore the highlight, at which point it is worse than not having one, so
half of these assert silence.
"""
import random

from serenedash.anomaly import index, scan, scan_series, series_of, spread

G = 2**30


def noisy(base, spread_frac, n, seed=1):
    r = random.Random(seed)
    return [base * (1 + r.uniform(-spread_frac, spread_frac)) for _ in range(n)]


def test_ordinary_jitter_is_not_an_anomaly():
    assert scan_series("mem", noisy(34 * G, 0.02, 120)) is None
    assert scan_series("cpu", noisy(250, 0.3, 120)) is None


def test_a_flat_series_does_not_fire_on_rounding():
    # MAD is exactly zero here, so without the absolute floor every last-digit wobble is an
    # infinite number of sigmas.
    assert scan_series("mem", [34 * G] * 100 + [34 * G + 4096]) is None


def test_an_empty_or_short_series_says_nothing():
    assert scan_series("mem", []) is None
    assert scan_series("mem", [1 * G, 40 * G]) is None            # no baseline to judge against
    assert scan_series("mem", [0] * 200) is None                  # never held anything


def test_a_single_excursion_is_a_spike():
    a = scan_series("rss", [8 * G] * 100 + [30 * G])
    assert a.rule == "spike"
    assert a.baseline == 8 * G


def test_a_step_that_stays_up_is_a_shift_not_a_spike():
    # The distinction is the whole point: an excursion comes back, a level change does not, and
    # they call for different reactions.
    a = scan_series("swap", [1 * G] * 80 + [26 * G] * 40)
    assert a.rule == "shift"
    assert a.label() == "step up"


def test_a_step_down_says_so():
    a = scan_series("t:BASE_TABLE", [30 * G] * 80 + [1 * G] * 40)
    assert a.rule == "shift"
    assert a.label() == "step down"


def test_a_monotonic_climb_is_growth():
    a = scan_series("t:BASE_TABLE", [10 * G + i * 0.25 * G for i in range(120)])
    assert a.rule == "growth"
    assert "not being released" in a.detail


def test_one_jump_at_the_end_of_a_flat_series_is_not_a_climb():
    # It reported growth: every step was non-decreasing, because 99 of them were zero. A leak
    # arrives in many increments — that is what separates it from a single allocation.
    a = scan_series("rss", [8 * G] * 100 + [30 * G])
    assert a.rule != "growth"


def test_a_climb_that_comes_back_down_is_not_a_leak():
    up = [10 * G + i * 0.25 * G for i in range(60)]
    a = scan_series("t:HASH_TABLE", up + up[::-1])
    assert a is None or a.rule != "growth"


def test_the_baseline_survives_the_event_it_is_measuring():
    # A mean would be dragged by the spike and a standard deviation inflated by it, so a big enough
    # excursion hides itself. Half the window can be arbitrary here without moving the median.
    vals = [10 * G] * 60 + [900 * G] * 59
    med, sd = spread(vals[:60], "bytes")
    assert med == 10 * G
    a = scan_series("mem", vals + [900 * G])
    assert a is not None and a.baseline == 10 * G


def test_every_detection_carries_its_own_evidence():
    a = scan_series("swap", [1 * G] * 80 + [26 * G] * 40)
    f = a.as_finding()
    for key in ("what", "detail", "series", "rule", "value", "baseline", "window_samples"):
        assert key in f, key
    assert str(f["window_samples"]) in a.line() or f["window_samples"] == 120


def test_scan_is_stable_and_indexed_by_series():
    hist = {"swap": [1 * G] * 80 + [26 * G] * 40, "cpu": noisy(250, 0.3, 120),
            "t:X": [10 * G + i * 0.25 * G for i in range(120)]}
    keys = [a.key for a in scan(hist)]
    assert keys == sorted(keys), "output order must not depend on dict iteration"
    assert set(index(hist)) == {"swap", "t:X"}


def test_a_row_label_maps_back_to_its_series():
    # The renderer and the tooltip both need to get from a drawn row to the series behind it, and
    # two copies of that mapping would be one copy too many.
    assert series_of("in use") == "mem"
    assert series_of("resident") == "rss"
    assert series_of("swapped") == "swap"
    assert series_of("BASE_TABLE") == "t:BASE_TABLE"
    assert series_of("") == ""

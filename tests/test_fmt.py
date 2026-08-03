"""Formatting and layout primitives.

Every case here is a bug that shipped. They are cheap to test and were expensive to find, because
each one renders as a plausible screen rather than an error.
"""
from serenedash.fmt import C, bar, clip, dur, human, spark, strip, to_bytes


def coloured(text, colour=C["dim"]):
    return f"{colour}{text}{C['r']}"


class TestClip:
    def test_counts_visible_columns_not_bytes(self):
        # A raw s[:n] charges the escape bytes against the width, so a row carrying four colours
        # silently loses about twenty real characters.
        s = coloured("columnar 50%") + "  " + coloured("vector 27%", C["mag"])
        assert len(strip(clip(s, 20))) == 21          # 20 kept + the ellipsis
        assert len(strip(s[:20])) < 20

    def test_exact_fit_is_not_truncated(self):
        # The bug: trailing escapes remain unconsumed at the cut, the code concluded it had
        # truncated, and appended an ellipsis worth one real column. One memory row sat a cell
        # right of every other, but only when colour was on.
        s = f"35.0% {C['dim']}of memory_limit{C['r']}"
        assert len(strip(s)) == 21
        assert strip(clip(s, 21)) == "35.0% of memory_limit"
        assert "…" not in clip(s, 21)

    def test_truncation_still_marks_and_closes_colour(self):
        out = clip(coloured("abcdefghij"), 4)
        assert strip(out) == "abcd…"
        assert out.endswith(C["r"])

    def test_no_escape_leaks_without_colour(self):
        # clip used to append the global palette's reset even for uncoloured text, printing a
        # literal "…[0m" under --no-color.
        assert clip("abcdefghij", 4) == "abcd…"

    def test_never_splits_an_escape_sequence(self):
        for n in range(1, 30):
            assert "\x1b[" not in strip(clip(coloured("abcdefghij"), n))


class TestSpark:
    def test_shared_ceiling_is_comparable_across_series(self):
        # Self-scaling drew a flat 260 MB pool at the same height as a 34 GB one.
        big, small = [34_000_000_000] * 8, [260_000_000] * 8
        assert spark(big, top=34_000_000_000)[0] == "█"
        assert spark(small, top=34_000_000_000)[0] == "▁"

    def test_flat_zero_series_draws_nothing(self):
        # A flat series rendered as a mid-height bar, so thirteen empty pools drew thirteen
        # identical stripes - ink that reads as activity and means the opposite.
        assert spark([0] * 8, top=100) == ""

    def test_height_tracks_the_fraction(self):
        levels = "▁▂▃▄▅▆▇█"
        assert spark([0.1] * 4, top=100)[0] == levels[0]
        assert spark([50] * 4, top=100)[0] == levels[3]        # eight levels, so half is index 3
        assert spark([100] * 4, top=100)[0] == levels[7]
        # monotonic: a bigger share never draws shorter
        heights = [levels.index(spark([v] * 2, top=100)[0]) for v in (5, 25, 50, 75, 100)]
        assert heights == sorted(heights)


class TestBar:
    def test_absolute_scale(self):
        assert bar(0.5, 18, "").count("█") == 9
        assert bar(0.0, 18, "").count("█") == 0
        assert bar(1.0, 18, "").count("█") == 18

    def test_clamps_rather_than_overfilling(self):
        assert bar(1.4, 18, "").count("█") == 18


class TestHuman:
    def test_units(self):
        assert human(0) == "0B"
        assert human(1024) == "1.0K"
        assert human(72.6 * 2**30).endswith("G")

    def test_round_trips_through_to_bytes(self):
        assert to_bytes("100.2 GiB") == 100.2 * 2**30
        assert to_bytes("16.0 MiB") == 16 * 2**20
        assert to_bytes("nonsense") == 0.0


class TestDur:
    def test_scales_the_unit_to_the_magnitude(self):
        assert dur(45) == "45s"
        assert dur(3600 * 7 + 60 * 11) == "7h 11m"
        assert dur(86400 * 3 + 3600 * 4) == "3d 4h"
        assert dur(0) == "?"

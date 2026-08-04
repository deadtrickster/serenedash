"""The on-disk series. Nothing here is load-bearing, and that is the property under test."""
import json
import os

from serenedash import history


def test_a_round_trip_keeps_order_and_series(tmp_path):
    d = str(tmp_path / "perf")
    for i in range(5):
        assert history.append(d, 1000.0 + i, {"mem": i * 100, "cpu": i})
    got = history.load(d)
    assert got["mem"] == [0, 100, 200, 300, 400]
    assert got["cpu"] == [0, 1, 2, 3, 4]


def test_a_series_absent_from_a_sample_records_a_zero(tmp_path):
    # Not skipped: a pool that drains has to read as dropping to the floor, and two lists compared
    # position by position cannot have holes in one of them.
    d = str(tmp_path / "perf")
    history.append(d, 1.0, {"mem": 10, "t:A": 5})
    history.append(d, 2.0, {"mem": 20})
    got = history.load(d)
    assert got["t:A"] == [5, 0]
    assert got["mem"] == [10, 20]


def test_a_torn_line_does_not_lose_the_file(tmp_path):
    # The writer appends; a process killed mid-write leaves a partial last line. Reading has to
    # survive it, because the alternative is a dashboard that will not start over a cache file.
    d = str(tmp_path / "perf")
    history.append(d, 1.0, {"mem": 10})
    with open(history.path(d), "a") as f:
        f.write('{"t": 2.0, "v": {"mem": 2')
    assert history.load(d)["mem"] == [10]


def test_a_missing_or_unreadable_directory_is_just_no_history(tmp_path):
    assert history.load(str(tmp_path / "nope")) == {}
    bad = tmp_path / "file-not-dir"
    bad.write_text("x")
    assert history.append(str(bad), 1.0, {"mem": 1}) is False
    assert history.load(str(bad)) == {}


def test_it_is_trimmed_rather_than_growing_without_end(tmp_path):
    d = str(tmp_path / "perf")
    os.makedirs(d)
    # Written directly: going through append() 9000 times is the same test, slowly.
    with open(history.path(d), "w") as f:
        for i in range(history.KEEP * 2 + 500):
            f.write(json.dumps({"t": float(i), "v": {"mem": i, "pad": "x" * 200}}) + "\n")
    history.append(d, 1.0, {"mem": 1})
    with open(history.path(d)) as f:
        assert len(f.readlines()) <= history.KEEP + 1


def test_load_returns_the_tail_not_the_head(tmp_path):
    d = str(tmp_path / "perf")
    for i in range(50):
        history.append(d, float(i), {"mem": i})
    assert history.load(d, limit=10)["mem"] == list(range(40, 50))

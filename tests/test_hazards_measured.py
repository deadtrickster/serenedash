"""Hazards that measure: what has to be true of the sample before a predicate says anything.

Every test here is a case where the warning would have been false. A hazard row is prose printed
next to a live number, so the failure mode is not an exception - it is a sentence the sample cannot
support, which is the one thing AGENTS.md is a file about.
"""
from serenedash.hazards import HAZARDS, _writers


def s(**kw):
    """A sample with the keys the predicates read. Defaults are a quiet server."""
    base = {"wal": 4 * 2**20, "queries": [], "states": {}, "mem": 0, "memlimit": 0}
    base.update(kw)
    return base


def ckpt(value, sample):
    return HAZARDS["checkpoint_threshold"][1](value, sample)


def writing(n, text="INSERT INTO ragflow_a73b VALUES (…)"):
    return [("active", text, len(text)) for _ in range(n)]


def test_a_small_threshold_shared_by_writers_names_both_numbers():
    # The measured case: 16 MiB with 11 concurrent inserts, which is why analyse-and-compress was
    # ~half of a 103-second checkpoint. The panel clips the tail to as few as 64 columns, so both
    # numbers have to be in the front of the string, not after the explanation.
    w = ckpt("16.0 MiB", s(queries=writing(11)))
    assert w is not None
    assert "16.0 MiB" in w[:64] and "11" in w[:64]
    assert "row groups" in w


def test_readers_are_not_writers():
    # This deployment's active sessions are usually two BM25 selects, and counting them would have
    # the compression warning firing on a server that is not writing at all.
    q = "WITH lex AS (     SELECT id, BM25(idx_ragflow_a73b, 'x') …"
    assert ckpt("16.0 MiB", s(queries=[("active", q, 68209)] * 11)) is None


def test_an_idle_session_is_not_writing():
    # `state` is what the session is doing now; the statement text is the last one it ran. An idle
    # session holding an INSERT has already committed it - that is a row in the activity panel, not
    # a writer contributing to the WAL.
    idle = [("idle", "INSERT INTO ragflow_a73b VALUES (…)", 185000)] * 11
    assert ckpt("16.0 MiB", s(queries=idle)) is None


def test_copy_counts_only_in_the_ingest_direction():
    # COPY is the ingest feeder here, so it is a writer - but `COPY (select …) TO` and
    # `COPY t TO 'f'` are exports, and an export's subquery carries the word `from`.
    assert ckpt("16.0 MiB", s(queries=writing(3, "COPY ragflow_a73b FROM STDIN"))) is not None
    assert ckpt("16.0 MiB", s(queries=writing(3, "COPY (SELECT * FROM t) TO '/x.csv'"))) is None
    assert ckpt("16.0 MiB", s(queries=writing(3, "COPY ragflow_a73b TO '/x.csv'"))) is None


def test_a_threshold_large_enough_for_its_writers_says_nothing():
    # The finding is WAL bytes per writer between checkpoints, not the writer count on its own. At
    # 1 GiB each of these eleven still has ~93 MiB to fill, which is not the shape that was measured.
    assert ckpt("1.0 GiB", s(queries=writing(11))) is None
    # Two writers sharing a threshold is not yet a pattern.
    assert ckpt("16.0 MiB", s(queries=writing(2))) is None


def test_a_runaway_wal_is_the_other_finding_and_wins():
    # 77 GB of WAL against 16 MiB means checkpoints are NOT landing, which is the opposite of
    # "checkpoints run too often". Printing both would be two readings of one number, a line apart.
    w = ckpt("16.0 MiB", s(wal=77 * 10**9, queries=writing(11)))
    assert "not landing" in w
    assert "row groups" not in w
    # And it must not name ONE cause. "look for write errors" read as a diagnosis, and an
    # automatic checkpoint that never gets a window with no other transaction open looks identical
    # from here. Both are offered, and the direction that is wrong either way is stated.
    assert "no other transaction" in w and "writes are failing" in w
    assert "raising the threshold lets the WAL grow further" in w


def test_predicates_survive_a_sample_that_is_missing_what_they_read():
    # Panels degrade by panel: a sample can arrive without the keys a predicate wants, and the row
    # then shows its static reason rather than raising through the frame.
    for name, (_why, pred) in HAZARDS.items():
        if pred is None:
            continue
        assert pred("?", {}) is None or isinstance(pred("?", {}), str), name
        assert pred("", s()) is None or isinstance(pred("", s()), str), name


def test_the_entries_that_cannot_measure_stay_silent():
    # Both were added with the predicate deliberately off. zstd's threshold is on the column
    # AVERAGE, which needs a scan of the column; auto_checkpoint_skip_wal_threshold is bytes of
    # estimated commit size and the sample carries statement TEXT length, which is not that.
    for name in ("zstd_min_string_length", "auto_checkpoint_skip_wal_threshold"):
        why, pred = HAZARDS[name]
        assert pred is None, f"{name} must not warn off a number the sample does not have"
        assert why and len(why) > 20


def test_every_entry_carries_a_reason():
    # The row shows `why` whenever the predicate is quiet, so an entry without one is a value with
    # nothing next to it - which is what the panel exists not to be.
    for name, entry in HAZARDS.items():
        assert len(entry) == 2, name
        why, pred = entry
        assert isinstance(why, str) and why.strip(), name
        assert pred is None or callable(pred), name


def test_hazard_names_are_plain_identifiers():
    # They are interpolated into `sample()`'s settings query - the one interpolation AGENTS.md
    # allows there, on the grounds that this table is the source. That only holds while the names
    # look like this.
    import re
    for name in HAZARDS:
        assert re.fullmatch(r"[a-z_][a-z0-9_]*", name), name


def test_writers_reads_the_head_only():
    # 200 characters of statement is all the tick fetches. Anything the count needs has to be in
    # the leading keyword, so a write buried in a longer statement is not counted - and must not be
    # guessed at either.
    assert _writers(s(queries=[("active", "insert into t values (1)", 24)])) == 1
    assert _writers(s(queries=[("active", "  UPDATE t SET a = 1", 20)])) == 1
    assert _writers(s(queries=[("active", "", 0)])) == 0
    assert _writers({}) == 0

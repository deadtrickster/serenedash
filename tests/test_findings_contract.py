"""The contract between what the collector emits and what the renderer can draw.

Both halves of this file exist because both halves failed on a live server, silently, in the same
afternoon:

- `snapshot` grew a finding of kind `activity` and `views.KINDNAME` never learned about it, so every
  long-query row on the findings screen was labelled `?`. A patch that was supposed to add it
  matched a block that no longer existed and reported success.
- `cpu_burn` read `host["cpu_percent_of_one_core"]`, a key `system.hostinfo` has never produced, so
  the finding could not fire at any input. It was written, reviewed, shipped and dead.

Neither is a rendering bug or a collection bug. Both are the seam between them, which nothing was
testing: the suite checked that frames have the right height and that findings carry their numbers,
and never that the two modules agree about what a finding IS.
"""
import inspect
import os
import re

import pytest

from serenedash import snapshot, system
from serenedash.fmt import NOCOLOR, strip
from serenedash.views import KINDCOL, KINDNAME, findings_frame, summary_line

# Every kind any producer in `snapshot` can attach to a finding, found in the source rather than
# listed here - a list beside the code is a second copy that goes stale, which is the bug.
EMITTED = set(re.findall(r'"kind":\s*"([a-z]+)"', inspect.getsource(snapshot)))


def test_the_source_actually_yields_kinds():
    # Guard the guard: if the regex stops matching, every test below passes vacuously.
    assert len(EMITTED) >= 5, f"only found {EMITTED}; the scan is broken, not the code"


@pytest.mark.parametrize("kind", sorted(EMITTED))
def test_every_kind_the_collector_emits_has_a_label(kind):
    # `?` on screen is what this looked like. The row was correct, complete and unreadable.
    assert kind in KINDNAME, f"snapshot emits kind={kind!r} and views has no name for it"


@pytest.mark.parametrize("kind", sorted(EMITTED))
def test_every_kind_the_collector_emits_has_a_colour(kind):
    assert kind in KINDCOL or kind == "other", f"kind={kind!r} has no colour"


@pytest.mark.parametrize("kind", sorted(EMITTED))
def test_no_kind_renders_as_a_question_mark(kind):
    # The end-to-end version: build a finding of this kind and look at the row.
    f = [{"kind": kind, "what": f"a {kind} finding", "detail": "with a detail."}]
    rows = [strip(x) for x in findings_frame(f, False, 120, 0, 0, 20) if x.strip()]
    body = [ln for ln in rows if f"a {kind} finding" in ln]
    assert body, f"the {kind} row did not render at all"
    assert "?" not in body[0].split("a " + kind)[0], f"{kind} renders as ?: {body[0][:60]!r}"


@pytest.mark.parametrize("kind", sorted(EMITTED))
def test_the_summary_line_names_every_kind(kind):
    # The pinned rule counts by kind too, and it reads the same table.
    line = strip(summary_line([{"kind": kind, "what": "x", "detail": "y"}], NOCOLOR, 120))
    assert KINDNAME[kind] in line, f"the rule cannot name {kind}: {line!r}"


# ---- the other half: a producer that reads a key nobody produces --------------------------------

HOST_KEYS = {"container", "cores", "load", "peak", "pid", "ram_avail", "ram_total", "rss", "swap",
             "swap_free", "swap_total", "threads", "uptime"}


def test_the_host_payload_still_has_the_keys_the_findings_read():
    # `hostinfo` is the only producer of this dict. If a key here disappears, a finding that reads
    # it goes quiet rather than failing - which is exactly what cpu_burn did for its whole life.
    #
    # Against our OWN pid, so the test needs no server and no container: some keys (uptime, rss)
    # only exist when there is a process to read, and passing None would assert a smaller contract
    # than the one the findings actually rely on.
    got = set(system.hostinfo(os.getpid(), None) or {})
    missing = HOST_KEYS - got
    assert not missing, f"hostinfo no longer produces {sorted(missing)}; findings read these"


def test_cpu_burn_fires_on_the_shape_it_was_written_for():
    # Five of 24 cores busy with a statement nobody is waiting for. This is the live measurement it
    # was written from, and it could not fire on it: the CPU figure is `tcpu`, a separate argument,
    # not something `host` carries.
    s = {"queries": [("active", "select 1", 8, 168_000, 168_000, "1265771991", "", "")]}
    host = {"cores": 24}
    got = snapshot.cpu_burn(s, host, tcpu=500.0)
    assert len(got) == 1, "the finding this was written for does not fire"
    assert got[0]["kind"] == "cpu"
    assert got[0]["cores"] == 24 and got[0]["cpu_percent_of_one_core"] == 500.0
    assert "1265771991" in got[0]["detail"]


def test_cpu_burn_needs_both_halves():
    # Busy is not a finding: a working server is busy. A long statement on an idle machine is
    # waiting for something, not burning. Only together do they mean cores are being spent on work
    # nobody is waiting for.
    old = {"queries": [("active", "select 1", 8, 168_000, 168_000, "7", "", "")]}
    new = {"queries": [("active", "select 1", 8, 5, 5, "7", "", "")]}
    host = {"cores": 24}
    assert snapshot.cpu_burn(old, host, tcpu=50.0) == [], "busy alone is not a finding"
    assert snapshot.cpu_burn(new, host, tcpu=500.0) == [], "a long statement alone is not either"
    assert snapshot.cpu_burn(old, host, tcpu=500.0), "together they are"


def test_a_finding_reads_nothing_it_was_not_given():
    # The general form of the cpu_burn bug: run every producer against EMPTY inputs. A producer that
    # raises is reading a key it assumed; a producer that returns [] has degraded properly.
    empty = {"queries": [], "settings": {}, "states": {}, "memtags": {}, "blocks": (0, 0, 0, 0),
             "size": 0, "wal": 0, "mem": 0, "memlimit": 0}
    for name, fn in (("long_running", lambda: snapshot.long_running(empty)),
                     ("checkpoint_waiting", lambda: snapshot.checkpoint_waiting(empty)),
                     ("cpu_burn", lambda: snapshot.cpu_burn(empty, {}, None)),
                     ("search_findings", lambda: snapshot.search_findings(None)),
                     ("setup_findings", lambda: snapshot.setup_findings([], None))):
        assert fn() == [], f"{name} invented a finding from nothing"


# ── decision trees, phase 1 ──────────────────────────────────────────────────────────────────────

REAL_CONVOY = [
    ("active", "WITH lex AS (SELECT id, BM25(", 68209, 199000, 199000, "1265771991", "", ""),
    ("active", "WITH lex AS (SELECT id, BM25(", 68209, 198910, 198910, "1711618162", "", ""),
    ("active", "FORCE CHECKPOINT", 16, 42564, 42564, "491336051", "", ""),
    ("active", "SELECT count(*) FROM ragflow_x", 44, 42564, 42564, "1558815683", "", ""),
    ("active", "CREATE INDEX IF NOT EXISTS idx_x", 260, 42563, 42563, "2119928374", "", ""),
    ("active", "CREATE INDEX IF NOT EXISTS idx_x", 260, 42563, 42563, "360567690", "", ""),
]


def test_a_convoy_is_recognised_by_agreeing_start_times():
    """The real one: four statements within 166 ms of a FORCE CHECKPOINT taking
    start_transaction_lock, then stuck 11.8 hours. There is no lock view on this server, so the
    only signal that these are one pile-up rather than four slow queries is that independent
    clients do not agree on a start time to the second."""
    from serenedash import snapshot as sn
    got = sn.convoy({"queries": REAL_CONVOY})
    assert len(got) == 1, "one finding per convoy, not one per member"
    f = got[0]
    assert set(f["pids"]) == {"491336051", "1558815683", "2119928374", "360567690"}
    # The two 55-hour spinners started 90s apart and 43 hours earlier: not part of this convoy.
    assert "1265771991" not in f["pids"]
    assert f["statement_kinds"][0] == "FORCE"


def test_the_convoy_finding_does_not_name_a_blocker():
    # There is no lock view. Saying "blocked by pid X" would be inference dressed as measurement -
    # the finding gives the set and the ordering and lets the reader draw the conclusion.
    from serenedash import snapshot as sn
    f = sn.convoy({"queries": REAL_CONVOY})[0]
    assert "blocked_by" not in f
    assert "cannot be named from SQL" in f["detail"]


def test_ordinary_traffic_is_not_a_convoy():
    from serenedash import snapshot as sn
    # Three statements at the same age but young: a burst, not a pile-up.
    young = [("active", f"select {i}", 9, 5, 5, str(i), "", "") for i in range(4)]
    assert sn.convoy({"queries": young}) == []
    # Three old statements that did NOT start together.
    apart = [("active", f"select {i}", 9, 3600 * i + 3600, 9999, str(i), "", "") for i in range(4)]
    assert sn.convoy({"queries": apart}) == []
    # Two is not a convoy.
    pair = [("active", f"select {i}", 9, 5000, 5000, str(i), "", "") for i in range(2)]
    assert sn.convoy({"queries": pair}) == []


def test_exposure_fires_on_any_deletion_not_on_a_share():
    """Twelve documents out of 14.6 million - 0.00008% - was the entire trigger. The existing
    'deleted documents not reclaimed' finding is about wasted space and has a 10% threshold; this
    one is about a hang and must have none."""
    from serenedash import snapshot as sn
    sr = {"available": True, "indexes": [
        {"relation_id": "2000801", "deleted_docs": 12, "num_docs": 14621592, "num_segments": 22},
        {"relation_id": "2000422", "deleted_docs": 0, "num_docs": 247665, "num_segments": 1}]}
    got = sn.bm25_exposure(sr)
    assert [f["relation_id"] for f in got] == ["2000801"], "a clean index must not be flagged"
    assert got[0]["deleted_docs"] == 12


def test_exposure_says_how_much_of_the_diagnosis_it_actually_has():
    # The hang needs three ingredients and only one is readable here: the index DDL is not
    # recoverable (pg_indexes.indexdef is empty, duckdb_indexes() drops the WITH clause) and nothing
    # reports query shapes. A finding claiming the other two would be asserting what it did not
    # measure - the failure this whole file exists to prevent.
    from serenedash import snapshot as sn
    f = sn.bm25_exposure({"available": True, "indexes": [
        {"relation_id": "1", "deleted_docs": 1, "num_docs": 10, "num_segments": 2}]})[0]
    assert f["ingredients_checked"] == 1 and f["ingredients_total"] == 3
    assert "not readable from this server" in f["detail"]
    assert "Exposure, not a fault" in f["detail"]
    assert "EXPLAIN" in f["fix"], "and it has to say how to check the ones it cannot"


def test_exposure_is_silent_when_the_metrics_are_unavailable():
    from serenedash import snapshot as sn
    assert sn.bm25_exposure(None) == []
    assert sn.bm25_exposure({"available": False, "reason": "sdb_metrics could not be read"}) == []


def test_a_spin_is_told_from_a_block_by_yielding_not_by_cpu():
    """CPU cannot separate them and neither can the thread count. A loop that never blocks never
    yields, so voluntary switches per cpu-second collapse toward zero - 13 on the 55-hour hang -
    while anything waiting on IO, a lock or a condvar reads in the thousands."""
    from serenedash import snapshot as sn
    spin = sn.spin_suspected({"volps": [12.0, 13.0, 13.0]}, 503)
    assert len(spin) == 1 and spin[0]["switches_per_cpu_s"] == 13.0
    assert spin[0]["cpu_cores_busy"] == 5.0
    assert sn.spin_suspected({"volps": [3000, 2800, 3100]}, 503) == [], "blocked work is not a spin"


def test_an_idle_server_is_not_a_spin():
    # Zero switches and zero CPU is a server doing nothing, which trips a switches-only rule.
    from serenedash import snapshot as sn
    assert sn.spin_suspected({"volps": [0, 0, 0]}, 4) == []


def test_one_sample_is_not_enough_to_call_a_spin():
    from serenedash import snapshot as sn
    assert sn.spin_suspected({"volps": [1.0, 2.0]}, 503) == [], "a tick can catch anything mid-yield"
    assert sn.spin_suspected({}, 503) == []
    assert sn.spin_suspected(None, 503) == []


def test_the_spin_finding_reports_a_shape_and_not_a_cause():
    # A tight loop doing useful arithmetic looks identical. Naming a cause here would be the same
    # error as the checkpoint finding's "the horizon keeps being re-pinned" - a plausible mechanism
    # asserted from a measurement that does not reach it.
    from serenedash import snapshot as sn
    f = sn.spin_suspected({"volps": [1.0, 1.0, 1.0]}, 200)[0]
    assert "SHAPE, NOT A CAUSE" in f["detail"]
    assert f["threshold_is_chosen"] is True
    assert "perf-snap" in f["fix"], "it has to name the measurement that would settle it"

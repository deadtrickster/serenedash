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

"""The first decision tree, executed.

Three outcomes look identical in `pg_stat_activity` - spin, blocked, genuinely slow - because
`state='active'` covers all three and this server has no wait events and no lock view. These tests
drive each branch with the evidence that actually occurred, so the tree is checked against the
incident rather than against itself.
"""
import pytest

from serenedash import mcp_server as m

# The real convoy: four statements within 166 ms of a FORCE CHECKPOINT, stuck 11.8 hours.
CONVOY = [
    ("active", "FORCE CHECKPOINT", 16, 42564, 42564, "491336051", "", ""),
    ("active", "SELECT count(*) FROM ragflow_x", 44, 42564, 42564, "1558815683", "", ""),
    ("active", "CREATE INDEX IF NOT EXISTS idx_x", 260, 42563, 42563, "2119928374", "", ""),
    ("active", "CREATE INDEX IF NOT EXISTS idx_x", 260, 42563, 42563, "360567690", "", ""),
]
LONE = [("active", "WITH lex AS (SELECT id, BM25(", 68209, 199000, 199000, "1265771991", "", "")]


@pytest.fixture
def evidence(monkeypatch):
    """Point the tool at fabricated /proc and pg_stat_activity."""
    def setup(rows, cpu, vol_delta):
        monkeypatch.setattr(m, "_sample", lambda **kw: ({"queries": rows}, None))
        monkeypatch.setattr(m.db, "full_queries", lambda _cfg: rows)
        monkeypatch.setattr(m.system, "host_pid", lambda _cfg: 4242)
        calls = {"n": 0}

        def hostinfo(_pid, _c, root="/proc"):
            calls["n"] += 1
            return {"pid": 4242, "vol_switches": 0 if calls["n"] == 1 else vol_delta}
        monkeypatch.setattr(m.system, "hostinfo", hostinfo)
        # (rows, total, cur, now) - second call carries the cpu and a 1s interval.
        seq = iter([([], 0.0, {}, 100.0), ([], cpu, {}, 101.0)])
        monkeypatch.setattr(m.system, "threads", lambda *a, **kw: next(seq))
        monkeypatch.setattr(m.time, "sleep", lambda _s: None)
    return setup


def test_a_convoy_is_reported_as_blocked_and_names_the_checkpoint(evidence):
    evidence(CONVOY, 5.0, 500)
    out = m.triage()
    assert out["verdict"] == "blocked - a convoy"
    assert out["confidence"] == "strong", "a FORCE CHECKPOINT among them makes it strong"
    assert "start_transaction_lock" in out["reasoning"]
    assert "cancel the FORCE CHECKPOINT" in out["next"]
    assert set(out["evidence"]["convoy_pids"]) == {r[5] for r in CONVOY}


def test_a_lone_statement_burning_cpu_without_yielding_is_a_spin(evidence):
    # 5 cores busy, 13 voluntary switches per cpu-second: the 55-hour hang.
    evidence(LONE, 503.0, 65)
    out = m.triage()
    assert out["verdict"] == "spinning"
    assert out["evidence"]["voluntary_switches_per_cpu_s"] < 50
    assert "perf-snap.sh" in out["next"], "it has to name what would settle it"


def test_busy_but_yielding_is_not_called_a_spin(evidence):
    # Same CPU, thousands of switches: work blocked on IO or a lock.
    evidence(LONE, 503.0, 40000)
    out = m.triage()
    assert out["verdict"] == "working, or slow - not a spin"
    assert out["next"].startswith("explain(")


def test_idle_but_old_is_reported_weakly_rather_than_guessed(evidence):
    evidence(LONE, 2.0, 10)
    out = m.triage()
    assert out["confidence"] == "weak"
    assert "no wait events and no lock view" in out["reasoning"]


def test_triage_never_reports_a_cause(evidence):
    # Every verdict is a shape. A tight loop doing useful arithmetic looks like a spin from here,
    # and the tree must not promote a shape into a diagnosis.
    for rows, cpu, vol in ((CONVOY, 5.0, 500), (LONE, 503.0, 65), (LONE, 503.0, 40000)):
        evidence(rows, cpu, vol)
        out = m.triage()
        assert "what_would_settle_it" in out, f"{out['verdict']} claims certainty it does not have"
        assert out["confidence"] in ("strong", "moderate", "weak")


def test_triage_terminates_nothing():
    """It reports; the operator decides. Asserted on the CALLS rather than on the text, because the
    docstring legitimately mentions pg_terminate_backend - to say that a statement whose loop never
    polls cannot be cancelled at all, which is the thing a caller most needs to know before
    reaching for it."""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(m.triage))
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            called.add(f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", ""))
    assert "terminate" not in called
    assert not {"apply_setting", "set_setting"} & called, "and it changes nothing either"

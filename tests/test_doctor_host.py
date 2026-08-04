"""The host preconditions in `doctor()`: RLIMIT_NOFILE, vm.max_map_count, allocator threads.

All three are spent by the workload rather than at startup, so nothing here can be checked by
running the server once and watching it come up. What these tests hold up is the part that is
checkable: the readers parse what /proc actually writes, the thresholds are the documented ones,
and a source that could not be read is reported as `info` and never as a pass. The last one is the
rule the rest of this dashboard keeps getting wrong - see `absence of evidence` in AGENTS.md.
"""
import math
import os
import resource

import pytest

from serenedash import symbols
from serenedash.symbols import MAP_COUNT_MIN, NOFILE_TARGET, doctor
from serenedash.system import rlimit_nofile, sysctl

LIMITS = (
    "Limit                     Soft Limit           Hard Limit           Units\n"
    "Max stack size            8388608              unlimited            bytes\n"
    "Max open files            {soft}               {hard}               files\n"
    "Max locked memory         8388608              8388608              bytes\n"
)


def fake_proc(tmp_path, pid, soft, hard):
    d = tmp_path / str(pid)
    d.mkdir()
    (d / "limits").write_text(LIMITS.format(soft=soft, hard=hard))
    return str(tmp_path)


# ── the readers ─────────────────────────────────────────────────────────────────────────────────

def test_nofile_matches_the_kernel_for_our_own_process():
    # Against getrlimit rather than against a fixture: the point of parsing /proc is that it reads
    # the same numbers a syscall would, for a process this user cannot prlimit.
    assert rlimit_nofile(os.getpid()) == resource.getrlimit(resource.RLIMIT_NOFILE)


def test_nofile_parses_soft_and_hard(tmp_path):
    root = fake_proc(tmp_path, 7, 65535, 524288)
    assert rlimit_nofile(7, root=root) == (65535, 524288)


def test_nofile_unlimited_is_infinite_not_zero(tmp_path):
    # "unlimited" satisfies any target. Parsed to 0 or dropped, it would read as the worst possible
    # limit and fire the loudest row on the healthiest box.
    root = fake_proc(tmp_path, 8, "unlimited", "unlimited")
    soft, hard = rlimit_nofile(8, root=root)
    assert soft == math.inf and hard == math.inf
    assert soft >= NOFILE_TARGET


def test_nofile_unreadable_is_none(tmp_path):
    assert rlimit_nofile(0) is None                       # no pid at all
    assert rlimit_nofile(9, root=str(tmp_path)) is None   # no such process
    d = tmp_path / "10"
    d.mkdir()
    (d / "limits").write_text("Limit  Soft Limit  Hard Limit  Units\n")
    assert rlimit_nofile(10, root=str(tmp_path)) is None  # a kernel that does not write the line


def test_sysctl_reads_an_int_and_refuses_anything_else(tmp_path):
    (tmp_path / "vm").mkdir()
    (tmp_path / "vm" / "max_map_count").write_text("1048576\n")
    assert sysctl("vm.max_map_count", root=str(tmp_path)) == 1048576
    assert sysctl("vm.nonesuch", root=str(tmp_path)) is None
    (tmp_path / "vm" / "junk").write_text("not a number\n")
    assert sysctl("vm.junk", root=str(tmp_path)) is None


def test_sysctl_reads_the_live_kernel():
    live = sysctl("vm.max_map_count")
    assert live is None or live > 0


# ── the rows ────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def rows(monkeypatch, tmp_path):
    """doctor() with every source stubbed. Returns {name: (status, detail, fix)}."""
    def run(pid=526418, lim=(65535, 524288), mmc=1048576, alloc="off", server=True):
        monkeypatch.setattr(symbols, "sample", lambda *a, **k: {"settings": {}} if server else None)
        monkeypatch.setattr(symbols, "host_pid", lambda cfg: pid)
        monkeypatch.setattr(symbols, "rlimit_nofile", lambda p: lim)
        monkeypatch.setattr(symbols, "sysctl", lambda n: mmc)
        monkeypatch.setattr(symbols, "query",
                            lambda *a, **k: [[[alloc]]] if alloc is not None else None)
        out, _fix = doctor({"container": "c", "port": "1", "target": "docker"}, str(tmp_path))
        return {r[1]: (r[0], r[2], r[3]) for r in out}
    return run


def test_this_box_fires_open_files_and_allocator(rows):
    # The measured state of the deployment this was written against: soft 65535 of a documented
    # 131072 with a hard limit of 524288, vm.max_map_count 1048576, allocator threads off on 24
    # cores. Two of the three fire, and the third passes without being dropped.
    r = rows()
    assert r["open files"][0] == "warn"
    assert "65535" in r["open files"][1] and "131072" in r["open files"][1]
    assert "524288" in r["open files"][1]
    assert r["memory maps"][0] == "ok"
    assert r["allocator"][0] == "warn"


def test_open_files_at_target_passes(rows):
    r = rows(lim=(NOFILE_TARGET, 524288))
    assert r["open files"][0] == "ok"
    assert r["open files"][2] == ""


def test_open_files_offers_prlimit_only_when_the_hard_limit_allows_it(rows):
    # A soft limit cannot go past the hard one, so printing prlimit against a hard limit below the
    # target would be a command that fails - and a fix that does not work is worse than no fix.
    raisable = rows(lim=(1024, 524288))["open files"][2]
    assert "prlimit" in raisable and "--nofile=131072:" in raisable
    stuck = rows(lim=(1024, 4096))["open files"][2]
    assert "prlimit" not in stuck and "--ulimit" in stuck


def test_unreadable_limits_is_info_not_ok(rows):
    for r in (rows(lim=None), rows(pid=None, lim=None)):
        assert r["open files"][0] == "info"
        assert "unchecked" in r["open files"][1]


def test_map_count_below_the_documented_minimum_fires(rows):
    r = rows(mmc=65530)
    assert r["memory maps"][0] == "warn"
    assert "65530" in r["memory maps"][1] and str(MAP_COUNT_MIN) in r["memory maps"][1]
    assert "kernel default" in r["memory maps"][1]
    assert f"sysctl -w vm.max_map_count={MAP_COUNT_MIN}" in r["memory maps"][2]


def test_map_count_without_a_pid_is_info_not_ok(rows):
    # No host pid means the server may not be on this machine, and the local sysctl then describes
    # the wrong kernel. Reporting the local value as a pass is the "absence of evidence" error.
    r = rows(pid=None, lim=None, mmc=1048576)
    assert r["memory maps"][0] == "info"
    r = rows(mmc=None)
    assert r["memory maps"][0] == "info"


def test_allocator_on_passes_and_off_fires(rows):
    assert rows(alloc="true")["allocator"][0] == "ok"
    assert rows(alloc="on")["allocator"][0] == "ok"
    off = rows(alloc="off")
    assert off["allocator"][0] == "warn"
    assert "allocator_background_threads=true" in off["allocator"][2]


def test_allocator_unavailable_is_info_not_ok(rows):
    assert rows(alloc=None)["allocator"][0] == "info"
    # No server at all: the setting cannot be read, and the row must not claim the default either.
    assert rows(server=False, alloc=None)["allocator"][0] == "info"


def test_no_server_still_reports_the_host_rows(rows):
    # Degrade by panel: /proc does not need a connection, so losing the server must not take the
    # two rows that are pure host reads with it.
    r = rows(server=False, alloc=None)
    assert r["open files"][0] == "warn"
    assert r["memory maps"][0] == "ok"


def test_every_row_keeps_the_shape_the_frame_renders(rows):
    for st, detail, fix in rows().values():
        assert st in ("ok", "warn", "fail", "info")
        assert isinstance(detail, str) and detail
        assert isinstance(fix, str)


def test_the_finding_survives_the_renderer(rows):
    # The doctor frame packs a wrapped detail into ONE list element and the next element is written
    # to an absolute row, so every continuation line is overwritten - measured in a pty at 45x120
    # and 30x80. Only the first wrapped line reaches the screen, and at width 80 that is 54 columns
    # (doctor_frame's max(30, W - 26)), so the numbers have to be inside it.
    r = rows()
    for name in ("open files", "memory maps", "allocator"):
        first = r[name][1][:54]
        assert any(ch.isdigit() for ch in first), f"{name}: {first!r} carries no measurement"

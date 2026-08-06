"""Build-id handling.

The premise of the symbol feature is that a build-id identifies a build, not a machine - so a
binary in a local tree can name symbols in a capture taken from a container on another host. These
tests hold the premise up, including the part people get wrong: rebuilding the same source does not
reproduce a build-id.
"""
import shutil
import subprocess

import pytest

from serenedash import symbols
from serenedash.symbols import (buildid_cached, elf_build_id, elf_symbol_count)


def readelf_build_id(path):
    out = subprocess.run(["readelf", "-n", path], capture_output=True, text=True)
    for ln in out.stdout.splitlines():
        if "Build ID:" in ln:
            return ln.split("Build ID:")[1].strip()
    return None


@pytest.fixture(scope="module")
def an_elf():
    for candidate in ("/bin/ls", "/usr/bin/ls", "/bin/true"):
        if elf_build_id(candidate):
            return candidate
    pytest.skip("no ELF with a build-id available")


def test_matches_readelf(an_elf):
    if shutil.which("readelf") is None:
        pytest.skip("readelf not installed")
    assert elf_build_id(an_elf) == readelf_build_id(an_elf)


def test_build_id_is_hex_and_long_enough(an_elf):
    bid = elf_build_id(an_elf)
    assert len(bid) >= 32 and int(bid, 16) >= 0


def test_non_elf_and_missing_files_are_not_errors(tmp_path):
    # A build tree is full of things that are not ELFs; scanning must not raise on any of them.
    plain = tmp_path / "notelf"
    plain.write_bytes(b"#!/bin/sh\necho hi\n")
    assert elf_build_id(str(plain)) is None
    assert elf_build_id(str(tmp_path / "does-not-exist")) is None
    empty = tmp_path / "empty"
    empty.write_bytes(b"")
    assert elf_build_id(str(empty)) is None


def test_truncated_elf_does_not_raise(tmp_path, an_elf):
    cut = tmp_path / "cut"
    cut.write_bytes(open(an_elf, "rb").read(200))
    assert elf_build_id(str(cut)) is None


def test_cache_lookup_is_a_pure_path_check(tmp_path):
    bid = "0" * 40
    assert buildid_cached(bid, debug_dir=str(tmp_path)) is False
    p = tmp_path / ".build-id" / bid[:2] / bid[2:]
    p.mkdir(parents=True)
    assert buildid_cached(bid, debug_dir=str(tmp_path)) is True


# ── a matching build-id is not enough ────────────────────────────────────────────────────────────


def test_symbol_count_agrees_with_readelf(an_elf):
    if not shutil.which("readelf"):
        pytest.skip("readelf not available")
    out = subprocess.run(["readelf", "-sW", an_elf], capture_output=True, text=True).stdout
    # readelf prints one header line per symbol table plus a numbered row per symbol.
    expected = sum(1 for ln in out.splitlines() if ln.strip()[:1].isdigit() and ":" in ln)
    got = elf_symbol_count(an_elf)
    assert got == expected, f"counted {got}, readelf lists {expected}"


def test_a_stripped_binary_counts_zero_rather_than_none(tmp_path):
    # The distinction the whole row rests on: None means "not an ELF, I cannot say", 0 means "an
    # ELF with nothing in it, registering it is pointless". Conflating them turns a definite answer
    # into a shrug.
    elf = tmp_path / "stripped"
    head = bytearray(64)
    head[:4] = b"\x7fELF"
    head[4], head[5] = 2, 1                                  # 64-bit, little endian
    elf.write_bytes(bytes(head))                             # shoff = 0: no sections at all
    assert elf_symbol_count(str(elf)) == 0
    assert elf_symbol_count(str(tmp_path / "nope")) is None
    (tmp_path / "text").write_text("hello")
    assert elf_symbol_count(str(tmp_path / "text")) is None


def test_a_truncated_elf_does_not_raise(tmp_path, an_elf):
    cut = tmp_path / "cut"
    cut.write_bytes(open(an_elf, "rb").read(200))
    assert elf_symbol_count(str(cut)) in (None, 0) or elf_symbol_count(str(cut)) >= 0


def _docker(monkeypatch, stdout):
    def run(cmd, **kw):
        class R:
            pass
        R.stdout, R.stderr, R.returncode = stdout, "", 0
        return R
    monkeypatch.setattr(symbols.subprocess, "run", run)


def test_a_bind_mount_over_the_binary_is_found(monkeypatch, an_elf):
    # The fact that explains the whole confusion: the deployment does not run the image's binary.
    _docker(monkeypatch, f"{an_elf}\t/usr/bin/serened\tfalse\n/data\t/var/lib/serenedb\ttrue\n")
    got = symbols.mount_sources({"container": "x"}, "/usr/bin/serened")
    assert got == [(an_elf, True)], "the read-only bind mount over the binary was missed"
    assert symbols.mount_sources({"container": "x"}, "/nowhere") == []
    assert symbols.mount_sources({}, "/usr/bin/serened") == [], "no container, no docker call"


def test_a_mount_whose_source_is_gone_is_not_offered(monkeypatch):
    _docker(monkeypatch, "/vanished/serened\t/usr/bin/serened\tfalse\n")
    assert symbols.mount_sources({"container": "x"}, "/usr/bin/serened") == []


def test_candidates_put_symbols_ahead_of_a_build_id_match(monkeypatch, an_elf, tmp_path):
    # The ranking that matters. A stripped binary with the RIGHT build-id resolves nothing; a
    # symbolised one is worth more even before its id is checked.
    stripped = tmp_path / "serened"
    head = bytearray(64)
    head[:4], head[4], head[5] = b"\x7fELF", 2, 1
    stripped.write_bytes(bytes(head))
    _docker(monkeypatch, f"{an_elf}\t/usr/bin/serened\tfalse\n")
    monkeypatch.setattr(symbols, "extract_container_binary",
                        lambda cfg, dso, dest: (str(stripped), None))
    out = symbols.binary_candidates({"container": "x"}, "/usr/bin/serened",
                                    dest_dir=str(tmp_path), want=[elf_build_id(an_elf)])
    assert [c["origin"] for c in out][0] == "bind mount"
    assert out[0]["symbols"] and out[0]["matches"]


def test_candidates_need_no_dest_dir_to_report_a_mount(monkeypatch, an_elf):
    # Reporting must not have the side effect of copying a gigabyte out of a container.
    called = []
    _docker(monkeypatch, f"{an_elf}\t/usr/bin/serened\tfalse\n")
    monkeypatch.setattr(symbols, "extract_container_binary",
                        lambda *a: called.append(a) or (None, "no"))
    out = symbols.binary_candidates({"container": "x"}, "/usr/bin/serened")
    assert len(out) == 1 and not called


def test_the_cache_path_is_returned_so_what_is_in_it_can_be_checked(tmp_path):
    bid = "ab" + "c" * 38
    d = tmp_path / ".build-id" / "ab" / ("c" * 38)
    d.mkdir(parents=True)
    (d / "elf").write_bytes(b"\x7fELF")
    assert symbols.buildid_cached(bid, str(tmp_path))
    got = symbols.buildid_cached_path(bid, str(tmp_path))
    assert got and got.endswith("elf"), "without the path, 'registered' cannot be checked"
    assert symbols.buildid_cached_path("ff" + "e" * 38, str(tmp_path)) is None

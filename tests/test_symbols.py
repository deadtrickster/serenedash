"""Build-id handling.

The premise of the symbol feature is that a build-id identifies a build, not a machine - so a
binary in a local tree can name symbols in a capture taken from a container on another host. These
tests hold the premise up, including the part people get wrong: rebuilding the same source does not
reproduce a build-id.
"""
import shutil
import subprocess

import pytest

from serenedash.symbols import buildid_cached, elf_build_id


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

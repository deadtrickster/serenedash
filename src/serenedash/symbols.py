"""serenedash.symbols"""
import math
import os
import shutil
import subprocess

from .db import query, sample
from .system import host_pid, rlimit_nofile, sysctl

# Documented host preconditions. Neither number is a guess: NOFILE_TARGET is the documented target
# for a server whose text index holds a file per segment (this box runs 16 of them over 54.1 GB),
# and MAP_COUNT_MIN is the documented minimum because IResearch memory-maps every one of those
# segments - the kernel default of 65530 is below it.
NOFILE_TARGET = 131072
MAP_COUNT_MIN = 262144


def elf_build_id(path):
    """The GNU build-id of an ELF file, or None. Parsed here rather than shelled out to readelf.

    `readelf` is not always installed, `file` reports it inconsistently across versions, and both
    cost a process per candidate binary while scanning a build tree. The note is four fields at a
    known offset; parsing it directly keeps this stdlib-only and turns a directory scan into reads.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(64)
            if head[:4] != b"\x7fELF":
                return None
            wide = head[4] == 2                      # EI_CLASS: 2 is 64-bit
            end = "little" if head[5] == 1 else "big"

            def num(off, size):
                return int.from_bytes(head[off:off + size], end)

            phoff = num(0x20, 8) if wide else num(0x1c, 4)
            phentsize = num(0x36, 2) if wide else num(0x2a, 2)
            phnum = num(0x38, 2) if wide else num(0x2c, 2)
            f.seek(phoff)
            phdrs = f.read(phentsize * phnum)
            for i in range(phnum):
                p = phdrs[i * phentsize:(i + 1) * phentsize]
                if int.from_bytes(p[0:4], end) != 4:  # PT_NOTE
                    continue
                off = int.from_bytes(p[8:16], end) if wide else int.from_bytes(p[4:8], end)
                size = int.from_bytes(p[32:40], end) if wide else int.from_bytes(p[16:20], end)
                f.seek(off)
                note = f.read(size)
                j = 0
                while j + 12 <= len(note):
                    nsz = int.from_bytes(note[j:j + 4], end)
                    dsz = int.from_bytes(note[j + 4:j + 8], end)
                    typ = int.from_bytes(note[j + 8:j + 12], end)
                    name = note[j + 12:j + 12 + nsz].rstrip(b"\0")
                    dpos = j + 12 + (nsz + 3 & ~3)
                    if typ == 3 and name == b"GNU":   # NT_GNU_BUILD_ID
                        return note[dpos:dpos + dsz].hex()
                    j = dpos + (dsz + 3 & ~3)
    except (OSError, ValueError, IndexError):
        return None
    return None


def capture_build_ids(perf_file):
    """(build_id, dso path) the capture references, biggest-looking user binary first.

    Kernel and vdso entries are dropped: they are never what a build tree can supply, and leaving
    them in makes "no local binary matches" look like a search failure rather than the expected
    answer for [kernel.kallsyms].
    """
    try:
        o = subprocess.run(["perf", "buildid-list", "-i", perf_file],
                           capture_output=True, text=True, timeout=60)
    except Exception:                                           # noqa: BLE001
        return []
    out = []
    for ln in o.stdout.splitlines():
        parts = ln.split(None, 1)
        if len(parts) == 2 and len(parts[0]) >= 32 and not parts[1].startswith(("[", "/tmp/perf-")):
            out.append((parts[0], parts[1].strip()))
    return out


def buildid_cached(bid, debug_dir=None):
    """Whether perf's build-id cache can already resolve this build for the current user."""
    root = debug_dir or os.environ.get("PERF_BUILDID_DIR", os.path.expanduser("~/.debug"))
    p = os.path.join(root, ".build-id", bid[:2], bid[2:])
    return os.path.exists(p) or os.path.exists(p + "/elf")


def symbol_sources(want, paths, limit=400):
    """Local ELFs whose build-id is one the capture wants. {build_id: path}.

    `want` comes from the capture, `paths` from config. The point of matching on build-id rather
    than on filename or version string is that it cannot produce a wrong answer: a match IS the
    build that produced the capture, even if the file is named differently and lives on a different
    machine from the server.
    """
    found, seen = {}, 0
    for base in paths or []:
        base = os.path.expanduser(base)
        for dirpath, _, names in os.walk(base):
            for n in names:
                if seen >= limit:
                    return found
                p = os.path.join(dirpath, n)
                try:
                    if not os.path.isfile(p) or os.path.getsize(p) < 4096:
                        continue
                except OSError:
                    continue
                seen += 1
                bid = elf_build_id(p)
                if bid and bid in want and bid not in found:
                    found[bid] = p
    return found


def near_misses(want_paths, paths, limit=400):
    """Local ELFs named like the ones the capture wants, whose build-id does NOT match.

    This exists to answer the most expensive wrong guess in the whole feature: "I'll rebuild the
    same version and point it at that". A build-id is a hash of the build, not of the source — three
    builds of this project from one tree produced three different ids. A rebuild will never resolve
    someone else's capture, and without saying so the failure looks like a broken search rather than
    a category error.
    """
    wanted = {os.path.basename(p) for p in want_paths}
    out, seen = [], 0
    for base in paths or []:
        for dirpath, _, names in os.walk(os.path.expanduser(base)):
            for n in names:
                if n not in wanted or seen >= limit:
                    continue
                seen += 1
                bid = elf_build_id(os.path.join(dirpath, n))
                if bid:
                    out.append((os.path.join(dirpath, n), bid))
    return out


def extract_container_binary(cfg, dso_path, dest_dir):
    """Copy the binary out of the container. Needs docker, not root.

    The exact binary that produced the capture, by definition — no build tree to find and no version
    to match. perf cannot use it in place: it reaches a container binary only through
    /proc/<pid>/root, which is root-only, and that asymmetry is the whole reason an unprivileged
    dashboard shows hex where perf-snap shows names. `docker cp` steps around it.
    """
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, os.path.basename(dso_path))
    try:
        o = subprocess.run(["docker", "cp", f"{cfg['container']}:{dso_path}", dest],
                           capture_output=True, text=True, timeout=900)
        if o.returncode != 0:
            return None, (o.stderr or o.stdout).strip()
        return dest, None
    except Exception as e:                                      # noqa: BLE001
        return None, str(e)


def register_symbols(path):
    """`perf buildid-cache --add`. Symlinks into ~/.debug — it does not copy the binary."""
    try:
        o = subprocess.run(["perf", "buildid-cache", "--add", path],
                           capture_output=True, text=True, timeout=300)
        return o.returncode == 0, (o.stderr or o.stdout).strip() or "registered"
    except Exception as e:                                      # noqa: BLE001
        return False, str(e)


def _count(v):
    return "unlimited" if v == math.inf else f"{int(v)}"


def doctor(cfg, perf_dir):
    """Every precondition for a full picture, each with what it costs you and how to fix it.

    Written because the failure mode here is silent and looks like a bug: a capture exists, the
    panel draws, and the symbols are hex. Nothing is broken — the reader is unprivileged — but
    nothing says so either. Each row below reports what is missing, what you lose without it, and
    the exact command, so the answer to "why don't I see names" is one keypress away rather than an
    afternoon.

    Returns rows of (status, name, detail, fix) where status is ok / warn / fail / info.
    """
    rows = []

    def have(prog):
        return shutil.which(prog) is not None

    s = sample(cfg, query_head=1)
    rows.append(("ok" if s else "fail", "server",
                 f"{cfg['container']}:{cfg['port']}" + ("" if s else " unreachable"),
                 "" if s else "check the container is running, and the password "
                              "(--print-config shows where yours came from)"))

    pid = host_pid(cfg)
    rows.append(("ok" if pid else "warn", "host pid",
                 f"serened is pid {pid} on the host" if pid else
                 "cannot resolve the container's pid - the threads panel needs it",
                 "" if pid else "docker inspect must be usable by this user"))

    # ── host preconditions ──────────────────────────────────────────────────────────────────────
    #
    # None of these three stops the server from starting, and none of them is visible in a healthy
    # hour. They are spent by the workload: file descriptors by segments, mappings by segments,
    # allocator work by whatever thread happens to be allocating. The failure arrives under load,
    # with a message that names the symptom ("Too many open files", ENOMEM from mmap) and not the
    # limit, which is the shape this whole view exists for.
    #
    # A source that cannot be read is reported as info, not as a pass. `absence of evidence` in
    # AGENTS.md: no pid and no /proc are "could not check", and printing `ok` off them would be a
    # sentence the data does not support.
    #
    # Details lead with the measurement and the threshold, and the reasoning goes in the fix. The
    # frame packs a wrapped detail into ONE list element while the next element is written to an
    # absolute row, so every continuation line is overwritten before it is read - checked in a pty
    # at 80, 100 and 120 columns, where only the first wrapped line of each detail reached the
    # screen. The fix lines are separate elements and survive intact. Until that is fixed in
    # views.py, a detail whose first 54 columns (max(30, 80 - 26), the narrowest wrap) do not carry
    # the number says nothing at all.
    lim = rlimit_nofile(pid)
    if lim is None:
        rows.append(("info", "open files",
                     f"could not read /proc/{pid}/limits - unchecked, which is not the same as "
                     f"fine" if pid else
                     "no host pid, so RLIMIT_NOFILE is unchecked - which is not the same as fine",
                     ""))
    else:
        soft, hard = lim
        if soft >= NOFILE_TARGET:
            rows.append(("ok", "open files",
                         f"soft {_count(soft)} of a documented {NOFILE_TARGET}, hard "
                         f"{_count(hard)}", ""))
        else:
            raisable = hard >= NOFILE_TARGET
            rows.append(("warn", "open files",
                         f"soft {_count(soft)} of a documented {NOFILE_TARGET}, hard "
                         f"{_count(hard)}. A descriptor per index segment, so it is the growing "
                         f"index that spends this - and it arrives as 'Too many open files' under "
                         f"load, hours after a clean start",
                         (f"sudo prlimit --pid {pid} --nofile={NOFILE_TARGET}: raises it on the "
                          f"running process - the trailing colon leaves the hard limit alone. "
                          if raisable else
                          f"the hard limit is {_count(hard)}, itself under {NOFILE_TARGET}, and a "
                          f"process cannot raise its own. ")
                         + f"The server sets its own soft limit to 65535 at startup (or the hard "
                           f"limit, if that is lower), so it has to be given a bigger one to keep: "
                           f"docker run --ulimit nofile={NOFILE_TARGET}:{NOFILE_TARGET}, or "
                           f"LimitNOFILE={NOFILE_TARGET} in the systemd unit"))

    # Only meaningful when the server is on this machine: vm.max_map_count is not namespaced, so a
    # container shares the host's value, but a remote target has no pid here and the local sysctl
    # would then describe the wrong kernel.
    mmc = sysctl("vm.max_map_count") if pid else None
    if mmc is None:
        rows.append(("info", "memory maps",
                     "could not read /proc/sys/vm/max_map_count" if pid else
                     "no host pid, so a local vm.max_map_count would describe this kernel and not "
                     "necessarily the server's - unchecked", ""))
    elif mmc >= MAP_COUNT_MIN:
        rows.append(("ok", "memory maps",
                     f"vm.max_map_count={mmc}, documented minimum {MAP_COUNT_MIN}", ""))
    else:
        rows.append(("warn", "memory maps",
                     f"vm.max_map_count={mmc} against a documented minimum of {MAP_COUNT_MIN}"
                     + (" (the kernel default)" if mmc == 65530 else "")
                     + ". IResearch memory-maps every index segment, so mmap starts failing as the "
                       "index grows - under load, not at startup",
                     f"sudo sysctl -w vm.max_map_count={MAP_COUNT_MIN}, and the same line in "
                     f"/etc/sysctl.d/ to survive a reboot. It goes on the HOST: docker will not "
                     f"take it as a --sysctl, because it is not namespaced - which is also why the "
                     f"container shares whatever the host has"))

    # One extra round trip, and only when the server answered. `sample()` fetches the settings the
    # HAZARDS table has an opinion about and this is not one of them, and doctor() is off the redraw
    # path - the TUI caches its rows and rebuilds them on the data tick, not per keypress.
    b = query(cfg, ["select value from duckdb_settings() "
                    "where name = 'allocator_background_threads'"]) if s else None
    alloc = b[0][0][0] if b and b[0] and b[0][0] else None
    cores = os.cpu_count() or 0
    if alloc is None:
        rows.append(("info", "allocator",
                     "duckdb_settings() has no allocator_background_threads row - unchecked" if s
                     else "no connection, so allocator_background_threads is unchecked - not that "
                          "it is at its default", ""))
    elif alloc.lower() in ("true", "on", "1", "yes"):
        rows.append(("ok", "allocator",
                     f"background threads on - jemalloc purges asynchronously, off the thread that "
                     f"allocated ({cores} cores)", ""))
    else:
        rows.append(("warn", "allocator",
                     f"background threads {alloc} (the default) on {cores} cores - jemalloc then "
                     f"purges dirty pages in the foreground, inside whichever query allocated "
                     f"them, rather than on a thread of its own",
                     "SET GLOBAL allocator_background_threads=true (the c view sets it in place), "
                     "and the server's config so it survives a restart. Documented as worth "
                     "enabling on many-core CPUs"))

    rows.append(("ok" if have("perf") else "warn", "perf",
                 "installed" if have("perf") else "not installed - no profile or call graph",
                 "" if have("perf") else "install linux-tools for your kernel"))

    try:
        caps = sorted((os.path.join(d, f) for d, _, fs in os.walk(perf_dir) for f in fs
                       if f.endswith(".data")), key=os.path.getmtime, reverse=True)
    except OSError:
        caps = []
    rows.append(("ok" if caps else "warn", "captures",
                 f"{len(caps)} in {perf_dir}" if caps else f"none in {perf_dir}",
                 "" if caps else f"sudo ./perf-snap.sh --container {cfg['container']}"))

    fix = None                       # ("register", path) or ("extract", dso) - what `r` will do
    if caps:
        want = capture_build_ids(caps[0])
        missing = [(b, p) for b, p in want if not buildid_cached(b)]
        if not want:
            rows.append(("warn", "build-ids", "the newest capture names no user binary", ""))
        elif not missing:
            rows.append(("ok", "symbols",
                         f"{len(want)} build-id(s) registered - names will resolve", ""))
        else:
            found = symbol_sources({b for b, _ in missing}, cfg.get("symbol_paths"))
            if found:
                bid, path = next(iter(found.items()))
                fix = ("register", path)
                rows.append(("warn", "symbols",
                             f"build {bid[:12]}… is not registered, but a matching binary is here: "
                             f"{path}",
                             "press r to register it - a symlink into ~/.debug, no copy, and every "
                             "capture from this build resolves afterwards"))
            elif cfg.get("target", "docker") == "docker":
                # The container has the exact binary and docker can read it out without root.
                dso = missing[0][1]
                fix = ("extract", dso)
                rows.append(("warn", "symbols",
                             f"build {missing[0][0][:12]}… is not registered and no local build "
                             f"matches, but the container has the exact binary at {dso}",
                             "press r to copy it out with docker cp and register it. It is the "
                             "binary that produced this capture, so nothing has to match"))
            else:
                misses = near_misses([p for _, p in missing], cfg.get("symbol_paths"))
                if misses:
                    p0, b0 = misses[0]
                    rows.append(("fail", "symbols",
                                 f"{p0} is the right name but build {b0[:12]}…, and the capture "
                                 f"wants {missing[0][0][:12]}…. A build-id is a hash of the build, "
                                 f"not of the source - rebuilding the same version does not "
                                 f"reproduce one",
                                 "you need the binary that produced the capture, its distro "
                                 "debuginfo (same build-id), or debuginfod - not a local rebuild"))
                elif os.environ.get("DEBUGINFOD_URLS"):
                    rows.append(("info", "symbols",
                                 f"no local match for {missing[0][0][:12]}…, but DEBUGINFOD_URLS is "
                                 f"set - perf will try to fetch debuginfo for distro builds",
                                 "for a self-built or vendor serened, copy that exact binary here "
                                 "and add its directory to symbol_paths"))
                else:
                    rows.append(("fail", "symbols",
                                 f"nothing here can name {', '.join(p for _, p in missing[:2])}. "
                                 f"perf resolves a container binary only for a reader that can "
                                 f"reach it through /proc/<pid>/root, ie root",
                                 "copy the binary that produced the capture onto this machine and "
                                 "add its directory to symbol_paths, or set DEBUGINFOD_URLS for a "
                                 "distro build. A build-id match is a build match - the binary does "
                                 "not have to come from the machine the server runs on"))

    try:
        with open("/proc/sys/kernel/kptr_restrict") as f:
            kptr = f.read().strip()
    except OSError:
        kptr = "?"
    rows.append(("ok" if kptr == "0" else "info", "kernel symbols",
                 "resolving" if kptr == "0" else
                 f"kptr_restrict={kptr}, so kernel frames stay as addresses. The engine split "
                 f"still works; only the names are missing",
                 "" if kptr == "0" else "sudo sysctl kernel.kptr_restrict=0"))

    try:
        with open("/proc/sys/kernel/perf_event_paranoid") as f:
            para = f.read().strip()
    except OSError:
        para = "?"
    rows.append(("info", "recording",
                 f"perf_event_paranoid={para} - the dashboard reads captures, it does not record. "
                 f"perf-snap.sh does that under sudo",
                 ""))
    return rows, fix

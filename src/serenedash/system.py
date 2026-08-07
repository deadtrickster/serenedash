"""serenedash.system"""
import math
import os
import subprocess
import time



SLOW_EVERY = 12


def rlimit_nofile(pid, root="/proc"):
    """(soft, hard) RLIMIT_NOFILE for a pid, read from /proc/<pid>/limits. None if it cannot be read.

    None means "could not check" and never "fine": a pid that has gone, a /proc this user cannot
    reach, and a kernel that spells the line differently all land here, and a caller that reports
    any of them as a pass is making the same error as `sleeping` on a thread at 60%.

    Read from /proc rather than through `resource.prlimit`, which needs CAP_SYS_RESOURCE for another
    process - measured on this box: prlimit(526418, RLIMIT_NOFILE) raises EPERM for an unprivileged
    reader while /proc/526418/limits is mode 0444 and readable by anyone. Same asymmetry as perf and
    /proc/<pid>/root, and the same workaround: read what is world-readable.

    `unlimited` comes back as math.inf so a caller can compare against a target without a case for
    it.
    """
    if not pid:
        return None
    try:
        with open(f"{root}/{pid}/limits") as f:
            for ln in f:
                if ln.startswith("Max open files"):
                    p = ln.split()
                    return tuple(math.inf if v == "unlimited" else int(v) for v in (p[3], p[4]))
    except (OSError, ValueError, IndexError):
        return None
    return None                          # no such line: an unexpected kernel, not a passing limit


def sysctl(name, root="/proc/sys"):
    """A /proc/sys value as an int, or None if it cannot be read or is not a plain number.

    THIS host's /proc/sys on purpose, even when the server is in a container: vm.max_map_count is
    not namespaced - which is why docker refuses `--sysctl vm.max_map_count`, its allowlist being
    kernel.shm*/msg*/sem, fs.mqueue.* and net.* - so the container shares the host's value. Only
    valid when the server runs on this machine, which the caller establishes with a host pid.
    """
    try:
        with open(os.path.join(root, *name.split("."))) as f:
            return int(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def hostinfo(pid, container):
    """The facts every other panel quietly assumes, and none of them state.

    A thread at 100% is 100% of ONE core — meaningless without the core count. `threads 24` is the
    store's setting, not how many OS threads exist (107 here, because pools, jemalloc and the wire
    layer all add their own). RSS is the number the OOM killer reads, which is not the number
    `duckdb_memory()` reports. All of it is free: three small reads under /proc.
    """
    d = {"container": container, "pid": pid, "cores": os.cpu_count() or 0, "load": [], "rss": 0,
         "threads": 0, "peak": 0, "swap": 0, "ram_total": 0}
    try:
        with open("/proc/loadavg") as f:
            d["load"] = f.read().split()[:3]
    except OSError:
        pass
    # The machine's own RAM, which is what makes memory_limit readable: 100.2 GiB is DuckDB's
    # default 80% of total, and whether that is generous or reckless depends entirely on what else
    # is running on the box.
    try:
        want = {"MemTotal:": "ram_total", "MemAvailable:": "ram_avail",
                "SwapTotal:": "swap_total", "SwapFree:": "swap_free"}
        with open("/proc/meminfo") as f:
            for ln in f:
                k = ln.split(None, 1)[0] if ln else ""
                if k in want:
                    d[want[k]] = int(ln.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    if pid:
        try:
            d["threads"] = len(os.listdir(f"/proc/{pid}/task"))
        except OSError:
            pass
        # RSS, its high-water mark, and swap. Swap is the one that changes a reading: a store that
        # reports 33.7 GB of buffers while only 7.3 GB is resident has not lost them, it has had
        # them paged out — 29.7 GB of them, measured here — and every touch of that memory is now a
        # disk read that no query plan, cache-hit ratio or memory_limit will show you.
        try:
            want = {"VmRSS:": "rss", "VmHWM:": "peak", "VmSwap:": "swap"}
            with open(f"/proc/{pid}/status") as f:
                for ln in f:
                    k = ln.split(None, 1)[0] if ln else ""
                    if k in want:
                        d[want[k]] = int(ln.split()[1]) * 1024
        except (OSError, ValueError, IndexError):
            pass
        # Field 22 of /proc/<pid>/stat is start time in ticks since boot; /proc/uptime is the other
        # half of the subtraction. Worth having: "the WAL has not checkpointed in two days" reads
        # differently against a process that started an hour ago.
        try:
            with open(f"/proc/{pid}/stat") as f:
                start = int(f.read().rsplit(") ", 1)[1].split()[19])
            with open("/proc/uptime") as f:
                d["uptime"] = max(0.0, float(f.read().split()[0]) - start / os.sysconf("SC_CLK_TCK"))
        except (OSError, ValueError, IndexError):
            pass
    return d


def du(cfg, path):
    try:
        argv = (["docker", "exec", cfg["container"]] if cfg.get("target", "docker") == "docker"
                else [] if cfg.get("target") == "local" else None)
        if argv is None:
            return None                      # remote: no filesystem to measure from here
        o = subprocess.run(argv + ["du", "-sm", path],
                           capture_output=True, text=True, timeout=240)
        return int(o.stdout.split()[0]) * 2**20
    except Exception:                                           # noqa: BLE001
        return None


def temp_files(cfg, temp_dir):
    """(mtime, size) for every file in the temp directory.

    Enough to split the directory into what a live query is using and what a dead one left behind,
    which are two different numbers that must not be added together. DuckDB deletes duckdb_temp_*
    files in TemporaryDirectoryHandle's DESTRUCTOR and nowhere else — there is no sweep at startup —
    so anything a killed server leaves survives every later run, forever. A file older than the
    process cannot belong to it: 73 GB sat in this one, two hours older than the serened holding it.

    Cheap because the directory holds tens of files, not thousands — one stat per file, on the du
    timer, not the refresh timer.
    """
    try:
        o = subprocess.run(
            (["docker", "exec", cfg["container"]] if cfg.get("target", "docker") == "docker"
             else []) + ["sh", "-c",
             f"find {temp_dir} -maxdepth 1 -type f -printf '%T@ %s %f\\n' 2>/dev/null"],
            capture_output=True, text=True, timeout=60)
        out = []
        for ln in o.stdout.split("\n"):
            p = ln.split(None, 2)
            if len(p) == 3:
                out.append((float(p[0]), int(p[1]), p[2]))
        return out
    except Exception:                                           # noqa: BLE001
        return []


def slow(cfg, data_dir, prev=None):
    """Directory sizes, plus how the temp directory MOVED since the last measurement.

    Size is a level; spilling is an activity. A killed query leaves its temp files behind, so a
    directory that has sat at 72 GB since yesterday reads identically to one being written to right
    now — and the level alone was reported as "spilling", which is wrong in exactly the way a
    momentary thread state reported as an interval verdict is wrong. Only the delta can say it.
    """
    cur = {
        "index": du(cfg, f"{data_dir}/engine_search"),
        "duck": du(cfg, f"{data_dir}/engine_duckdb"),
        "temp": du(cfg, f"{data_dir}/tmp"),
        "total": du(cfg, data_dir),
    }
    cur["temp_files"] = temp_files(cfg, f"{data_dir}/tmp")
    was = (prev or {}).get("temp")
    cur["temp_d"] = None if was is None or cur["temp"] is None else cur["temp"] - was
    # How long that delta actually covers. "since last check" left the reader to work out that a
    # check is SLOW_EVERY ticks, which is 60s at the default -n 5 and 24s at -n 2.
    cur["t"] = time.time()
    cur["dt"] = cur["t"] - prev["t"] if prev and prev.get("t") else None
    return cur


def host_pid(cfg):
    """The serened pid as the HOST sees it. /proc is the only way to per-thread detail — the server
    exposes sessions, not threads, and a spin lives in a thread that owns no session."""
    try:
        tgt = cfg.get("target", "docker")
        if tgt == "remote":
            return None                      # no /proc to read across the wire
        argv = (["docker", "inspect", "-f", "{{.State.Pid}}", cfg["container"]] if tgt == "docker"
                else ["pgrep", "-x", "-n", cfg.get("process") or "serened"])
        o = subprocess.run(argv, capture_output=True, text=True, timeout=10)
        pid = int(o.stdout.strip().splitlines()[0])
        return pid if pid > 0 else None
    except Exception:                                           # noqa: BLE001
        return None


def threads(pid, prev, prev_t):
    """Per-thread CPU and state, newest first by CPU.

    Threads, not the process total. 100% of one core out of 24 reads as "4% busy" at process level
    and as a pinned thread here — and that difference is the whole diagnosis when a single thread
    spins.

    `pct` is a delta over the whole refresh interval; `st` is a single instantaneous sample taken
    at the end of it. The two are not comparable, and treating `st` as a verdict on the interval is
    wrong: a thread at 60% duty is off-CPU 40% of the time, so the one read lands on `S` about two
    times in five. Only `D` — blocked in the kernel, which `pct` cannot show — is worth reporting
    from `st`. Everything else the renderer takes from `pct`.
    """
    out, cur = [], {}
    # comm defaults to the process name for any thread that never called pthread_setname_np, and
    # DuckDB's scheduler pool never does — 103 of serened's 107 threads report "serened", which
    # names nothing. jemalloc does name its own, so a comm that differs from the process's is real
    # information and is kept; one that matches is dropped for the tid, which at least identifies
    # the row and is the key perf attributes samples to.
    try:
        with open(f"/proc/{pid}/comm") as f:
            pcomm = f.read().strip()
    except OSError:
        pcomm = ""
    # Read everything first, then stamp the clock. dt used to be measured in the caller, around a
    # loop body that also parses perf captures and shells out to du — work whose duration swings by
    # hundreds of milliseconds between ticks. The ticks counted always spanned read-to-read, so any
    # swing landed straight in the divisor and printed a thread at 110.6% of a core, which is not a
    # thing that can happen. Stamping immediately after the reads makes the divisor the interval the
    # counters actually cover.
    seen = {}
    try:
        for t in os.scandir(f"/proc/{pid}/task"):
            try:
                with open(f"{t.path}/stat") as f:
                    fields = f.read().rsplit(") ", 1)
                comm = fields[0].split("(", 1)[1]
                rest = fields[1].split()
                seen[t.name] = (int(rest[11]) + int(rest[12]), rest[0], comm)
            except (OSError, IndexError, ValueError):
                continue
    except OSError:
        # The process went away mid-scan - it was restarted, or it died. Four values, in the same
        # order as the normal return: this path returned three, in a different order, and the caller
        # unpacks four, so the dashboard did not degrade here, it crashed with
        # "not enough values to unpack" the moment the server it watches was restarted.
        return [], 0.0, cur, prev_t
    now = time.time()
    dt = now - prev_t if prev_t else 0.0
    total = 0.0
    for tid, (ticks, st, comm) in seen.items():
        cur[tid] = ticks
        if prev and tid in prev and dt > 0:
            # Clamped at one core: the remaining error is sub-tick quantisation, and a bar that
            # overfills its own scale is a worse lie than a rounded 100.0%.
            pct = min(100.0, (ticks - prev[tid]) / os.sysconf("SC_CLK_TCK") / dt * 100)
            # Summed over EVERY thread, before the display filter. `top` says serened is at 300% and
            # the rows say 8%, 6%, 6% — both true, three cores spread over a hundred threads, and
            # with no total on screen the panel looked like it was missing the work.
            total += pct
            if pct > 1:
                name = (comm[:18] if comm != pcomm else
                        "main" if tid == str(pid) else f"tid {tid}")
                out.append((pct, name, st, tid))
    # Every thread that cleared the filter, not the top 32: the main panel slices to what fits, and
    # the `t` view wants the rest. A cap here would have silently limited both.
    return sorted(out, reverse=True), total, cur, now

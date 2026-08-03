# serenedash

Live terminal dashboard for a SereneDB server. Single file, stdlib only, no dependencies.

![serenedash](serenedash.png)

    ./serenedash.py                 refresh every 5s
    ./serenedash.py --once          one frame (scripts, logs)
    ./serenedash.py --perf-dir DIR  where perf-snap.sh writes captures

Every panel has a view behind it, keyed by its own name, and every view is a toggle:

`q` quit · `s` storage · `m` memory · `a` activity · `t` threads · `p` profile · `g` call graph ·
`c` config · `h` host · `l` legend · `j`/`k` scroll

`l` documents every label and number on the screen. It is the place to look first.

## Panels

| panel | answers |
|---|---|
| storage | database vs WAL size, and the **ratio** — a WAL several times the database means checkpointing has stalled, not that the WAL is big. Temp files older than the process are counted as `orphaned`, not as spill |
| memory | `duckdb_memory()` by pool with a history trace each, plus RSS and **swap** — a store reporting 34 GB of buffers while 30 GB of it is paged out looks fine until you see that row |
| activity | live query text from `pg_stat_activity`; **and** it says so when nothing is running, because a pinned core with no session is orphaned server-side work |
| threads | total process CPU against every core, then the threads carrying it. 100% of one core reads as 4% at process level and as a pinned thread here. Rows are identified by tid — 103 of serened's 107 threads inherit the process name |
| profile | sampled symbols by engine over a sliding window of captures, joined per thread so a row says what it is running |
| host | cores, load, RAM, swap, and `memory_limit` as a share of the machine — the context every other number is read against |
| config | the settings with measured consequences, each predicate evaluated against the live server. `c` opens all 297 with the server's own descriptions |

Every bar and its history share one denominator, and each panel says what that denominator is. A
thread bar is a share of one core; storage shares add to 100 against the on-disk total; a memory
sparkline is its own bar over time.

## MCP

`serenedash_mcp.py` exposes the same collectors as MCP tools, so an agent can read the server's
state instead of being shown a screenshot of it. `serenedash.py` stays stdlib-only; only the MCP
server needs the dependency:

    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
    .venv/bin/python serenedash_mcp.py              # read-only
    .venv/bin/python serenedash_mcp.py --allow-write   # also exposes set_setting

For Claude Code, from the directory you start it in:

    claude mcp add serenedash --scope project -- \
      /path/to/.venv/bin/python /path/to/serenedash_mcp.py

Tools: `status` `storage` `memory` `activity` `threads` `profile` `callgraph` `host` `config`, plus
`set_setting` under `--allow-write`. `status` is one round trip and leads with `findings` — each
one a condition that was measured, with the numbers behind it and how to check it, rather than a
verdict to be taken on trust. Same environment variables as the dashboard
(`SERENEDB_CONTAINER`, `PGPASSWORD`, `SERENEDASH_PERF_DIR`, …).

Rates arrive next to their base — `{"cpu_percent_of_one_core": 94.9, "cores": 24}`, storage shares
with the total they divide. Every display bug this dashboard has had was a unit error rather than a
collection error, and a bare number in JSON is exactly as easy to misread as a bare number on screen.

## perf

The dashboard cannot record: `perf_event_paranoid` blocks attaching to a container process without
root, and running the whole tool as root to get one panel is a bad trade. So `perf-snap.sh` ships
alongside it and does the recording under sudo:

    sudo ./perf-snap.sh --container oracle-serenedb --window 100 --every 900

`--every N` replaces a manual `perf record ... -- sleep N` loop; phase detection replaces a one-shot
catch-and-exit script, and re-arms. After each capture it signals the dashboard (`SIGUSR1` via a pid
file in the perf directory) so new data appears immediately. The dashboard rescans every tick anyway
— the signal only removes the latency, so an older perf-snap still works.

Symbols resolve for an **unprivileged** reader only if the matching binary is registered by build-id:

    perf buildid-cache --add /path/to/the/serened that matches this build

Without it, perf can name symbols only for a reader that can reach the binary through
`/proc/<pid>/root` — which is root — so perf-snap's own summaries have names while the dashboard
shows raw addresses. The `profile` panel prints the command when it detects that. A new build is a
new build-id, so this is per-build. Kernel *symbols* additionally need
`sysctl kernel.kptr_restrict=0`; without it they stay as addresses and the engine split still works.

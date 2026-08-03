# serendash

Live terminal dashboard for a SereneDB server. Single file, stdlib only, no dependencies.

    ./serendash.py                 refresh every 5s
    ./serendash.py --once          one frame (scripts, logs)
    ./serendash.py --perf-dir DIR  where perf-snap.sh writes captures

Keys: `q` quit · `c` effective config · `s` call graph · `j`/`k` scroll.

## Panels

| panel | answers |
|---|---|
| storage | database vs WAL size, and the **ratio** — a WAL several times the database means checkpointing has stalled, not that the WAL is big |
| memory | `duckdb_memory()` by tag, so a grown IN_MEMORY_TABLE is visible rather than inferred from RSS |
| activity | live query text from `pg_stat_activity`; **and** it says so when nothing is running, because a pinned core with no session is orphaned server-side work |
| threads | per-thread CPU and R/S state. 100% of one core reads as 4% at process level and as a pinned thread here |
| cycles | top symbols over a sliding window of the newest captures, plus the user/kernel split |
| config | `c` — all 297 settings with the server's own descriptions, hazards first and annotated |

## perf

The dashboard cannot record: `perf_event_paranoid` blocks attaching to a container process without
root, and running the whole tool as root to get one panel is a bad trade. So `perf-snap.sh` ships
alongside it and does the recording under sudo:

    sudo ./perf-snap.sh --name serened --window 100 --every 900

`--every N` replaces a manual `perf record ... -- sleep N` loop; phase detection replaces a one-shot
catch-and-exit script, and re-arms. After each capture it signals the dashboard (`SIGUSR1` via a pid
file in the perf directory) so new data appears immediately. The dashboard rescans every tick anyway
— the signal only removes the latency, so an older perf-snap still works.

Kernel *symbols* need `sysctl kernel.kptr_restrict=0`; without it they show as addresses, but the
user/kernel split is always available.

## Notes

`--once` shows `···` for anything derived from a delta (thread CPU, rates) — one sample has no
previous sample. That is structural, not a bug.

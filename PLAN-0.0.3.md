# 0.0.3 plan

Written after reading the SereneDB and DuckDB docs end to end and checking the findings against
`oracle-serenedb`. Each item exists because something specific could not be answered with the
dashboard as it stands, not because it would be nice to have.

`v0.0.2` is tagged. The collectors these build on are already in `db.py` and verified against the
live server: `search()`, `progress()`, `temp_files_held()`, and `memspill` inside `sample()`.

## Why these, in this order

The dashboard covers the process (`/proc`, `perf`) and the store (`duckdb_*`). It has no number at
all from the search engine, which is what SereneDB actually is. This deployment runs a 54.1 GB
inverted index over 11.2M documents in 16 segments, and none of that was on screen.

It matters beyond completeness. SereneDB's own developer offered two hypotheses for the periodic
5x CPU spikes: refresh coupled to the autocheckpoint (single-threaded, synchronous segment flush),
or compaction. Both are directly observable in `sdb_metrics` - `refresh_active`, `refresh_pending`,
`compaction_active`, `avg_consolidation_time_ms`. We spent a week not being able to tell those
apart. `avg_consolidation_time_ms` read 672 ms in one sample and 15,368 ms in another taken an hour
later, with `compaction_active` at 1. That is the answer arriving the moment the number is
collected.

## Work items, by owner

File ownership is exclusive. Nothing outside your list, so four of these can run at once without
touching each other. Add tests in a NEW file rather than extending a shared one.

### A - the search panel

Owns `views.py`, `tui.py`, `tests/test_search_view.py`.

1. `search_frame(...)` - a detail view behind a new key, listing per-index rows: live docs, deleted
   (`num_docs - num_live_docs`), buffered, segments, files, on-disk size, and the three
   `avg_*_time_ms`. Server-wide counters as a header line.
2. A compact search row on the main frame if the budget allows it, or fold the pending counts into
   an existing panel. Constant height either way.
3. Activity: show statement size, and flag when a large share of it is one literal. 68 KB with 31%
   in a single float literal is the finding that took a vendor reading a screenshot to spot.
4. Storage: show what `temp_files_held()` returns next to the orphaned figure. "server holds 0 of
   24 files" makes the orphan claim prove itself.
5. Memory: render `memspill` - which pool spilled, not just that spill happened.

Rules that apply: constant panel height, one denominator per row, `clip()` for truncation, say only
what was measured. See `AGENTS.md`.

### B - doctor checks for the host preconditions

Owns `symbols.py`, `system.py`, `tests/test_doctor_host.py`.

Add rows to `doctor()`. These all fail under load rather than at startup, which is exactly the
shape that view exists for. Two of them fire on this deployment today.

1. `RLIMIT_NOFILE` from `/proc/<pid>/limits`. The server raises its own soft limit to 65535; the
   documented target is 131072. This box is at 65535 soft, 524288 hard - so it is raisable and
   currently below target.
2. `vm.max_map_count` from `/proc/sys`. Documented minimum 262144, because IResearch memory-maps
   every index segment. This box is at 1048576, so the check passes here and still belongs.
3. `allocator_background_threads`. Off by default; documented as worth enabling on many-core
   machines. Off on this 24-core box.
4. `temp_directory` must be absolute. A relative path resolves against a root-owned cwd and every
   spill fails with EACCES, silently, until the working set first exceeds `memory_limit`.

Each row needs the same shape as the existing ones: status, what it costs you, and the exact fix.

### C - hazard predicates that measure

Owns `hazards.py`, `tests/test_hazards_measured.py`.

The config panel already runs predicates against the live sample. These use real data rather than
comparing against a constant.

1. `zstd_min_string_length` against the actual average length of the largest text column. 4096
   against a real average of 1891 means zstd can never fire, and that is computable rather than
   guessable.
2. `checkpoint_threshold` against the number of concurrent writers. 16 MiB with 11 of them is why
   analyse-and-compress runs continuously on row groups that never fill.
3. `auto_checkpoint_skip_wal_threshold` against observed statement sizes. Above it the store skips
   the WAL and blocks concurrent commits during the checkpoint.
4. `disabled_compression_methods` empty while a wide float column exists - every codec is analysed
   on a column that is incompressible by construction.

Predicates may need a number the sample does not carry yet. Adding to `sample()` is allowed if it
is one column on an existing statement; a new round trip is not.

### D - expose it over MCP

Owns `snapshot.py`, `mcp_server.py`, `instructions.md`, `tests/test_snapshot_search.py`.

1. `search` in `collect()`, and a `search()` tool.
2. `progress` folded into the `activity` payload.
3. `temp_files_held` into the storage payload, beside the orphaned split.
4. Findings for: non-zero `num_failed_*`, a growing pending queue, `num_docs - num_live_docs`
   above a threshold, and consolidation time above a threshold.
5. `instructions.md` - fold the new fields into the vocabulary and the "what the panels do not
   cover" section, which currently tells an agent to reach these by hand.

Every rate ships with its base. Keep `status()` under the size cap.

## Deferred

**Anomaly rules over the search series.** Wants `sdb_metrics` in the recorded history first, which
means A and D landing. Segment count and pending queues are the monotonic-growth shape the existing
rules already detect.

**The spike explainer.** Asserting "this CPU spike is a refresh" needs WAL sawtooth, `refresh_active`
and the CPU series correlated over a real window. Build the collection first, look at the data, then
decide whether the correlation is strong enough to put on screen. Claiming it before checking is the
mistake this codebase keeps a rules file about.

## Not ours

RAGFlow sending 1024-dim embeddings as 21,684-character text literals is an adapter fix, not a
dashboard feature. The dashboard's job was to make it visible, which item A.3 does.

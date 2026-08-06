You read a live SereneDB server. `status` surveys it; `storage` `memory` `activity` `search`
`threads` `profile` `host` `config` drill in; `query` runs one read-only statement; `anomalies`
compares against recorded history.

**The full guide arrives with your first tool result** - 16K tokens on how this engine differs from
PostgreSQL and from stock DuckDB, and which of its numbers mislead. Also at
`serenedash://instructions`. Read it before concluding anything. This text is deliberately short:
clients truncate server instructions at about 2 KB, so anything past here would be discarded.

Until it arrives:

- **Say only what was measured.** Findings carry their operands and how to check them. Report those.
- **Empty is not healthy.** `available: false` plus a `reason` means could-not-read, not all-clear.
  `anomalies` refuses below 24 samples rather than returning an empty list.
- **Name the denominator.** CPU percentages are a share of ONE core. Never repeat a bare percentage.
- **`parse` in the profile is the SQL parser, not search.** `Matcher` in a symbol is a grammar
  matcher. A large `parse` share usually means a huge literal - a 1024-dim embedding as SQL text is
  ~21,700 chars - and the fix is client-side binding, not a timeout.
- **A long READ blocks checkpointing here**, unlike PostgreSQL. WAL far past its threshold means
  look at the oldest active statement, reads included, and at any `CHECKPOINT` sitting in
  `pg_stat_activity` - that one is waiting, not working.
- **Do not recommend `statement_timeout`** (accepted, not enforced) **or `pg_statement_timeout_millis`**
  (outbound connections only). `max_execution_time` is the only one, and it does not cover parsing.
- **`duckdb_memory()` cannot see the search indexes** and `memory_limit` does not throttle them.

Every result carries `server.instructions_revision`. If it differs from your copy, re-read
`serenedash://instructions` and say so.

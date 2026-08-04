# Working on serenedash

Rules that cost something to learn. Each one below was violated by a real change to this repo, and
each produced a panel that stated something the data could not support.

## The one that matters: say only what was measured

A dashboard's failure mode is not a crash, it is a confident wrong sentence. Three shipped here:

- **`sleeping`** on a thread at 60% of a core. The percentage was a delta over the refresh interval;
  the state was one instantaneous `/proc` read. A thread at 60% duty is off-CPU 40% of the time, so
  the single read lands on `S` about two times in five. The two are not comparable and only `D`
  (blocked in the kernel, which a percentage cannot show) survived.
- **`spilling`** on a temp directory that had not grown in a day. Size is a level; spilling is an
  activity. Only a delta can distinguish them, and only file mtimes can tell live spill from the
  wreckage a killed query left behind.
- **`1 active`** printed directly above **`nothing running`**. Two definitions of "active" one line
  apart: the count included this dashboard's own session, the list did not.

Before adding a word to a panel, ask what measurement would be false if the word were wrong. If
there isn't one, the number alone was already saying it.

## One denominator per row, and name it

A bar and its history answer the same question, so they divide by the same thing. Rates travel next
to their base. This was wrong in four places independently:

- thread bars normalized to the busiest thread, so the top row was always full whether it sat at 8%
  or 99% — it must be a share of one core
- storage shares divided by a total that appeared nowhere on screen
- the engines line divided by the six symbols that fit, printing `columnar 50%` above rows of 2%
- memory sparklines self-scaled, so a flat 260 MB pool drew the same full-height trace as a 34 GB one

## Fetch what will be displayed

Bound server-supplied text at the source, and size the bound to the consumer.

The activity query fetched whole statements. On this deployment a generated `INSERT` is ~185 KB, so
twelve sessions moved **1.84 MB through a docker exec every five seconds** — for a panel that gives
each session one row and clips it to about 90 columns. The same unbounded text went out over MCP as
a single 1.66 MB tool result, of which `activity` was 1.66 MB and every other panel together was
under 14 KB.

`left()` in the SQL, sized to what the caller will render, cut both to 40 KB and 15 KB. Trimming on
arrival would have fixed neither — the bytes still cross the wire to be thrown away. Carry
`length()` alongside the head so a truncated value is never mistaken for a short one, and let the
one view that shows whole statements re-fetch them, only while it is open.

**Nothing that costs a round trip belongs on the redraw path.** That view re-fetched them inside the
render lambda, so it ran per keypress rather than per tick — invisible until pointer tracking landed
and started reporting every cell the mouse crossed, which turned a 185 KB fetch into one per cell of
mouse movement. A redraw is free by construction; keep it that way and gate everything else on the
data tick.

## Layout

- **Constant height.** A panel's height comes from a budget, not from how many rows it happens to
  have. A frame that resizes when a query ends is one you have to re-read from the top every
  refresh. Pad short panels; count overflow on the last row.
- **Truncate by visible width.** Every row is full of ANSI escapes, so `s[:n]` counts escape bytes
  as columns and can cut mid-sequence. Use `clip()`. Its subtlety: a string whose visible length
  exactly equals the budget is *not* truncated, even though trailing escape bytes remain — treating
  it as truncated appends an ellipsis worth one real column, which is how one memory row ended up a
  cell right of every other.
- **Fit the terminal both ways**, and never exceed it. The status bar is pinned to the last line.
- **An overlay draws over the frame, never into it.** The tooltip is written after the frame and
  marks the rows it covered dirty so the next pass repaints them. Inserting it into the line list
  would push the panels down and move the thing being pointed at. Its own border arithmetic is the
  usual trap: a box row is `│` + a space + the text + padding + `│`, so the inner width has to be
  the longest line **plus one** — sized to the longest line itself, that row's padding went negative
  and it printed a column past its own border, over the frame beneath.

## Degrade by panel, not by process

Losing the connection used to replace the whole screen with one line saying it could not reach the
server. Most of what is on screen never needed it: the threads panel and the profile are /proc and
perf captures, the storage directory sizes are `du`, and `resident`/`swapped` are `/proc/<pid>`.
A server that will not accept a connection is exactly when those are worth looking at.

So each panel keeps the rows it can still measure and says which of `no driver` /
`no credentials` / `cannot connect` applies to the rest, with the fix. What it must NOT do is draw
the shape with zeros in it: `sessions 0` above `nothing running`, rendered from an empty result, is
not a degraded panel, it is a false one. Activity and config are replaced wholesale for that reason
and storage and memory are not.

The frame keeps its height either way. A layout that depends on whether a password is set is one
you have to re-read whenever the connection blips.

## Absence of evidence

`findings: []` means nothing tripped. `anomalies` on eleven samples means nothing could have
tripped, and reporting those the same way is the one mistake this whole file exists to prevent — it
is the same error as `sleeping` on a thread at 60%, one level up. Anything that judges a window
says how much window it had, and refuses when there is not enough.

## SQL that came from outside this file

`query(sql)` over MCP hands an agent a statement runner against someone's database, and `config`
and `set_setting` take arguments that end up in one. These rules are in `db.py`; the last of them
is there because writing this list down found a violation.

**Bind, do not interpolate.** `query(cfg, sql, params=[(v,)])`, one tuple per statement. The MCP
`config` tool built `where name = '{name}'` from its own argument — read-only connection, so it
could not write, but it could read anything. "The damage is bounded" is not "the query is correct".

**Two interpolations are allowed and both are vetted:** `left(query, {int(query_head)})`, cast to
int at the boundary so it can only be a number, and the `HAZARDS` name list, which is this file's
own table. A test walks every f-string in `db.py` carrying a SQL keyword and fails on anything
else — the next one of these will be somewhere new, so the check is for the class, not the case.

**Identifiers cannot be bound, so validate them.** `SET GLOBAL {name}` has no parameter form;
`name` must match `[A-Za-z_][A-Za-z0-9_]*` or it is refused. It comes from the server's own
settings list, but a dashboard that can be talked into running SQL by a setting name is a dashboard
with an injection bug, and the check costs nothing. Values in that statement are single-quoted with
`'` doubled.

**Caller SQL passes an allowlist of statement kinds, not a blocklist.** `select with show describe
explain summarize pragma values table from call`. A blocklist has to be complete to be correct, and
DuckDB grows statement kinds faster than anyone will remember to update one.

**Read the kind past comments and brackets.** `-- harmless\nDELETE FROM t`, `/* x */ drop table t`
and `((select 1))` all have to resolve to what they actually are. A naive prefix check is the
obvious way past this.

**One statement.** `select 1; drop table x` has a read-only leading keyword and a write behind it.
A single trailing semicolon is not a batch.

**Check before connecting**, so a refusal costs nothing and cannot hang on an unreachable host.

**Open the connection read-only anyway.** `cn.read_only` unless `_write` is set, which only
`apply_setting` does. The allowlist is the first of two lines, not the only one — but it exists
because "the server would have rejected it" is a poor thing to learn from an error message, and
read-only does not stop a statement from being expensive.

**Bound the output twice**: rows, then total characters. A hundred rows of a wide system view is
still megabytes and the reader is a context window. Truncation is always reported
(`truncated_rows`, `truncated_chars`) — a silently short answer is a wrong answer.

**Return the server's error, trimmed.** Not raised, not swallowed: it is the useful half of a
failed query. Cut at 2000 characters, because a DuckDB parse error quotes the statement back with
a caret diagram.

## Explanations live in one place

The tooltip does not carry its own prose. It looks up `LEGEND`, which already claims to document
every label on screen, so `l` and the pointer cannot disagree and there is no second copy to go
stale — the drift showed up immediately, in a `headroom` entry for a row that had been removed. The
same rule as the collectors: one implementation, two consumers. It also inherits the constraint that
matters — the legend says only what was measured, so the tooltip cannot invent a reading of a number
that the panel itself would not make.

Where the legend cannot answer, say less rather than guessing: a symbol name or a statement is the
server's text, not the dashboard's vocabulary, and matching words in it against the legend answered
`flat_map` with the storage panel's definition of `flat`.

## Dependencies

This was a single stdlib-only file for a while, and that constraint was inherited from its own
README rather than chosen. It cost more than it bought: reaching the server meant `docker exec psql`,
which works for exactly one deployment shape and pays a process spawn per tick.

It is a normal Python package now. `psycopg` is a real dependency and every target uses it, so
reaching the server does not branch on where the server runs. `mcp` is an extra, because someone who
wants the TUI should not have to install an agent protocol. Filesystem and /proc access still fork by
target — those genuinely differ.

One implementation of each collector, shared by the TUI and the MCP server. Two copies of the thread
accounting would drift within a week.

That applies above the collectors too. The whole-server snapshot lived inside the MCP server, which
made `--format json` impossible to write without either a second implementation or a dependency on
`mcp` for a flag that has nothing to do with it. It is `snapshot.py` now: one builder, three callers
— `status()`, `--format json`, and the findings the dashboard shows.

`perf-snap.sh` must pass `shellcheck` and `shfmt` before it ships.

## Verify against the live server

`ast.parse` proves nothing about a dashboard. Run it — `--once` for a frame, a real loop for
anything involving a delta, a redraw, or a key — and read the output. Bugs found this way that no
syntax check would have caught: threads showing zero rows because a "N more" notice consumed the
only budgeted line; every frame one line too tall so the bottom border scrolled off; keys echoing
into the corner of the screen because the terminal was left in cooked mode between reads; the call
graph taking 81 seconds per tick.

Check widths and heights against several terminal sizes, not just yours.

## Comments

Explain the finding, not the syntax. A comment here should say what was measured and what it ruled
out, so the next person can tell whether the reasoning still holds — most of this file's comments
exist because someone would otherwise "simplify" the code back into a bug. Cite the number when
there is one: `81.06s with inline expansion, 0.30s without` is worth more than `slow`.

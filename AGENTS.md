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

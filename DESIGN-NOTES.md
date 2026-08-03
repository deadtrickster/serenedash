# Pending design — not yet built

## `p` — a process × stage matrix

Modelled on the pg-fuzz matrix (screenshot `photo_2026-08-03_14-24-20.jpg`), which solves the same
problem: many workers each moving through the same fixed set of stages, where the interesting thing
is the *pattern* across the grid rather than any single number.

Shape:

```
                    rp sq sp pr br sc bt nu dt fm ge nw jb jp jx
  pg17-7-add   REL_17_  ·  1  1  ·  2  ·  ·  ·  ·  ·  ·  2  ·  ·  ●   14
  pg17-7-und   REL_17_  3  1  1  ·  1  2  1  1  4  ·  4  ·  ·  3  ·   33
  pg18-4-add   REL_18_  ·  ·  1  ·  ·  ·  ·  ·  ·  ·  ·  3  ·  ·  ●    8
```

What makes it readable, and what to copy:

* **rows are workers, columns are stages** — two-letter column codes, expanded in a `targets:` key
  at the bottom so the header stays narrow enough to fit many columns.
* **a cell vocabulary rather than numbers everywhere**: `··` not built, blank idle, `●` running
  (blinking), `·` swept clean, `NN` a count. Most cells are quiet, so the eye lands on the ones that
  are not.
* **inactive rows dimmed entirely**, active rows bold white. Half the rows in the screenshot are
  doing nothing and they recede.
* **a legend line** (`cells: ·· not built  (blank) idle  ● running …`) — without it the glyphs are
  a private language, which is what `◦0` was here before it was removed.
* header carries the invariants: phase, round, `built 20/24`, elapsed, `[q] quit`.

For this stack the mapping is: rows = RAGFlow task executors (or serened sessions), columns =
pipeline stages (parse / chunk / embed / insert / commit). The value of the grid is that a stage
that is blocked for everyone shows as a full column, while one slow worker shows as a row — and
those two have completely different causes. Today's embedding bottleneck is a column; an orphaned
query is a row.

Open question: for serenedash specifically, "processes" may be the wrong noun. SereneDB exposes
sessions, not stages, so the columns would have to come from query phase, which is not currently
observable through `pg_stat_activity`. This may belong in ragdash instead, where the stages are
real and named.

## Grid conventions to match (from ragdash)

```
COL_LABEL = 18   COL_VALUE = 16   COL_BAR = 18   COL_NUM = 12
line(label, value, glyph, tail, lc, vc, va)
```

Every row goes through one `line()` so the glyph column is a single ruler down the frame and the
number after it lands on the same column in every panel. Labels truncate with `…`, not by clipping.
serenedash currently builds rows ad hoc and should adopt this before it grows more panels.

## Mouse support — TODO

The config view is cursor-driven (`j`/`k`, Enter for the untruncated description). Real clicking
needs SGR mouse tracking:

* enable with `\033[?1000h\033[?1006h` on entry, disable on exit — and it MUST be disabled in the
  `finally` block alongside the cursor and termios restore, or the terminal keeps emitting escape
  sequences into the user's shell after the dashboard quits.
* parse `\033[<b;x;yM` / `m` from stdin in `wait_key`, which already selects on stdin so the plumbing
  is there.
* map row → setting using the same `body[scroll:scroll+view]` slice the renderer uses, so hit
  testing and drawing cannot disagree. Deriving the mapping separately is how a click lands on the
  wrong row after a resize.
* wheel events are buttons 64/65; treat them as scroll, not selection.

Worth doing for the config list and for the datasets rows. Not worth doing for the bars.

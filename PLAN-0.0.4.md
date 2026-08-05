# 0.0.4: the Zig rewrite

You are typing this one. This is the plan, the risks I would want to know before starting, and the
one piece of scaffolding that decides whether it goes well.

## Verdict

Feasible. Both hard dependencies exist and I checked them rather than assuming:

- **zigzag** (`meszmate/zigzag`) - 32,397 lines, zero third-party dependencies, `minimum_zig_version`
  0.16.0, which matches the local toolchain. `zig build test` exits 0 and every example compiles
  against 0.16.0. Created 2026-01-31, 246 commits over six months, last push 2026-07-24. Elm
  Model-Update-View, diff rendering rather than full repaint, mouse and resize handled. Components
  that map onto what we already draw: `sparkline`, `gauge`, `chart`, `data_table`, `sortable_table`,
  `split_pane`, `status_bar`, `viewport`, `virtual_list`, `heatmap`, `tooltip`.
- **pg.zig** (`karlseguin/pg.zig`) - 10,478 lines, SCRAM-SHA-256 in `src/auth.zig` and
  `src/proto/SASLInitialResponse.zig`, connection pool, actively maintained (last commit
  2026-07-22). Two small dependencies of its own.

- **The web dashboard, added in 0.0.3.** `--format html`, `--format svg` and `--serve` mean the Zig
  port also needs an HTTP server that can hold a `text/event-stream` open. Two options, both
  checked by building rather than by reading: **`std.http.Server`** exists in 0.16 and is enough for
  SSE, since SSE is an ordinary response that never ends. **`karlseguin/http.zig`** is 12,934 lines,
  last commit 2026-08-03, and carries a websocket module if that is ever wanted. The SVG export
  itself is pure string building and ports directly - `export.py` is deliberately a pure function
  from ANSI lines to a document, with no terminal in it.

The standing rule holds: use a library for the wire protocol, do not implement pg proto.

**MCP is the problem, and not for the reason I first assumed.** There *is* a Zig SDK -
`muhammad-fiaz/mcp.zig`, 6,308 lines, `minimum_zig_version` 0.16.0, builds clean and its tests pass.
But look at the versions:

| | protocol revision | last activity |
|---|---|---|
| MCP specification | **2026-07-28** | released 2026-07-28 |
| our Python server (`mcp` 2.0.0) | **2026-07-28** | released the same day |
| `mcp.zig` v0.0.5 | **2025-11-25** | last commit 2026-05-28 |

One revision behind, and it is the revision that restructured the protocol rather than extending it.
2026-07-28 **removes the `initialize` / `notifications/initialized` handshake** and makes MCP
stateless: every request carries its protocol version and client capabilities in `_meta`, and
servers **MUST** implement a new `server/discover` RPC. It also requires a `resultType` on every
result, requires `ttlMs` and `cacheScope` on list and read results, replaces
`resources/subscribe` with `subscriptions/listen`, removes `ping` and `logging/setLevel`, renumbers
error codes, and deprecates Roots, Sampling and Logging outright.

So `mcp.zig` implements the shape the current spec deleted. Using it means either shipping a server
a revision behind (clients negotiate, so it would work, but new clients would be talking down to it)
or doing the 2026-07-28 work yourself inside someone else's abstraction - which is usually harder
than doing it in your own.

One thing that does survive the redesign, and it matters to us: `DiscoverResult` still carries
`instructions`. The staleness mechanism in `instructions.md` moves from `InitializeResult` to
`DiscoverResult` and otherwise stands.

**Recommendation: defer this decision, do not gate the rewrite on it.** An earlier draft of this
plan said to decide before writing any Zig. That was wrong for two reasons.

It is not on the critical path. MCP is phase 3 of 4 below; the collectors and the differential
harness come first and do not touch it. By the time it matters, `mcp.zig` may well have caught up -
the spec is eight days old and SDKs normally lag a release by weeks.

And the fallback got cheaper, not more expensive. The old protocol had a handshake, session state,
subscription bookkeeping and server-initiated requests. 2026-07-28 deleted all of it. A stdio server
exposing tools plus one resource needs `server/discover`, `tools/list`, `tools/call`,
`resources/list` and `resources/read` - stateless request/response, with comptime generating the
tool schemas from function signatures the way Python's decorators do by introspection. That is a few
hundred lines, not a protocol project.

So: bet on `mcp.zig` catching up, and hand-roll against the current spec if it has not. Either way
the answer arrives when you reach phase 3, and nothing before then depends on it.

## What the rewrite buys, honestly

A single static binary. No venv, no Python on the target host, no `pip install` on a machine you are
debugging. Fast startup and low RSS for something you drop onto a server that is already in trouble.
For an observability tool that is a real argument, not a taste argument.

## What it costs, equally honestly

Sixty-odd tests and roughly a year of accumulated correctness. Read `AGENTS.md`: every rule in it
exists because a panel shipped a confident wrong sentence. `clip()` counting escape bytes as
columns, constant panel height, one denominator per row, `fit(items, 1)` eating the only row,
the frame being one line too tall so the bottom border scrolled off. None of that is hard code. All
of it is hard-won, and a fresh implementation re-learns every single one unless something stops it.

That is the whole risk. Not the language, not the libraries.

## The scaffolding that decides it: differential testing

`--format json` already exists in Python and emits the full snapshot from `snapshot.py`. Build the
Zig port so that its `--format json`, run against the same live server within the same second,
produces the same structure. Then:

```
serenedash --once --format json          > /tmp/py.json
serenedash-zig --once --format json      > /tmp/zig.json
jq -S . /tmp/py.json > a; jq -S . /tmp/zig.json > b; diff a b
```

Numbers that come from a moving system will differ - CPU deltas, timestamps, byte counts a second
apart. So compare **shape and derivation**, not values: same keys, same types, same units, same
denominators, and for anything derived (`wal_over_database`, `used_fraction_of_limit`,
`cpu_percent_of_one_core`) the same arithmetic applied to the same inputs. Feed both a frozen sample
where you can.

This turns the rewrite from "port it and hope" into "port it against an oracle". Build this in week
one, before any TUI code. If you write the renderer first, you will be debugging two unknowns at
once.

## Order of work

**0. Spike, before committing to anything.** One session, three questions, throw the code away:

  a. **Does pg.zig talk to SereneDB?** This is the real risk and it is not obvious. SereneDB
     *emulates* the PostgreSQL wire protocol; it is not PostgreSQL. psycopg works, but pg.zig may
     lean on things SereneDB stubs. Most catalogs are stubs by design - `pg_type` and `pg_class` are
     real, `pg_index`, `pg_locks` and `pg_stats` are not. A driver that resolves type OIDs from a
     stubbed catalog fails in week three, not on day one. Connect, authenticate with SCRAM, run
     `SELECT * FROM duckdb_settings()` and `SELECT * FROM sdb_metrics`, and read a few types back.
  b. **Does zigzag render our shape?** Not "does it run" - it does. Build one panel with a border, a
     bar, a sparkline and a clipped tail at 200, 150, 100 and 80 columns, and check the frame never
     exceeds the terminal and never changes height with content.
  c. **What does a `ReleaseSmall` static binary weigh?** The debug example binaries are 13-18 MB.
     If the release build is not small, the main argument for the rewrite weakens.

**1. Collectors, no TUI.** `/proc` reading, `du`, perf capture parsing, the SQL queries. Output the
snapshot as JSON. This is the bulk of the real logic and it is all testable without a terminal.
Differential-test it against Python from the first commit.

**2. The renderer.** On top of a snapshot struct, not on top of the collectors. Keep frame building
**pure** - a function from snapshot to `[][]const u8` - with zigzag only doing input, the event loop
and the diffed write. That keeps the widget layer swappable, which matters (see the risk below) and
also makes the layout testable without a pty, exactly as `test_views.py` does today.

**2b. The export and the server.** Cheap, and worth doing early rather than late: `export.py` is a
pure function from ANSI lines to SVG, so it ports with no terminal involved and gives you a way to
LOOK at the Zig renderer's output in a browser, beside Python's, before any of it drives a real
terminal. That is differential testing you can see rather than diff.

**3. MCP.** Last, because it is the most protocol and the least shared logic. `@embedFile` for
`instructions.md`; it is already a data file and should stay one.

**4. Cut over.** Keep the Python `--format json` alive as the oracle until the Zig one has been
diffed against it across a full range of server states - loaded, idle, no credentials, no perf
captures, no index.

## Risks worth naming now

**zigzag is six months old, one maintainer, 507 stars.** That is fine *if* the renderer stays the
replaceable part. serenedash already writes raw ANSI escapes directly - there is no curses
dependency today - so a zigzag-free fallback is a substitution, not a rewrite. Keep frame building
pure and this risk stays cheap. Let zigzag types leak into the panel code and it does not.

**Zig 0.16 is pre-1.0.** Language and stdlib churn is a tax you will pay at least once during this.

**The MCP protocol surface is larger than it looks.** Handshake, capabilities, tool schemas
generated from function signatures (which Python's SDK does for free via decorators and
introspection - Zig has comptime, so this is doable and interesting, but it is work), resources,
and the stamping/revision mechanism `instructions.md` depends on.

## Carry across rather than rewrite

- **`instructions.md`** - `@embedFile`, plus the SHA-256 revision stamp. Unchanged.
- **The legend text.** It currently lives as a tuple in `views.py` and is the single source for both
  the `l` view and the hover tooltips. Move it to a data file both implementations read, or it will
  fork.
- **The test assertions**, not the test code. Each one names a bug. `test_views.py` asserts frame
  height and width at six terminal sizes; `test_hover.py` asserts every cell of a frame can be
  named; `test_anomaly.py` asserts what must stay *silent*. Port the assertions first and let them
  fail, then write the code.
- **`AGENTS.md`.** It is language-independent. Every rule applies to the Zig one identically.

## Open question

Whether the Python version stays. Two implementations is a cost, but `--format json` as a permanent
oracle has value beyond the port, and the Python one is what CI already exercises against a real
`serenedb/serenedb:26.07.4` container. My inclination is to keep it until the Zig one has run
against a real deployment for a while, then decide - rather than deciding now.

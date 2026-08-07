# Decision trees and heuristics - scope

What this is for: every diagnosis in this project so far has been a walk down a tree that existed
only in someone's head, executed by hand across **three shells** - the dashboard in one,
`perf-snap.sh` under `sudo` in another, `psql`/psycopg in a third - with the operator carrying state
between them. The BM25 hang took two days and most of it was that carrying.

The goal is to move the tree into the tool: the same walk, executed by the thing that already has
the measurements, on one surface.

## Where a tree can live

Three consumers, and a tree written for one is wrong for the others:

| surface | form | who reads it |
|---|---|---|
| `instructions.md` | prose tree with the discriminating measurement at each branch | an agent, before it calls anything |
| findings (`snapshot.py`) | a rule that fires with operands, a fix and a `verify` | terminal and browser, and `status()` |
| a new `triage` tool | the tree **executed**, returning the node reached plus the evidence | an agent mid-incident |

The third is the one that removes the shells. The first two are how it stays honest: the tool must
not reach a verdict the findings cannot show and the guide cannot justify.

## The trees worth encoding

Ordered by how often they have come up, with the discriminator that actually separates the branches.
Every one of these has been walked by hand at least once.

### A. A statement will not finish

The one that cost two days. Three outcomes that look identical in `pg_stat_activity`, because
`state='active'` covers all three and there are no wait events.

```
statement running past a threshold
├─ CPU of its threads ~0
│  └─ started within ~1s of another long statement?      -> BLOCKED (convoy)
│     discriminator: no CPU, no progress, near-simultaneous start
│     and: is a FORCE CHECKPOINT present? it holds start_transaction_lock
├─ CPU ~100% of a core, voluntary ctx switches/cpu-s < 50
│  └─ profile identical across two captures minutes apart -> SPIN
│     discriminator: no phase change, one symbol dominant, no IO, RSS flat
└─ CPU high, profile CHANGES between captures, IO or RSS moving -> genuinely slow
   discriminator: the bucket signature changes at all
```

What it needs that exists: per-thread CPU, `perf-snap` bucket signatures, the profile.
What it needs that does not: nothing. This tree is buildable today.

### B. Checkpointing is not happening

```
WAL / database ratio > 1
├─ a CHECKPOINT in pg_stat_activity?
│  ├─ forced   -> it is WAITING, not working. It also blocks every new transaction
│  │             and busy-spins a core. Cancel it first: it is interruptible
│  └─ plain    -> it errors rather than waits, so an active one is doing real work
└─ none        -> nothing is trying. The blocker is the oldest open statement,
                  reads included -> tree A on that statement
```

Mostly encoded already in `checkpoint_waiting`; needs the corrected lock semantics and a link into
tree A rather than stopping at "deal with the statement in front of it".

### C. The profile is unreadable

Largely automated this session, worth writing down as a tree anyway because the failure looks like a
tool bug.

```
symbols are hex
├─ build-id in the capture registered?
│  ├─ no  -> is the container bind-mounting a binary over the image's?  -> register that
│  │        else docker cp and register
│  └─ yes -> does that copy have symbols at all?  (elf_symbol_count)
│            0 -> it is stripped. Registering again will not help
```

### D. Index maintenance falling behind

```
pending queues (refresh/compaction/cleanup) non-zero
├─ against avg_commit_time_ms / avg_consolidation_time_ms -> falling behind ingestion
└─ num_docs - num_live_docs climbing and segments not consolidating -> deletions accumulating
```

`sdb_metrics` has all of it. Partly shown in the search panel, not expressed as a rule.

### E. Memory

Exists as prose in the guide ("Is memory actually the problem?"). Converting it to a tree is cheap
and mostly editorial.

## The heuristics, and where each number came from

The rule this project already lives by is that a threshold has to say whether it was measured or
chosen. Same here:

| heuristic | number | provenance |
|---|---|---|
| spin vs block | voluntary ctx switches per cpu-second < 50 | chosen, but validated on this box - a spin collapses to ~13, IO-bound work is thousands |
| steady state | no bucket change for N minutes | observed: 11 minutes at constant signature was what proved the spin |
| convoy | statements starting within ~1s of each other, none progressing | observed: four statements within 166 ms |
| WAL stalled | WAL > 1x database | documented |
| BM25 hang exposure | `optimize_top_k` index AND `num_docs != num_live_docs` | derived from the repro's ingredient matrix |
| abandoned statement | age past threshold AND connection much older than the statement | observed: a pooled connection 25 h older than its query |

The BM25 one is the most valuable and the most dangerous: it is a *predictive* finding - "this
server is exposed to a known hang" - not an observation. It must say so, and it must name the
version range, or it becomes noise the day the bug is fixed.

## Proposed order

1. **Findings first** (`snapshot.py`): `convoy`, `spin_suspected`, `bm25_exposure`,
   `index_maintenance_behind`. Each with operands, a fix, and a `verify` statement. Low risk,
   immediately visible on all three surfaces, and it forces the heuristics to be concrete.
2. **Trees into `instructions.md`**, as a "Decision trees" section written the way A is above -
   branch, discriminator, what to measure. This is where the agent-facing value is.
3. **`triage(pid=...)` tool**: executes tree A. Gathers activity + explain + per-thread CPU + the
   two most recent captures' signatures, returns `{verdict, node, evidence[], what_would_change_it}`.
   Never terminates anything.
4. **Trees B-E** as findings and guide sections, once A has proved the shape.

## Risks

- **A heuristic stated as a fact is the failure mode of this whole idea.** The checkpoint finding
  said "the horizon keeps being re-pinned" for months and it was backwards. Every node must carry
  its evidence and be falsifiable from the operands shown.
- **Thresholds drift.** 50 switches per cpu-second is a guess with one validation. It needs the same
  `threshold_is_chosen` treatment the existing findings use.
- **A predictive finding ages badly.** `bm25_exposure` must name the affected versions and check
  them, or it will still be firing after the fix ships.
- **`triage` must not become a thing that pretends to know.** If two branches are equally consistent
  with the evidence it says so and names the measurement that would separate them.

## Not in scope

Killing anything. `triage` reports; the operator decides. Today's session is the argument: the
statements we most wanted stopped could not be stopped at all, and a tool that had promised to stop
them would have been lying.

# Vote-model resolution — per-user marks; sweep needs unanimity

Replaces the actor-blind "most recent mark covering a prefix wins" fold with a
per-user one (2026-09-01, after two whole-`checkpoints/` broad sweeps silently
repainted 255 keeps and a broad unmark repainted 747 more — see
`gcs-usage sweep clobbers`). Proposed by Ryan in this session, refining the
#internal-discuss "any keep wins" idea: keep the *information* (who wants
what) and make the collapse per-question.

## Model

- The ledger is unchanged — every action already records its actor. This is a
  **resolution-layer** change only; no storage migration.
- **A user's vote at a path** = that user's most-recent live keep-axis row on
  an ancestor-or-equal prefix (`ts DESC, action_id DESC` — the existing fold,
  partitioned by actor). A user CAN still repaint their *own* earlier marks at
  any granularity. A NULL keep = **retract my vote** (the user drops out of
  the set), not "paint unmarked".
- **A path's state** = the set of live votes, max one per user:
  - only keep-valued votes → `keep`
  - only `sweep` votes (≥1) → `sweep` — deletable
  - both → **`conflict`** — never deleted; a first-class triage state with
    both parties' provenance (the `sweep clobbers` report is the v1 worklist)
  - no votes → `unmarked` — untouched now, swept when the review window
    closes (deadline policy unchanged)
- **KLC** decomposes into votes: a `keep_last_ckpt` vote = keep on the
  max-step split (`klc_split`) + sweep on the band's remainder, then
  aggregates like any other votes.

## Implementation shape

Partition-by-actor reuses the existing single-actor fold everywhere:

1. **Executor (tonight)** — `gcs_usage.sweep_plan.VoteResolver`: per-actor
   `FateResolver`s + aggregation. Manifest = keys whose vote set is
   sweep-only; `conflict` and `unmarked` excluded and reported. The
   `ever_kept_prefixes` guard stays as belt-and-suspenders (it over-protects
   self-repaints; the vote model is the precise rule).
2. **Site (next)** — `/api/resolve` (per-actor winner: `GROUP BY actor`),
   `_lib/marks.ts computeTotals` (band painting over vote sets; totals gain a
   `conflict` bucket), client `MarkIndex.resolve` + `sweep.ts` fate walks,
   fate legend/color + a conflicts lens, `/marks` feed unchanged (it's the
   raw WAL). CLI `_fate_totals` mirror.
3. **Consequence**: the 2026-09-01 clobbered keeps need **no ledger revert**
   — the victims' votes still stand; the resolution change restores them.

## Non-goals

Owner axis (unchanged). Vote weights/roles (any user's keep blocks deletion;
social pressure handles abuse). Regex patterns (still don't exist).

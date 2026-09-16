# Selection actions: bulk keep/sweep/claim over filter matches & row selections

Ryan (8/26, with screenshots): the filter already *computes* the right selection —
`swarm` → 163 Ti matched, treemap + table scoped to it — "I should be able to do
roughly this, and keep/sweep/clear or claim/clear on the selection, no?" Plus: row
multi-select under the treemap, and a select-all/none checkbox.

## What exists / what landed 8/26

- Filter + re-aggregate: `filterTree.ts` keeps the **outermost matched prefixes**
  (exactly the set a bulk action should target). Now matches **full paths**
  (`grug/swarm` works) and plain-text `|` alternation (`grug|swarm`); `/…/`
  regex spans `/` too.
- `POST /api/actions` accepts an **array** of `{pattern, keep?/owner?, memo, scan}`
  — prefix patterns only; the schema (actions.pattern + expanded
  `keep_prefixes`/`owner_prefixes`) was designed for regex patterns "later".
- Attribution-side name patterns: `prefix_owners` path-globs (this change) — but
  that's curation in yaml, not a user gesture.

## Design

**One selection model, two feeders.**

- `selection: Set<prefix>` in app state.
- Feeder 1 — **filter**: an "actions on matches" affordance next to the match
  chip (`155 Ti matched · 1,062 prefixes → [keep] [sweep] [clear] · [claim…]`).
  Under the hood: collect the outermost matched nodes from the filtered tree
  (already computed), confirm, act.
- Feeder 2 — **row checkboxes**: leading checkbox column on the children table,
  header checkbox = all/none of the *visible page*; shift-click ranges. "Select
  matched" seeds the selection from the filter, so both feeders converge.
- **Bulk bar** (appears when selection non-empty): count + total bytes +
  `keep / sweep / clear` + `claim / assign to <user datalist> / release` + memo
  input. Confirm modal lists the first N prefixes + "…and K more".

**Apply = client-side expansion, v1.** POST the exact prefixes as an actions
array (chunked ~100/POST like the CLI's `batches()`); memo auto-prepends the
query (`filter:'grug/swarm'`) so the ledger records *why* 1,062 rows appeared.
Caps: warn > 500 prefixes, hard-stop > 5,000 (that's a rule, not a gesture —
point at `prefix_owners`).

**v2 — server-side pattern rows.** Store the *pattern* on the action
(`actions.pattern` as regex/glob), expand server-side against the current scan
into the per-axis prefix tables, and **re-expand on each new scan** (tombstone
machinery already anticipates this) so later-appearing dirs inherit the action.
That's what makes "sweep everything matching X" durable rather than
snapshot-frozen. The UI gesture stays identical; only the payload changes
(`{pattern: '/grug\\/swarm_/', …}` instead of N prefixes).

## Ledger → attribution bridge (required for owner actions to matter)

Owner/claim actions currently surface **only in mark mode** — webdata never
reads D1, so the group lens (Percy's gray) is untouched by claims. Bridge: the
daily job exports the folded owner-state as an attribution parquet
(`attr/attribution-ledger.parquet`, `source='ledger'`, lowest precedence —
parquet rows and yaml curation win) and passes it to `webdata` like the wandb
one. Then a UI claim genuinely re-colors the estate on the next snapshot —
closing the loop the lost-&-found design always implied.

## Order

1. Bulk bar over filter matches (client expansion) — the gesture Ryan asked for.
2. Row checkboxes + select-all/none, sharing the selection model.
3. Ledger→attribution bridge in the daily job.
4. Server-side pattern actions with per-scan re-expansion.

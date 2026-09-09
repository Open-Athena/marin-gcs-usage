# Index extras: one sidecar for provenance and checkpoint shape

Two asks land on the path index at once — inferred-ownership provenance (`assignment-provenance.md` Phase 2) and an ahead-of-time "checkpoint-shaped" flag per directory (`children-table-selection.md` §3). Both are small, per-path facts the daily index build knows and the site cannot cheaply recompute. Rather than two new parquet columns (a schema bump, `index_schema` pointer churn, every reader touched), they ride the mechanism `index-blob` already established: a JSON sidecar **beside** the tiers under `listing/<date>/index/<gen>/`, written by the daily job for new scans and backfilled for old ones by a CLI, opened by the site on demand and cached per isolate.

## The files

Two, beside the tiers under `listing/<date>/index/<gen>/`, so the small one (the flag) never waits on the big one (provenance):

```
ck.json    { "v": 1, "ck": ["marin-us-east5/checkpoints/isoflop/isoflop-3e+19-…", …] }   // checkpoint-shaped dirs (tens of thousands)
attr.json  { "v": 1, "attr": {                                                          // every ATTRIBUTING prefix (~200k)
               "marin-us-east5/checkpoints/isoflop": ["will-held", "user-prefix", null],
               "marin-us-central2/grug/moe-…":       ["calvinxu", "wandb-run", "entity/project/run_id"],
               … } }                     // [user, source, evidence]; source ∈ user-prefix | artifact-record | manual (rule) | wandb-run | wandb-config | executor-wandb | iris-path
```

Keys are index paths (bucket-relative, no `gs://`, no trailing slash) — the same key space as the tiers, so a row's provenance is the deepest `attr` key that is an ancestor-or-self of the row's path with the row's `usr` (the walk `deepest_lookup` does at build time, replayed on ≤ depth map probes). A node's `ck` is exact: the flag was computed over the dir's **full** child list at listing time, not the pixel-budgeted subtree the client sees.

`ck` rule (one place, `gcs_usage.prefixes.is_ckpt_shaped`): the dir's own last segment matches `(^|[-_.])(ckpts?|checkpoints?)([-_.]|$)`, or a direct child does, or ≥ 2 direct children match the step/checkpoint-number pattern (`sweep.ts`'s `CKPT_SEG_RE`, mirrored). The client's `looksCkpt` keeps the same rule as the fallback for scans without extras.

## Writers

- **New scans**: `webdata` writes both files next to `path-index.parquet` (`gcs_usage.extras.write_extras`) from data it already holds — `by_prefix` (user, source; evidence once the attribution rows carry it) and `dir_stats` (the parent/child relation for `ck`). Cost: one pass over the dir list, seconds.
- **Backfill**: `gcs-usage index-extras -P <path-index.parquet> [-a <attribution.parquet>…] [-i <identities.yaml>] [-o <dir>] <date>` rebuilds the same files for an archived generation from the attribution parquets and the floor-free path index (children = rows at depth+1 under the path; the floor-free index has every dir). Runs on the `mgu` node, not the laptop.

## Reader (site)

`functions/_lib/extras.ts`: `extras(env, date)` → the parsed files for the scan's current generation (`index_schema.dir`), memoized per isolate with the same TTL/`shared()` pattern as the owner-lens claims; `null` when the file is absent (older gens until backfilled) — every consumer degrades to today's behaviour.

- `buildView` stamps `k: 1` on nodes whose path is in `ck`, and `pv: [source, evidence?, prefix]` for the node's top owner (deepest attributing ancestor with that user). Both ride the subtree payload as optional `TreeNode` fields (`k`, `pv`).
- `/api/resolve` gains `owner.inferred` later; the client reads `pv` off the node it already has.
- `?by=<signal>` (Phase 2's "filter slices by source") becomes: keep the rows whose deepest attributing ancestor has that source — the same probe, no parquet column.

## Client

- `<OwnerFactChip>`: the assignee's chip plus a provenance mark — the assigner's avatar for a person, a source glyph for a signal — and one tooltip (`assignment-provenance.md` § Surfaces). Used by the mark panel's owner row, the children table's owner cell, and the treemap tooltip.
- `looksCkpt(node)` returns `node.k` when defined; the shape heuristic only otherwise.

## Not in this round

- Structured W&B evidence (`entity/project/run_id`) needs the attribution rows to carry it; the next `wandb-mine` writes it, and `extras.json`'s third slot is null until then.
- `/api/assignments` signal rows (Phase 2's `by_source` rollup) — once `extras` is on every scan, the rollup is a fold over `attr` × the tiers; separate change.

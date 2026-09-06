# `webdata` memory: why the daily job sits at the node ceiling, and how to get off it

**Status:** analysis 2026-09-05 (the 9/5 daily was OOM-killed, exit 137, at the `ptu` step). Proposals below, in order of leverage per effort.

## Where the 124 GiB goes

The 9/4 run (succeeded) and 9/5 (killed) had the same `[rss]` profile on the n2-highmem-16 (128 GiB; container asks for 124000 MiB):

| step | RSS peak | what it is |
|---|---|---|
| dir-stats, age-days, dir_attr | 85.5 GiB | DuckDB buffer pool at its cap (`DUCKDB_MEM=86GB`): the two object-level hash aggs (610M objects → 179M dir groups) fill it |
| access | +17 GiB → 102.8 | **`amap`, a Python dict of every dir read since logging began** — 64.3M entries (`SELECT DISTINCT bucket, path FROM access/agg/**`), ~300 B each |
| dir-agg, ptu | +20 GiB → 122–124 | DuckDB overshooting its own accounting on the 179M-row per-dir tables and the 220M-group `(path, depth, team, usr)` rollup agg |

The node has ~1 GiB to spare. That's been true since the 8/26 W&B re-mine grew the prefix map; the 9/1 near-miss and the 9/5 kill are noise around a fixed ceiling, not a trend.

## Does it creep? Two different answers

- **Dirs: flat.** `dir-stats` rows 179,033,012 (9/4) → 179,065,420 (9/5), +0.02 %/day. Path-index rows 220.06M (8/28) → 220.15M (9/3), +0.04 %/week. Bytes are *falling* (3364 → 3123 TiB over three weeks; the sweeps work) while objects rise slowly (586M → 610M over five weeks: big old checkpoints leave, many small files arrive).
- **The access dict: monotonic.** `amap` is keyed by *distinct dirs ever read since 2026-08-13*. Every new dir a training job touches adds an entry forever (the agg shards are never windowed at this join). 64M entries today, ~+0.5 GiB/day at the `access` step (102.3 → 102.8 GiB in one day). This is the only structure that grows regardless of what the sweep does, and it is entirely avoidable: the tree needs `a`/`ro`/`rb` for ~100k kept paths, and `ptu` already carries subtree-max `a`.

So without a fix, yes, it keeps creeping — but only because of `amap`.

## The shape of the fleet's directory tree

From `dir-stats` 2026-09-05 (`tmp/dir-shape-0905.txt`, computed on `mgu`):

- **171.6M of 179M dirs (96 %) contain exactly one object.** Mean 3.4 objects/dir fleet-wide.
- **`datakit/store*` is 164M dirs — 92 % of all dirs — for 165M objects and ~120 TiB (3.6 % of bytes)**, at 1.0 object per dir. `marin-eu-west4/datakit/store` alone: 141.7M dirs. Of that, `v0.1_20260518` 84.3M dirs / 22 TiB and **`_smoke_v0.1_20260518_mixed` 55.7M dirs / 11.4 TiB (a May smoke test)**.
- Everything else in the fleet is ~15M dirs. Depth 7–11 holds 165M of the 179M.
- The path index is "floor-free" by design (every ancestor path), so it inherits all of this: 220M rows, 7.6 GB, three sorted copies. Rows clearing a byte floor (9/4 index, `tmp/index-floor-0904.txt`):

| floor | rows | vs 220M |
|---|---|---|
| 1 MiB | 44.6M | 5× smaller |
| 64 MiB | 3.9M | 56× |
| 256 MiB | 2.0M | 110× |
| 1 GiB | 0.77M | 285× |
| 5 GB (`ABS_FLOOR`, the tree's) | 0.22M | 1000× |

The 9/4 `webdata` wall time (56 min) by step: object aggs 21.3 min · dir_attr 4.6 · access 2.9 · dir-agg 2.3 · ptu 4.6 · path-index + by-user/by-team copies 15.4 · tot/keep 3.4 · age 1.1.

## Options

Rough on-demand us-central1 rates: n2-highmem-8 ≈ $0.47/h, -16 ≈ $0.95/h, -32 ≈ $1.89/h, plus ~$0.08/h for the two local SSDs. The snapshot node runs ~2.1 h → ~$2.2/run today.

| # | change | memory | time | $ | effort | notes |
|---|---|---|---|---|---|---|
| 1 | **`amap` → SQL**: `a` from `ptu` (`max(a)` per kept path), `ro`/`rb` from `access_agg ⋈ keep` | −19 GiB now, and removes the only monotonic term | −1 min | 0 | ~2 h | do first; makes the current node comfortable again (~105 GiB peak, 20 GiB headroom) |
| 2 | drop the redundant `fp` column from `dir_stats`/`dir_agg` (it duplicates `bucket || '/' || dir`; ~100 B × 179M rows, twice) | per-dir tables ~40 % smaller inside the cap; lets `DUCKDB_MEM` come down | ~0 | 0 | ~1 h | the dir-cache parquet keeps `fp` for compatibility or is regenerated |
| 3 | **level-wise rollup** for `ptu` (the floor half is now the *coarse index tier* of `view-serving.md` §1 — a tier, not a loss; the floor-free `dirs` tier stays): aggregate depth 21 → 1, each level = own dirs at that depth ∪ the previous level rolled to its parent, *emitting only paths whose subtree clears an index floor* (256 MiB–1 GiB; anything ≤ 5 GB keeps `tree.json` byte-identical) | the 220M-group agg becomes per-level aggs (largest level: 59M paths, ~10 GB); `ptu`/`tot`/`keep` shrink 100–300×; DuckDB cap can drop to 48–60 GB | −~20 min (the 15-min index writes and 3-min `tot`/`keep` mostly vanish; the level-wise pass touches ~360M rows instead of ~1.7B exploded) | enables highmem-8 (−~$1/run) or 2× headroom on -16 | 1–2 days incl. a byte-identical `tree.json` check on a REPROC | trade-off: `/api/subtree` / `status` stop resolving paths below the floor (they fold into the parent). Those are 260 KB single-file dirs today; nothing mark-worthy lives there |
| 4 | **sweep `datakit/store*`** (owner decision; communal pool) | −92 % dirs, −27 % objects; the whole aggregation fits in ~16 GB | −~35 min | −$1/run and a 120 TiB bill line | a mark | the smoke store (55.7M dirs, 11 TiB, May) is the obvious first candidate. This is the app doing its job |
| 5 | lower `DUCKDB_MEM` and let the object aggs spill to the NVMe | −20–40 GiB | +10–20 min (external hash agg, 1.3–2× on that phase) | 0 | 0 | untested; the "tree_rows needs >44.7 GiB un-spillable" finding was the *old* ptu agg, which #3 replaces |
| 6 | n2-highmem-32 | +128 GiB headroom | 0 | +$2/run (+$60/mo) | 0 | keeps the creep; buys ~250 days |

CPU is not a lever here: the memory is group counts (dirs), not parallelism. The only CPU↔memory trade is #5 (spill), and NVMe makes that cheap-ish.

## Recommendation

1. Today: #1 (+#2), rebuild `:latest`, `REPROC=1 SNAPSHOT_DATE=2026-09-05` on the same node (the 9/5 listing + dir-cache are archived; ~50 min). Falls back to #6 for one run if it still trips.
2. This week: #3, verified byte-identical against the 9/5 REPROC's `tree.json`; then set `DUCKDB_MEM` to what the measured peak wants and decide -8 vs -16.
3. Raise #4 with the datakit owners via the map (`?o=unclaimed` → `marin-eu-west4/datakit/store/_smoke_v0.1_20260518_mixed`).

Scratch data: `tmp/dir-shape-0905.txt`, `tmp/index-floor-0904.txt`, `tmp/batch-task-logs-0905.txt`, `tmp/rss-0904.txt`; the parquets are on `mgu:/tmp/gcs/`.

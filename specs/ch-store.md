# ClickHouse store: one interval table behind the Worker

**Status:** built and measured on branch `ch-store` (code `[cloud]`: `dt_cloud.chstore`, `serve-query -e ch`, `ch-ingest`, `ch-bench`, `site/functions/_lib/queryBox.ts`; gcs's scripts: `job/ch-store.sh`, `job/ch-store/`, the optional stage in `job/run.sh`). Measured on gcs 2026-10-03 (§6). Builds on [serving-options] §4.6 (SCD-2) and §5–6 (suspend/resume, ClickHouse measured), and on [filter-query-service] §6.3 (`serve-query`), §7 (code layout) and §8 (serving tiers).

## 1. Goal

One ClickHouse database holds every scan of a deployment as **change records with intervals**: a row per version of a `(path, owner slice)`, valid from the scan that first saw it until the scan that first didn't. It subsumes the per-scan path store for serving: any scan's subtree, filter, diff and series answers come from it, and history costs about one scan plus the churn.

The Worker stays in front (auth, edge cache, fallback). It forwards the box-served routes to `dt-cloud serve-query -e ch`, which answers in the Worker's exact response shapes, and falls back to its own reads when the box errors, times out or declines.

One small VM, suspendable. RAM page cache → the VM's persistent disk (the big middle tier: every scan, every version) → GCS of record (the per-scan path stores, from which the store can always be rebuilt).

## 2. Schema (`dt_cloud.chstore.schema`)

### 2.1 Tables

- **`nodes`**: one row per version of a `(depth, path, usr)` owner slice: the path store's row (`kind`, `size`, `n_files`, `n_children`, `n_desc`, `mtime`, `mtime_mean`, `mtime_w`, `last_read`, `c2`–`c4`), the lowercase last segment `name`, the `parent` path (a column default), and the interval `[vf, vt)` as `DateTime`s (a scan id `2026-10-01` or `2026-10-01T0003` maps to its minute).
  - `ORDER BY (depth, path, usr, vf)`: a subtree at one depth is one primary-key range (`path ∈ [P/, P0)`, `'0'` sorting just past `'/'`), which is how the Worker's path store is laid out too.
  - `PARTITION BY toYYYYMM(vt)`: the **open** versions (still present at the newest scan) carry `vt = 2106-01-01` and so sit alone in partition `210601`; **closed** versions sit in the month of their `vt`.
  - `PROJECTION by_name (SELECT * ORDER BY name, depth, path, usr, vf)`: the filter's name → nodes access path. ClickHouse picks it whenever a query's `name IN (…)` prunes more marks than the base order (`EXPLAIN indexes = 1` on gcs: `ReadFromMergeTree (by_name)`, 8 of 29,069 granules for `safetensors`).
  - `PROJECTION by_parent (SELECT * ORDER BY depth, parent, size)`: a level's children over a size bound are one short range per parent.
  - `INDEX size_mm size TYPE minmax GRANULARITY 1`: prunes the granules of a level's read that hold no slice big enough.
- **`changes`**: every version once at its opening (`sign = 1`, `at = vf`) and once at its closing (`sign = −1`, `at = vt`), with the fields a delta needs (`kind`, `size`, `n_files`, `c2`–`c4`, `name`). `ORDER BY (at, depth, path, usr, sign)`, `PARTITION BY toYYYYMM(at)`. This is the **change-keyed access path** (§2.3).
- **`names`**: every lowercase segment name ever seen, `ReplacingMergeTree ORDER BY l`, with a `text(tokenizer = ngrams(3))` index: the filter's vocabulary. Append-only; a name whose paths are all gone stays (it then matches no node).
- **`scans`**: the ingested scans: `id` as the site names them, the source's version (1: a dirs-only v1 index; 2: a store generation), rows, versions opened and closed, seconds, the source.

Nullable source fields are stored as sentinels, since sort keys can't be `Nullable` and a null map costs a column: `usr = ''` (unattributed), `n_children` / `n_desc` / `mtime` / `last_read = −1`, and a missing `mtime_mean` as `0` with `mtime_w = 0` (the weight the Worker's `merge` skips).

### 2.2 Index types used, and why

- **Sparse primary key** `(depth, path, usr, vf)`: point reads (`(depth, path) IN (…)`: a view root, the diff's lookups, a series' versions) and the final read of a level's kept paths.
- **Partitions** on `toYYYYMM(vt)`: the as-of predicate `vf ≤ D AND vt > D` prunes every closed partition whose month is before `D`'s by the partition's min/max `vt`, so the latest scan reads the open partition only. Partitions are also how the open versions are replaced in one atomic step (§3).
- **Projection** `by_name`: a filter's candidates are the nodes of a few thousand names; sorted by name they are a handful of ranges. A projection, not a second table, because it is maintained with every part (the open partition's swap carries it) and the optimizer picks it per query.
- **Projection** `by_parent` plus the **`size` minmax index**: the threshold walk (plain views, a filter's per-root subtree, both diff sides) asks "the children of these kept parents whose bytes can clear `thr`". A path over `thr` has an owner slice over `thr / 64` (gcs holds at most 25 slices per path), so each level is `parent IN (…) AND size ≥ thr/64` on `(depth, parent, size)` — one short range per parent, the `mem` box's largest-first children in SQL — then the matching paths' slices summed from the base order. Before it, a level was an OR of thousands of `path` ranges evaluated per row: gcs's root view took 209 s, then 18 s with the `size` index alone, 1.3 s with both.
- **Text index** (`ngrams(3)`) on `names.l`: the `LIKE '%term%'` / `startsWith` / `endsWith` segment tests read only the granules holding the term's trigrams. Regex name tests (`match`) can't use it; they scan the 127M-name table (a few GB).
- **No skip index on `vf` / `vt`, no `set` or bloom indexes.** `vf` and `vt` are uncorrelated with the sort order (a granule holds paths from every era), so a `minmax` on them prunes nothing in the open partition; the partitioning does the interval pruning instead. Bloom filters on `name` are superseded by `by_name`.

### 2.3 Open versions and their closing, without heavy mutations

- Closed versions are written once, to the month partition of their `vt`, and never touched again.
- The open versions are **rewritten whole** at each ingest into a staging table of the same structure, then swapped in with `ALTER TABLE nodes REPLACE PARTITION ID '210601' FROM …`: a metadata operation (hard links), atomic for readers. No row is ever updated or deleted in place; the old open parts are dropped by the swap.
- The cost is writing the scan's rows once per ingest (the same order of work as loading the scan at all, §6.2). The alternative — appending new versions to the open partition and lightweight-deleting the closed keys — writes only the churn but leaves a delete mask on every open part; not built.
- **The change-keyed access path is a second table (`changes`), filled by the ingest's fan-out**, not a projection (a `vf`-ordered projection of the open partition would be rebuilt with it every day) nor a materialized view (the open rows are re-inserted whole daily, so a view can't tell new versions from carried ones). Rows closing in `(D1, D2]` are also the closed partitions with `vt` in that range. The Worker-shaped diff doesn't use it: its response is both sides' views at one threshold plus lookups, which the as-of reads answer in 1.6–1.8 s (§6.2); a "what changed under P" delta query reads `changes` in time proportional to churn. Not timed on gcs.
- The only mutation is on the recovery path: a re-run deletes a crashed attempt's appended rows (`vt` or `at` = the scan; a lightweight delete on one small partition).

### 2.4 A scan "as of D"

- A scan is the set of versions with `vf ≤ D < vt`.
- **Between scans** (a missing day), `D` resolves to the newest scan before it: its versions have `vf ≤ D` and `vt` ≥ the next scan. The box serves only ingested scan ids (another date is a 409 and the Worker answers), but the store itself answers any instant.
- **A partial scan** (a bucket's listing missing, a truncated index) is refused at ingest (§3), so it never becomes a version: as-of its date, the store shows the previous complete scan. The daily job already refuses to publish a scan with a bucket missing; this is the store's own guard.
- **A dirs-only (v1) scan** next to a store (v2) one: the diff folds the store side's objects into `(other)`, as the Worker does. The switch from v1 to v2 sources opens a new version of every directory (v2 adds child counts and stamps).

## 3. Daily ingest (`dt-cloud ch-ingest -d DATE [SRC]`)

SRC is the scan's path store `path` sort (a v2 generation's or a v1 index; default the newest generation under `gs://<bucket>/listing/<date>/`), read server-side from the box's `user_files` (`-F`) or streamed to the server as Arrow.

1. **Stage**: the scan's rows (`s = 1`) and the current open versions (`s = 0`, with their `vf`) into one table sorted by key.
2. **Pair**: an in-order `GROUP BY (depth, path, usr)` over the stage, cut into `threads × 4` key ranges (from the stage's primary index) run concurrently (one in-order aggregation is one thread). It inserts into a `Null` table whose four materialized views fan each key out: its open version for the scan (`vf` kept when unchanged, else the scan), the closed old version (`vt` = the scan) when it changed or vanished, and both as `changes` events. A source that lists one slice twice (v1 indexes hold duplicate unattributed rows) is merged the way the Worker's `merge` sums it.
3. **Swap**: the new open versions, plus a sentinel row (`depth = 0`, `vf` = the scan), replace the open partition.
4. **Record**: new names into `names` (from the day's opened versions only), the scan into `scans`.

**Idempotent and re-runnable:** a scan in `scans` is a no-op; one whose swap happened (the sentinel says so) is only recorded; anything else starts over after deleting what a crashed attempt appended. Scans are appended in order: an older scan than the open one is refused (a back-fill is a rebuild).

**Partial-scan guards:** a depth-1 root the open versions have and the scan lacks refuses the ingest (`--allow-drop` closes them), as does a scan of under half the open rows (`--force`).

**In gcs's daily job:** `job/run.sh` runs `ch-ingest` on the local `path` sort right after the index publish when `CH_STORE_URL` is set (off by default; `batch-submit.sh` forwards it on manual submits; the job image wasn't rebuilt). A failure warns and never fails the snapshot: the box 409s the scan it lacks and the Worker answers.

## 4. Serving (`dt-cloud serve-query -e ch <clickhouse-url>`)

`dt_cloud.chstore.serve` answers, in the Worker's response shapes, at any ingested scan:

- `/api/subtree`, plain: P's own slices give the threshold `P.b · minArea / (w·h)`; then one read per depth of the children of the paths kept one level up, grouped per path with `sum(size) ≥ thr(depth)`. Exact: bytes are monotone up the tree, so whatever clears a level's threshold has a parent that cleared the shallower one.
- `/api/subtree?q=`, filtered: the `mem` box's semantics (`dt_cloud.box.view`) in SQL on one session's temporary tables. Candidates by name (`names` → `by_name`), full-path flags, roots = the outermost of `pos ∧ ¬neg` and exclusions = the outermost of `neg` charged to their root, each one window pass in `k` order (the path with `/` as `\0`, so a subtree is contiguous and "nested" is a running max of `k ∥ 0xff`); net aggregates, synthesized ancestors, and the per-root attenuated subtree level by level. Root and match lists stream straight from ClickHouse (`toJSONString`).
- `/api/diff`, plain and filtered: both sides' views at one threshold, the Worker's walk, exact point lookups for the names one side lacks.
- `/api/series`: every version of P's slices (or of `paths=`, or the depth-1 rows for `split=roots`), summed per scan.

Statuses, as the `mem` box: `x-query-engine: box;dur=…` on every response; a scan not in the store 409; a series whose scans differ from the Worker's (`n` / `first` / `last`) 409; a lens 409; owner / class scopes 501; a regex no name filter plans 501; ClickHouse unreachable or failing 503. `/healthz` lists the scans.

**Parity** (`cloud/tests/test_chserve.py`, against `box-parity.json` from `site/functions/_lib/boxParity.test.ts`): 39 filtered subtrees and 3 filtered diffs (also equal to the `mem` box's), 12 plain subtrees, 12 diffs with changes (plain and filtered, against a second fixture generation `v2-search-b`), 7 series; all equal field for field less `tier` / `index` and the Worker's coverage flags. Ingest is tested to reproduce every scan exactly (`test_chstore.py`), including re-runs, crashes at each step, partial scans and v1 sources.

Deliberate differences from the Worker:

- Per-path aggregates always sum every owner slice; the Worker's `bysize` read drops a multi-slice path's slices under the threshold (3,254 such paths on gcs).
- Lookups are uncapped (`lookups_capped` false); the Worker stops at 240.
- On a v1 scan, `(other)`'s `f` counts every folded child; the Worker counts those its coarse tier holds.
- Filters: the `mem` box's (every match found, `folded` past 50K roots, the store-root match covers everything under it).
- No `pv` provenance in the box's trees: the Worker lays it on (§5).

## 5. Worker hand-off (`site/functions/_lib/queryBox.ts`)

- `QUERY_BOX_URL` set (and the primary store, no lens / owner / class scope): `/api/subtree`, `/api/diff` and `/api/series` ask the box after the edge-cache miss, with `Authorization: Bearer $QUERY_BOX_TOKEN` and a `QUERY_BOX_TIMEOUT_MS` budget (default 20 s).
- The box's 200 is the answer, edge-cached under the Worker's key; on a subtree the Worker lays the index extras' `pv` provenance on the tree. The box's 400 / 404 are the answer too.
- A 409 / 501 / 503 / any other status, an unreachable or stalled box, or a body past 64 MiB: the Worker's own read, marked `x-query-engine: worker;fallback=<why>`. A request the box isn't asked: `worker;box=skip`.
- A series carries the Worker's scan list (`n`, `first`, `last`); one whose tier-less scans add `meta.json` points stays the Worker's.
- Unset: nothing runs and no header is added; vitest (`functions/api/queryBox.test.ts`) checks no fetch happens and that every fallback body equals the no-box body.

## 6. Measured on gcs, 2026-10-03

**Setup:** one VM in us-east1-c beside the data, a 500 GB pd-ssd boot disk, ClickHouse 26.9.8 (stable) and `serve-query -e ch` in the gcs job image (`job/ch-store.sh`). 30 consecutive daily scans, 2026-09-04 … 10-03: 26 v1 (dirs only, 217M rows) and 4 store generations (09-30 on, 778–782M rows: objects and dirs, owner slices). The bulk load ran on n2-standard-32; the box was measured as n2-custom-8-16384 (8 vCPU, 16 GB), then n2-standard-4. The Worker side is prod gcs.oa.dev from a laptop, uncached (`ch-bench -s 1`: the same jittered `minArea` on both sides). Records: `tmp/ch-store/` (not committed). Spend: about $8 of VM and disk.

### 6.1 Ingest

| scans | box | per scan | opened / closed versions |
|---|---|---|---|
| v1 → v1 (25 days) | n2-standard-32 | 210–280 s (load 25 s, stage 17 s, pair 165–180 s) | 0.07–5.8M each way (median ≈ 0.5M, 0.2%) |
| v1 → v1 (2 days, first build) | 8 vCPU / 16 GB | 666–972 s | — |
| v1 → v2 switch (09-30) | n2-standard-32 | 1,047 s | 777M opened, all 217M v1 rows closed |
| v2 → v2 (10-01, 10-02) | n2-standard-32 | 809–839 s (load 140 s, stage 65 s, pair 600 s) | 2.9–3.1M opened, 1.3–1.6M closed (0.4% / 0.2%) |
| v2 → v2 (10-03) | **8 vCPU / 16 GB** | **3,824 s** (load 440, stage 520, pair 2,855 at 2 ranges at once) | 2.7M / 1.3M |

- **A daily v2 ingest is ~14 min on 32 vCPU and ~64 min on the 8 vCPU / 16 GB box.** Pairing four key ranges at once on 16 GB ran out of memory twice (each re-run recovered, as designed); two at once fit. Whole-open-partition rewriting is the cost (§2.3).
- **The first v1 build showed 80% of rows "changed" daily** — the job's float sums jitter `mtime_mean`'s last bits. Versions now compare it to the second; churn fell to the real 0.05–2.7%.
- Ingest reproduces every scan exactly in tests; on gcs the open partition held 781,501,174 rows for 10-03, the source parquet's row count.

**On disk, 30 scans** (after `by_parent`): `nodes` 46 GiB (the open versions with both projections and the `parent` column; before them the open partition was 18.5 GiB and the closed ones 7.9 GiB, almost all the v1 era closed at the switch), `changes` 10.3 GiB (of which ~10 GiB is the first scan's and the switch's full openings), `names` 3.9 GiB (131M names). **About 61 GiB in all.** Per added v2 day, before `by_parent`: about 170 MB (closed versions ≈ 85 MB, change events ≈ 80 MB); with the closed versions also in both projections, roughly 0.25 GB.

### 6.2 Serving, through `serve-query`'s HTTP (8 vCPU / 16 GB)

Warm = median of two passes after a first; cold = ClickHouse's caches and the OS page cache dropped before the request. Seconds.

| request | box warm | box cold | Worker (uncached) | bodies |
|---|---:|---:|---:|---|
| subtree, store root (10-03) | 1.32 | 1.86 | 2.52 | differ at multi-slice dirs (a) |
| subtree, a bucket | 1.20 | 1.62 | 2.37 | (a) |
| subtree, a deep dir | 1.03 | 1.26 | 2.73 | equal |
| subtree, root `depth=1` | 0.06 | 0.09 | 0.90 | equal |
| subtree, root, older scan (09-30, v2) | 1.45 | 2.02 | 3.15 | (a) |
| subtree, root, older scan (09-20, v1) | 1.38 | 1.87 | 2.59 | equal but `(other)`'s `f` (c) |
| subtree, bucket, older scan (09-20, v1) | 1.45 | 1.87 | 1.71 | (c) |
| diff, root, 1 day | 1.73 | 2.24 | 3.05 | (a), (b) |
| diff, root, 7 days | 1.81 | 2.37 | 3.64 | (a), (b) |
| diff, bucket, 1 day | 1.58 | 1.88 | 3.03 | (a), (b) |
| diff, bucket, 7 days | 1.82 | 2.33 | 4.41 | (a), (b) |
| diff, root, `summary=1` | 0.03 | 0.10 | 3.25 | equal |
| filtered diff, root, `checkpoints`, 1 day | 16.7 | 17.9 | 13.0 | box exact, 51 MB (every root listed) |
| filtered diff, bucket, `step-`, 1 day | 10.7 | 11.7 | 15.2 | box exact, 28 MB |
| filtered diff, root, `safetensors`, 7 days | 5.4 | 6.1 | 503 (failed) | box exact, 19 MB |
| series: root, bucket, deep dir, `split=roots` | 0.03–0.05 | 0.05–0.08 | 0.15–0.29 (edge-cache hits) | equal on all 30 common scans (d) |

(a) The box sums every owner slice of a path; the Worker's `bysize` read drops slices under the threshold. On the root view 103 named nodes differ, and in every one the box holds each of the Worker's slices, equal, plus the dropped ones. (b) The Worker caps lookups at 240 (`lookups_capped`), and then names real directories `removed` (e.g. `swarm_fisher_dsp_d512_000241…`: Worker `removed`, box `changed` 168.8 → 10.5 GB). (c) Documented in §4. (d) The Worker's series has 65 scans; the box only its 30, so it 409s the Worker's `n`/`first`/`last` and the Worker answers.

**The 105-query filter bench** (scan 10-01, `probe -Q` against the phase-1 truth): **103 exact, 0 FAIL, 2 declined** (503: `step` and `.npy` at the store root, 18.7M / 20.4M roots, run ClickHouse out of memory at 16 GB and at 32 GB; the Worker answers them, flagged). Latency, full responses (tree, every root listed): warm p50 / p90 / max 3.2 / 8.6 / 39 s; first pass 3.5 / 14.3 / 53 s; cold 4.9 / 11.6 / 55 s. The Worker (§6.1 of [filter-query-service]): 6.9 / 12.1 / 16.0 s with 71 of 105 flagged. Bench B's ClickHouse ([serving-options] §6.1) answered in 0.20 / 1.46 / 4.33 s warm, but computed roots and totals only; the time here is the response — the per-root tree, the ancestors and lists of up to 100 MB.

**On 4 vCPU / 16 GB (n2-standard-4):** plain views 10–25% slower (root 1.53 warm / 2.28 cold), diffs 3.1–3.4 s before the concurrent-sides change, series unchanged, the filter bench 3.3 / 8.8 s warm (p50 / p90). Memory: the server holds ~1.1 GB between queries; the heaviest answered query peaked at 5.3 GB.

### 6.3 Wake-up (8 vCPU / 16 GB, pd-ssd, idle 60 s)

| | down call | up call | → ssh | → `/healthz` | → first answer (store root) | that answer | then a filter |
|---|---:|---:|---:|---:|---:|---:|---:|
| suspend → resume | 41.7 s | 11.0 s | +4.4 s | +5.8 s | +9.4 s (≈ 20 s from the call) | 2.4 s | 4.5 s |
| stop → start | 27.4 s | 9.7 s | +17.2 s | +23.1 s | +26.5 s (≈ 36 s from the call) | 2.2 s | 4.3 s |

The disk-backed box needs no reload: a stop/start costs ~36 s to a first answer against the `mem` box's 140 s ([serving-options] §5.2), and a resume ~20 s against its 30 s. Suspending writes 16 GB, not 64.

### 6.4 Verdict and cost

- **The smallest box that holds up: n2-standard-4 (4 vCPU, 16 GB), about $142 a month on demand**, plus the disk (§6.1: ~61 GiB for 30 scans growing ~0.25 GB a day; a 200 GB pd-balanced ≈ $20, a 500 GB pd-ssd ≈ $85). 8 vCPU / 16 GB (n2-custom, ≈ $245) is 10–25% faster. Both serve plain views and diffs faster than the uncached Worker, and exactly.
- **But not the daily ingest:** 64 min on 8 vCPU, competing with serving. Run it on a bigger box or Batch (14 min on 32 vCPU), or change the open-version design (§7).

## 7. What remains before production

- **Ingest cost:** the open partition is rewritten daily (781M rows). Appending new versions and lightweight-deleting closed ones (or a separate `open_keys` set) writes only the churn; or ingest on Batch into a store the box attaches. Don't record full openings in `changes` on a first scan or a format switch (10 GiB here).
- **Filters at scale:** the two 20M-root answers decline; the per-root ancestors' `sumMap` aggregation needs bounding (or the contract caps `matches` / `matched`, [filter-query-service] §9). The full lists also make filtered diffs 11–17 s.
- **Owner / class scopes and the user lens** are 501 / 409 (the Worker answers).
- **Where the store lives:** a persistent VM disk is the middle tier; a back-up / rebuild path from the scans of record (`ch-ingest` replays them; ~2.5 h on 32 vCPU for 30 days).
- **The Worker's hand-off** is built and tested but untried end to end (the optional dev-stack check through a quick tunnel wasn't run), and the job's `CH_STORE_URL` stage needs a box reachable from Batch, credentials and an image rebuild.
- **Series** only match the Worker once the store holds every scan the index knows (back-fill the older ones).

[serving-options]: serving-options.md
[filter-query-service]: filter-query-service.md

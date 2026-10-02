# cw storage: consolidate before purging

cw scans every 6 h since 2026-10-02 (`infra/gcp` cron `0 */6 * * *`). Every scan keeps full, independent copies of its data, though only ~3–5% of rows change in 12 h (measured below; mostly objects added and removed, almost none changed in place). This spec makes per-scan storage cheap first (dedupe, then store deltas), and only then adds a retention schedule.

Storage cost is the motivation, not a hard limit. D1 is at 264 MB of its 10 GB after ~110 scans; storage grows ~6.7 GB per scan (~800 GB/month at 4 scans/day).

## Today's footprint (measured 2026-10-02, scan `2026-10-02T1201`)

| Where | What | Per scan |
|---|---|---|
| GCS `cw-l2/<scan>/` | `marin-us-east-02a.parquet` (the layer-2 rollup) | 1.30 GB |
| | `hero-checkpoints.parquet` | 0.01 GB |
| | `index/path-index.parquet` | 1.31 GB |
| | `index/path-index-bysize.parquet` | 1.39 GB |
| | `index/age-pyramid-*.parquet` (8 bins) | 0.06 GB |
| | `*.groups.{json,parquet}` sidecars | ~0.01 GB |
| R2 (`publish-r2`, what the site serves) | the two path-index sorts + age pyramids + sidecars | ~2.7 GB |
| | **total** | **~6.8 GB** |

GCS `cw-l2/` holds 103 scans, 180 GiB in all. The over-time interval groups (`job/cw-overtime.sh`, 16 scans per group) already hold the whole history compactly; the size-over-time chart reads them.

Who reads what:
- The site reads the path-index tiers from R2 only. The GCS copies are the job's writes, the source `publish-r2` copies from, and what the over-time builder reads (details and the full reader table in phase 1). There is no per-request GCS fallback: a scan whose publish failed is left out of the picker.
- The layer-2 rollup is read by sweep execution (`dt-cloud plan-sweep manifest` reads `cw-l2/<date>/<bucket>.parquet` for a plan's scan) and by reindex one-shots (`cw-reindex*`). It is row-for-row the path store's `path` sort: `2026-10-02T1201`'s two rollups hold 61,964,152 + 1,052,524 = 63,016,676 rows, exactly the `path` sort's count.

### Where the 180 GiB is (measured 2026-10-02)

Per-scan sizes jumped on 2026-10-01: that is when the path store (`index_schema.version` 2: objects as rows, `path` + `bysize` sorts) replaced the dirs-only index (~0.3 GB/scan). Of the 103 indexed scans, 4 are store generations; the ~6.7 GB/scan figure applies from there on. Breakdown of the whole prefix, split by whether D1's `index_schema.dir` names the generation dir:

| Bucket | What | GB |
|---|---|---|
| GCS `cw-l2/` (193 GB) | rollups (`<scan>/<bucket>.parquet`, 136 files) | 63.5 |
| | `path-index*` in a D1-pointed generation | 57.6 |
| | `age-pyramid-*` in a D1-pointed generation | 44.4 |
| | **generations no D1 pointer names** (left behind by the 9/16–9/21 reindexes, in 77 scans) | **26.3** |
| | over-time groups (6) + `over-time-dev/` | 1.5 |
| R2 `cw-l2/` (134 GB) | the same pointed generations (path-index 57.6 + age 44.4 + over-time 1.3) | 103.3 |
| | **the same unpointed generations** (`publish-r2` copies the whole `cw-l2/<scan>/` prefix, every gen) | **26.3** |
| | a stale **v1** rollup for `2026-09-30T1203` (published before that scan's `recompress`; GCS holds the 1.16 GB v2) | 4.4 |

`index-gc` deletes only D1 rows, never files, so every reindex leaves its previous generation in both buckets.

## Phase 1: one serving copy

The path-index tiers exist twice, in GCS and in R2. After `publish-r2` succeeds (and its served copy is verified), the GCS tiers are redundant except as a fallback that only matters for the newest scan.

- After a scan's R2 publish is verified, the job deletes the previous scans' GCS tiers older than N scans (N ≈ 4, one day), keeping the rollup. Or, instead of deleting them, a GCS lifecycle rule moves `cw-l2/*/index/` to Coldline after 7 days. That costs ~5× less, deletes nothing, and still allows a re-publish.
- Check first that nothing but the fallback reads GCS `cw-l2/*/index/`: the site's store seam (`_lib/index.ts`), `index-sync`, `index-gc`, `warm-cache`, the over-time builder.
- Saves ~2.75 GB/scan (≈40%).

### Who reads the GCS tiers (checked 2026-10-02)

| Reader | Reads | From | Needs the GCS tiers once `publish-r2` succeeded? |
|---|---|---|---|
| Site: `/api/subtree`, `/api/diff`, `/api/series`, `/api/path-index`, search, `/api/age-pyramid`, `/data/*`, `/v1/files` | D1 pointer dir → `path-index[-bysize]`, age pyramids, over-time groups, sidecars; snapshot JSONs | **R2 only.** One global target (`storeTarget`: `STORE_ENDPOINT`/`STORE_BUCKET` set in prod and preview); there is no per-scan R2-vs-GCS choice and no automatic fallback. The scan picker is (snapshot dirs in R2) ∩ (D1 `path` pointers), so a scan whose publish failed is simply not offered | No |
| D1 `index_schema.dir` / `index_row_groups` | bucket-relative keys (`cw-l2/<scan>/index/<gen>`), no scheme | provider-agnostic: resolved against whatever `storeTarget` names | No. GCS copies matter only for a rollback that unsets `STORE_*` (the "fallback" is this manual rollback, not a per-request one) |
| `dt-cloud index-sync` (job 4b, reindex) | this run's new generation (footers; writes `.groups.{json,parquet}` beside it) | GCS mount | Only within the run, before 4c |
| `dt-cloud index-gc` (cw runs it without `-r`) | D1 rows | D1 | No (`-r`'s cold-footer existence check defaults to GCS, unused on cw) |
| `dt-cloud publish-r2` (job 4c, `cw-publish-submit.sh`) | lists and copies `cw-l2/<scan>/` | GCS → R2 | It is the re-publish source; needed again only to repair R2 |
| **`dt-cloud over-time-groups`** (job 4d, `cw-overtime.sh`) | `/gcs/<data>/<pointer dir>/path-index.parquet` for each of the group's 16 scans, when the 16th lands | **GCS mount** | **Yes: the last 16 scans (4 days at 6 h)**, until it reads R2 or keeps its own per-scan roll-ups |
| `cw-reindex.sh` | rollups; its skip check globs GCS `index/*/age-pyramid-1d.parquet` | GCS | Rollups yes; tiers only for the skip check (a missing tier means "re-index"). It does not `publish-r2`; a reindex must be followed by `cw-publish-submit.sh` |
| `plan-sweep manifest` (`cli.py` ~1106) | `/gcs/<data>/cw-l2/<date>/<bucket>.parquet` | GCS | Rollup only |
| `cw-path-store-phase0`, `cw-recompress-submit.sh` | rollups | GCS | Rollup only |
| `warm-cache`, `probe`, `healthcheck`, `cw-digest` | the site's API; `warm-cache`/`cw-digest` list snapshot JSONs in GCS | site (R2) / GCS `snapshots/` | No |
| meta self-scan (`cw-meta-run.sh`) | object listings of the GCS and R2 buckets (metadata only) | both | No |

Two side findings:
- `job/cw-overtime.sh` calls `dt-cloud publish-r2 "$gid"` without `-L`, so each sealed group copies that scan's rollup (~1.3 GB) to R2, where nothing reads it. Should be `publish-r2 -L`.
- `publish-r2` copies every generation under `cw-l2/<scan>/`, pointed or not (the 26.3 GB above). It should copy only the dirs D1 points at.

### Phase 1 recommendation: delete after publish, not Coldline

1. **Now, no behavior change needed first:** delete the 26.3 GB of unpointed generations in both buckets (a `dt-cloud index-gc --files` pass: list `cw-l2/<scan>/index/*/`, keep every dir any `index_schema` row names, dry-run first), and drop the stale R2 v1 rollup (4.4 GB). Make `publish-r2` copy only pointed dirs, and make `index-gc` remove a superseded generation's files (GCS + R2) after the pointer flips.
2. **Then delete-after-publish for the GCS tiers**, once `over-time-groups` stops reading them: point it at R2 (`r2://` through `blobfs`, which the meta store already uses), or at a per-scan `(depth, path, b, o)` dir roll-up written while the scan's tiers are still on local disk (~7.5M dir rows, on the order of the 0.15–0.25 GB a whole over-time group takes). After that, job step 4c deletes the GCS `index/<gen>/` of scans older than N = 4 whose R2 copy `publish-r2` verified (size + md5, which it already compares). Until then, N must be ≥ 16.
3. **Not Coldline.** Coldline bills a 90-day minimum: phase 4 thins 6-hourly scans to 12-hourly after 7 days and to daily after 28, so most of these objects would be deleted (and billed) well inside 90 days. And a cold copy of something R2 already holds buys little: the R2 copy is the durable one, and a rollback to GCS serving needs only the newest scans. If a second copy of old tiers is wanted at all, the rollup is it (the tiers rebuild from it with `cw-reindex.sh`).

Saves ~2.75 GB/scan going forward (40%) plus 26 GB in each bucket once.

## Phase 2: the rollup

The 1.3 GB rollup is kept per scan for sweeps and reindexes.

- Check that it's already v2 (`disk-tree listing-format`) and on the zstd level the path store uses; recompress if not (`disk-tree recompress`).
- Move rollups older than 7 days to Coldline (lifecycle rule) unless a pending sweep plan pins them. Reads are rare (a sweep, a reindex), so Coldline's retrieval fee is fine.

### Findings (2026-10-02)

- **All 136 rollups (103 scans × 1–2 buckets) are already v2 under zstd** (`disk-tree listing-format` over every `cw-l2/<scan>/<bucket>.parquet`, footer-only). zstd level 3 everywhere (`listing_format.ZSTD_LEVEL`, the same constant the path store's `duckdb_codec` uses). `recompress` would skip all of them: nothing to gain.
- Examples: `2026-08-19T0303` 21.5M rows, 0.33 GB; `2026-09-10T0001` 44.8M rows, 0.67 GB; `2026-10-02T1201` 62.0M rows, 1.30 GB (237 row groups). The rollup grows with the bucket, not with format.
- What's left in a v2 rollup (`2026-10-02T1201`, compressed column bytes): `path` 828 MB, `size` 146, `mtime_mean` 135, `mtime` 114, `parent` 68, the rest < 5. `parent` is derivable from `path`, and `mtime_mean` equals `mtime` on every object row; a v3 dropping both would save ~15%. Small next to phases 1 and 3.
- The path store's `path` tier carries the same redundancy plus a stored `sum_storage_class_id_1` (117 MB, equal to `size` on every cw row; the rollup already drops it as `implied`), ~18% of that tier between the three.
- **Sweep pins:** D1 has one plan (closed, no items, one stage batch) and no `deletion_runs`. Nothing pins a rollup today. How a pin arises: a dry run (`dispatchPlan`) records the scan it used in `deletion_runs.scan`, and the real run must use that same scan. So the rule is: keep (in Standard) the rollup of any scan named by a `deletion_runs` row whose plan has no finished real run yet, or whose `undo_deadline` hasn't passed.

### Phase 2 recommendation

No recompress. Lifecycle to Coldline only for rollups that will be kept ≥ 90 days. Under the phase 4 schedule that is the daily survivors past day 28, so the rule is "age ≥ 30 days → Coldline", applied after phase 4's thinning is in place (until then, every rollup survives, and 30 days is still the right age). Before phase 4 lands, a sweep's pinned rollup is always recent (dry runs pick a current scan), so pinning and Coldline don't conflict. A GCS lifecycle rule can't express "top-level `*.parquet` only", so it must wait until phase 1 has removed the old `index/` dirs (or use `matchesPrefix` per scan, which doesn't scale).

**The bigger lever: the rollup duplicates the `path` sort.** Same rows (see the count above), and the sweep manifest reads only `path`, `size`, `mtime`, `kind` (`sweep._eligible_query`), all of which the `path` sort has (paths bucket-prefixed; the sort's `(depth, path)` order even lets a plan's prefixes prune row groups). Once phase 3 makes the path store cheap per scan, a separate 1.3 GB rollup per scan becomes the largest per-scan cost (5.2 GB/day at 6 h). Point `plan-sweep manifest` (and `cw-reindex.sh`, which needs `parent`, derivable, and nothing else the sort lacks) at the `path` sort, and keep rollups only for keyframe scans, or not at all.

## Phase 3: deltas instead of full copies

The big lever. Store a scan as a **keyframe** (a full path store) plus **deltas**: the rows added, removed or changed (size, mtime, counts) since the previous scan. A keyframe every K scans (K = 4, daily, to start) bounds how many deltas a read has to merge.

Questions to settle with measurements before building:
1. **Churn at 6 h:** rows changed per scan, by tier. Dir rows change whenever any descendant does, so the dir churn is higher than the object churn (every ancestor of a changed object). Measure on two adjacent real scans (on Batch or node `mgu`, not the laptop).
2. **Reader cost:** the site does row-group range reads planned from D1 footers. A delta-backed scan needs the keyframe's groups plus the deltas' matching rows, merged by path. Is that one extra range read per request, or more? Does it fit the Workers CPU and memory budget (see the 503 = isolate-memory lesson)?
3. **Or materialize lazily:** keep full path stores only for keyframes and recent scans. Rebuild any other scan's store on demand from keyframe + deltas, or from the over-time interval groups (which already have every path's per-scan sizes), and cache it in R2 with a TTL. Reads of old scans are rare, so a slow first view may be acceptable.
4. **D1:** per-scan footer rows (`index_row_groups`) for delta-backed scans: footers of the deltas only, plus a pointer to the keyframe.

Deliverable for this phase: a measured churn table and a recommendation (merged reads, lazy materialization, or both), then the build.

### Measured churn (2026-10-02)

How: `dt-cloud churn A B` (`dt_cloud.churn.scan_churn`) full-outer-joins two scans' `path` sorts on `(depth, path)`, one depth at a time, compares every shared column (`size`, `mtime`, `mtime_mean`, `n_files`, `n_children`, `n_desc`, `created`, `last_read`, `sum_storage_class_id_1`), and writes the delta: B's added and changed rows plus key-only tombstones for removed ones, an `op` column, sorted `(depth, path)`, zstd 3 (the store's codec), default row groups. One GCP Batch job (`job/cw-churn-submit.sh`, n2-standard-8, `cw-churn-20261002-214205`) staged the four store generations from GCS onto local disk: ~40 s per pair, 14.3 GB peak RSS under a 22 GB DuckDB limit; 6 min end to end. Only 4 store generations exist (since `2026-10-01T0003`) and they are 12 h apart, since 6-hourly scans started later on 10-02.

| A → B | Span | Objects +added / −removed / ~changed | Dirs + / − / ~ | Rows churned (of A's) | Delta | Delta, objects only | Full `path` sort (B) |
|---|---|---|---|---|---|---|---|
| `10-01T0003` → `10-01T1201` | 12 h | +1,856,126 / −945,090 / ~12,306 | +27,869 / −413,738 / ~1,844 | 3.26M (5.45%) | 73.6 MB | 70.1 MB | 1.23 GB |
| `10-01T1201` → `10-02T0001` | 12 h | +1,499,367 / −202,146 / ~12,220 | +27,758 / −1,009 / ~1,487 | 1.74M (2.90%) | 48.2 MB | 47.2 MB | 1.27 GB |
| `10-02T0001` → `10-02T1201` | 12 h | +1,484,286 / −73,870 / ~22,018 | +48,214 / −1,133 / ~3,751 | 1.63M (2.65%) | 41.3 MB | 39.8 MB | 1.31 GB |
| `10-01T0003` → `10-02T0001` | 24 h | +3,154,936 / −946,679 / ~12,329 | +55,321 / −414,441 / ~1,882 | 4.59M (7.68%) | 112.1 MB | 107.8 MB | 1.27 GB |
| `10-01T0003` → `10-02T1201` | 36 h | +4,588,694 / −970,021 / ~12,406 | +103,522 / −415,561 / ~1,930 | 6.09M (10.20%) | 149.1 MB | 145.1 MB | 1.31 GB |

What the numbers say:
- **Churn is adds and removes, not edits.** Objects changed in place (size or mtime) are 12–22K per 12 h, < 0.05% of objects; the bulk is new objects (~1.5–1.9M per 12 h, checkpoints) and deletions (TTL'd `tmp/` and the like). The spec's assumption that "dir churn exceeds object churn" doesn't hold: changed dir rows are 1.5–3.7K per 12 h, because new objects land under few, deep, new dirs.
- **A 12 h delta is 3–6% of a full sort's bytes**; objects are ~95% of it. Dir rows are cheap to keep in the delta, so readers needn't re-aggregate.
- **Deltas against a fixed keyframe grow sub-additively**: 73.6 → 112.1 → 149.1 MB at 12/24/36 h, vs 73.6 → 121.8 → 163.1 MB for the chained 12 h deltas. A cumulative delta (every scan vs its keyframe) costs no more than a chain and needs one merge instead of up to K−1.
- **Spikes happen.** The over-time groups (below) show one boundary (`2026-09-16T0001` → `T1201`) where 4.5M of 10.3M dir paths (43%) disappeared at once: a bulk delete. A delta must fall back to a new keyframe when it outgrows a fraction of the keyframe (say 25%).

Dir churn per scan boundary across all six sealed over-time groups (`dt-cloud over-time-churn`, `group_churn`: each group's `__scan_lo`/`__scan_hi` runs, dir rows only, 16 scans 12 h apart). The 9/21–9/29 group's numbers match, boundary for boundary, an independent row-streaming count of the same file from the laptop.

| Group (last scan) | Dir paths | Dir churn per boundary: median (min–max) | Median changed / added / removed |
|---|---|---|---|
| `2026-08-22T1201` | 5.8M | 1.42% (0.38–7.12%) | 724 / 67,060 / 391 |
| `2026-08-28T1233` | 6.6M | 0.49% (0.35–4.39%) | 1,601 / 28,061 / 307 |
| `2026-09-06T0001` | 11.1M | 3.54% (0.23–11.54%) | 940 / 250,579 / 2,775 |
| `2026-09-14T0001` | 11.1M | 0.45% (0.02–1.61%) | 828 / 25,697 / 572 |
| `2026-09-21T0001` | 11.2M | 0.84% (0.03–77.4%: the 9/16 bulk delete) | 1,154 / 29,918 / 1,721 |
| `2026-09-29T0001` | 7.6M | 1.18% (0.25–2.90%) | 1,138 / 76,569 / 4,684 |

At 6 h a boundary's delta should be roughly half a 12 h one (adds dominate and arrive steadily); this is an estimate until two adjacent 6 h store generations exist (`job/cw-churn-submit.sh` re-measures in one run).

### Phase 3 recommendation: cumulative deltas + merged reads for the `path` sort; no lazy materialization

**Layout.** A keyframe once a day (K = 4 at 6 h), aligned with phase 4's long-term "daily, indefinitely" grain: the scans phase 4 keeps forever are exactly the keyframes, and every non-keyframe scan it keeps is ≤ 28 days old. Each non-keyframe scan stores one **cumulative delta vs its keyframe** (sorted `(depth, path)`, dir and object rows, tombstones). Estimated per-day cost of the `path` sort at 6 h: 1.3 GB keyframe + ~3 deltas of ~0.04–0.11 GB ≈ 1.5 GB, vs 5.2 GB for four full sorts (~3.5× less). With phase 1 and the rollup change from phase 2, a day of scans drops from ~27 GB (4 × 6.8) to ~3 GB (`path` 1.5 + keyframe `bysize` 1.4 + age pyramids 0.25), or ~4.5 GB keeping the keyframe's rollup.

**`bysize`: keyframes and the newest scan only.** A delta can't be merged into a size-bucket sort cheaply: a tombstone or a shrunk row has to suppress the keyframe's old row, and those are found by path, not by size bucket. Non-keyframe scans answer their size-threshold reads from `path` sort + delta (more decode, but these are older, rarely viewed scans). The newest scan keeps a full `bysize` until the next scan supersedes it.

**Reader (merged reads).** A request's plan (depth rect, path range, `b_max` threshold) runs against the keyframe's D1 footer rows and the delta's: one extra set of range reads per request, against a file 3–11% the size. The merge is a linear two-way merge on `(depth, path)`, delta wins, tombstones drop the row; CPU and memory scale with the delta groups selected, which are few for any subtree. One soundness rule: **the delta's rows must carry the keyframe's old `size`** (`size_prev`; tombstones too) and the delta's `b_max` stat must cover `max(size, size_prev)`. Otherwise a row that shrank below the threshold is pruned from the delta read while the keyframe still returns its old, larger value.

**D1.** A delta's footer is ~200–750 row groups at 8K rows (1.6–6.1M rows), vs ~7,700 for a full sort. A delta-backed scan's `index_schema` row names its delta dir plus a `keyframe` column naming the keyframe scan. At 4 scans/day that is ~17K footer rows/day instead of ~62K (two full sorts × 4).

**Diffs get cheaper too.** `/api/diff` between a scan and its keyframe is the delta itself (filtered to the requested subtree), and between two scans on the same keyframe it is a merge of two deltas. No full-sort join, and full diffs are the slow path today.

**Why not lazy materialization.** (1) The over-time groups hold dir rows only (objects were never series), so they can't rebuild a path store's object rows. (2) Keyframe + delta → full sort is a ~40 s DuckDB job on Batch, too heavy for a Worker (a 1.3 GB output), so "on demand" means a Batch round-trip of minutes on first view. (3) With daily keyframes kept forever and deltas only within 28 days, every retained scan is directly servable by a merged read, so nothing needs materializing. Keep materialization as an escape hatch: if merged-read latency on some view proves bad, `dt-cloud` can write a full sort for a scan from keyframe + delta (the same join `churn` runs), cached in R2 with a TTL.

**Before building:** (a) measure one 6 h pair; (b) prototype the merged read in `_lib/index.ts` against a real keyframe + delta, cold, at the root and at a deep subtree (`dt-cloud probe -c` style), checking Worker CPU time and memory; (c) decide the spike threshold (keyframe when the delta exceeds ~25% of the keyframe's bytes).

## Phase 4: retention (later)

Only after phases 1–3, and only on a go, because it deletes data. The schedule (Ryan, 2026-10-02):
- every scan for the last 7 days (6-hourly);
- every 12 h for the 3 weeks before that;
- daily before that, indefinitely.

Rules:
- Never delete a rollup a pending or recent sweep plan points at.
- Purging a scan also drops its D1 rows (`index_row_groups`, `index_schema`), its R2 objects, its KV/edge-cache entries, and its `scans.json` entry, in one job step, logged.
- Size-over-time keeps full history (it reads the interval groups). Treemaps and diffs exist for retained scans only; the scan picker must not offer purged ones.
- Dry-run first (`-n`), printing what would go, with sizes.

## Not in scope

- Spot VMs: compute is small at 4 scans/day; staying on on-demand Batch (2026-10-02 decision).
- Faster listing (more parallel lister workers): separate; listing is ~41 min of a ~45-min scan and grows with object count.

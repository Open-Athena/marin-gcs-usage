# cw storage: consolidate before purging

cw scans every 6 h since 2026-10-02 (`infra/gcp` cron `0 */6 * * *`). Every scan keeps full, independent copies of its data, though well under 1% of paths change between scans. This spec makes per-scan storage cheap first (dedupe, then store deltas), and only then adds a retention schedule.

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
- The site reads the path-index tiers from R2. The GCS copies are the job's writes and the site's fallback when `publish-r2` fails (`job/cw-run.sh` step 4c).
- The layer-2 rollup is read by sweep execution (`dt-cloud` `sweep` reads `cw-l2/<date>/<bucket>.parquet` for a plan's scan) and by reindex one-shots (`cw-reindex*`).

## Phase 1: one serving copy

The path-index tiers exist twice, in GCS and in R2. After `publish-r2` succeeds (and its served copy is verified), the GCS tiers are redundant except as a fallback that only matters for the newest scan.

- After a scan's R2 publish is verified, the job deletes the previous scans' GCS tiers older than N scans (N ≈ 4, one day), keeping the rollup. Or, instead of deleting them, a GCS lifecycle rule moves `cw-l2/*/index/` to Coldline after 7 days. That costs ~5× less, deletes nothing, and still allows a re-publish.
- Check first that nothing but the fallback reads GCS `cw-l2/*/index/`: the site's store seam (`_lib/index.ts`), `index-sync`, `index-gc`, `warm-cache`, the over-time builder.
- Saves ~2.75 GB/scan (≈40%).

## Phase 2: the rollup

The 1.3 GB rollup is kept per scan for sweeps and reindexes.

- Check that it's already v2 (`disk-tree listing-format`) and on the zstd level the path store uses; recompress if not (`disk-tree recompress`).
- Move rollups older than 7 days to Coldline (lifecycle rule) unless a pending sweep plan pins them. Reads are rare (a sweep, a reindex), so Coldline's retrieval fee is fine.

## Phase 3: deltas instead of full copies

The big lever. Store a scan as a **keyframe** (a full path store) plus **deltas**: the rows added, removed or changed (size, mtime, counts) since the previous scan. A keyframe every K scans (K = 4, daily, to start) bounds how many deltas a read has to merge.

Questions to settle with measurements before building:
1. **Churn at 6 h:** rows changed per scan, by tier. Dir rows change whenever any descendant does, so the dir churn is higher than the object churn (every ancestor of a changed object). Measure on two adjacent real scans (on Batch or node `mgu`, not the laptop).
2. **Reader cost:** the site does row-group range reads planned from D1 footers. A delta-backed scan needs the keyframe's groups plus the deltas' matching rows, merged by path. Is that one extra range read per request, or more? Does it fit the Workers CPU and memory budget (see the 503 = isolate-memory lesson)?
3. **Or materialize lazily:** keep full path stores only for keyframes and recent scans. Rebuild any other scan's store on demand from keyframe + deltas, or from the over-time interval groups (which already have every path's per-scan sizes), and cache it in R2 with a TTL. Reads of old scans are rare, so a slow first view may be acceptable.
4. **D1:** per-scan footer rows (`index_row_groups`) for delta-backed scans: footers of the deltas only, plus a pointer to the keyframe.

Deliverable for this phase: a measured churn table and a recommendation (merged reads, lazy materialization, or both), then the build.

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

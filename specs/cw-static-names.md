# cw's static name index

Status: base generation `2026-10-09cw` **paused mid-build** (2026-10-09 15:00 UTC, handed off; see "Where the build stands"). The pipeline and the readers are generic, on `cloud` (`cw-static`). This branch (`cw-static-site`, off `cw-s3`) carries cw's driver, its dev-stack config and an opt-in per-scan stage. Not deployed.

The map's `?f=` filter and `/names` answer a literal from the static name index on R2: exact, at any drill path and on any scan of the generation, with no path-store walk (specs/architecture/static-name-search.md, specs/static-append.md). This is cw's copy of it.

## Inventory (2026-10-09 13:00 UTC)

| | |
|---|---|
| Scans | 130 with a `path` sort, `2026-08-19T0303` → `2026-10-09T1201` (5 older snapshots have none and are skipped) |
| Cadence | every 6 h, ids to the minute: 51 days, 34 with 2 scans, 13 with 4, 1 with 5 |
| Where | `gs://oa-gcs-usage-dvx/cw-l2/<scan>/index/<gen>/path-index.parquet`, newest index generation per scan (also on R2 under the same keys) |
| Formats | 99 v1 (dirs only, `b`/`o`, 164–369 MB, ~4.5–8.4M rows) through `2026-09-30T1203`; 31 v2 (objects as rows, no `usr` column, 1.19–1.60 GB, 60–76M rows) from `2026-10-01T0003` |
| Bytes | 69.9 GB of sources (25.4 GB v1, 44.5 GB v2) |

## Profile

`STATIC_NAMES_PROFILE=cw` (`dt_cloud.static_profile_examples.CW`):

- layouts: `cw-l2/{id}/index/{gen}/path-index.parquet`;
- data bucket `oa-gcs-usage-dvx`, scratch `oa-gcs-usage-scratch` (gcs's: one data bucket, generations side by side under `static-names/<gen>/`, cw's named `…cw`);
- generation `2026-10-09cw`;
- R2: bucket `oa-cw-s3-usage-index`, under the same keys `static-names/2026-10-09cw/`;
- accounts: the stages run as `gcs-usage-job` (the account with the scratch bucket); the R2 copy runs as `cw-s3-job` with the R2 key cw's scan job already uses for `publish-r2` (Secret Manager `cw-s3-r2-{endpoint,access-key-id,secret-access-key}`). No new key, no new grant.

## Build

`job/static-names.sh` (cw's copy of gcs's driver: the cw profile in every task, `r2-batch` as `cw-s3-job` without the scratch mount):

```bash
export STATIC_NAMES_PROFILE=cw G=2026-10-09cw
dt-cloud static-names scans > scans.json                        # laptop, footers only
dt-cloud static-names ranges -k 64 gs://…/static-names/$G/scans.json > ranges.json
SPOT=1 job/static-names.sh run intervals 16 -g $G -C -n 4      # coalesced versions straight from the scans
dt-cloud static-names plan-shards -g $G -H chist -n 50000000 -t T
SPOT=1 job/static-names.sh run suffix-map 16 -g $G -C -n 4 && SPOT=1 job/static-names.sh run shards T -g $G
dt-cloud static-names sidecar -g $G
SPOT=1 MODULE=static_catalog job/static-names.sh run census T -g $G -f 50000
dt-cloud static-names catalog members -g $G -V 100000
SPOT=1 MODULE=static_catalog job/static-names.sh run short 16 -g $G -n 4
SPOT=1 MODULE=static_catalog job/static-names.sh run answers T -g $G
SPOT=1 MODULE=static_catalog job/static-names.sh run assemble 1 -g $G
job/static-names.sh r2-batch $G
```

## Where the build stands (handoff)

Done (GCS `gs://oa-gcs-usage-dvx/static-names/2026-10-09cw/`):

| Stage | Output | Run |
|---|---|---|
| `scans.json` | 130 scans, `layouts` recorded | laptop, footers |
| `ranges.json` | k = 64 | laptop, 8 s |
| `intervals -C` | `cintervals/` 1.31 GB: **93,208,980 coalesced versions, 3,139,635,637 suffix rows**; `chist/`, `cdone/` ×64 | `sn-cw-civ-131614`, 16 spot tasks, 44.7 min wall (spot capacity gave it 1–2 VMs at a time), 4,742 task-s (≤ 180 s a range) |
| `shards.json` | 65 shards ≤ 50M rows, 16 task groups | laptop |
| `suffix-map` + `shards` | `sx/` **65 files, 118.7 GB (37.8 B/row)**; shuffle 108.5 GB in `gs://oa-gcs-usage-scratch/static-names/2026-10-09cw/` (7-day expiry) | `sn-cw-map-140619` 4.9 min, `sn-cw-shards-141420` 20.5 min, 16 spot tasks each |

Batch so far ≈ 6 spot n2-highmem-16 VM-hours ≈ **$1.5**.

**Next**, in order (commands under "Build"):

1. `sidecar`.
2. `census` → `members -V 100000` → `short` ∥ `answers` → `assemble`.
3. Verify. Terms come from `job/static-names/cw-catalog-terms.py` over the census. Run `catalog brute` (Batch, one task per scan) on, e.g., `2026-10-09T1201`, `2026-10-05T0601`, `2026-10-01T0003` (the first v2), `2026-09-30T1203` (the last v1) and `2026-08-19T0303`. Then `catalog query -x` and `catalog verify`.
4. **R2 copy: get the go first.** The shards are 118.7 GB, 3.8× gcs's bytes per row. cw's paths are long (≈200 characters) and the suffix sort scatters them, so `path` dominates. GCS→R2 egress at ~$0.12/GB is **≈ $14** on top of the build, which is past the ~$15 budget. Levers before copying:
   - zstd level 9+ (~10% on gcs);
   - a smaller row group;
   - storing `path` as a parent-dictionary id.

   The last is a format change in readers and writers. Ask "main" before spending.
5. Drill (`FILTER_STATIC_HEAVY`): not started. At gcs's ratio (~23 B of drill per suffix row) it would be ~70 GB more, ≈ $8 egress. Until it exists, heavy literals (catalog members, 1–2 characters) keep the path-store fallback.
6. Per-scan runs: cw has scans after the base (`2026-10-09T1801`, …). `runs add -c <latest>` catches up once the base is on R2, run with `STATIC_NAMES_PROFILE=cw` (needs the IAM decision below to run from cw's own job).

## Turning it on (dev)

`site/wrangler.toml` `[env.preview]` on this branch: `FILTER_STATIC = "1"`, `NAME_SUMMARY_STATIC = "1"`, `STATIC_GEN = "2026-10-09cw"`, and an `[[env.preview.r2_buckets]]` binding `INDEX_R2` → `oa-cw-s3-usage-index`. Production is unchanged.

## Per-scan runs

`job/cw-run.sh` stage 4f, opt-in (`STATIC_NAMES=1`): `STATIC_NAMES_PROFILE=cw dt-cloud static-names runs add -c "$SNAP_ID"`. Before it can be turned on:

1. The job's account (`cw-s3-job`) must be able to submit Batch jobs that run as `gcs-usage-job` (`iam.serviceAccountUser` on it), or the profile's `sa` becomes `cw-s3-job` with `objectAdmin` on the scratch bucket. Either is an IAM grant: Ryan's call.
2. The stages' code: the job image must carry this `dt_cloud` + `pyrmts` (set `STATIC_NAMES_IMAGE` to it), or `STATIC_NAMES_SRC` points at a staged tree.
3. Each run is ~4 scans a day here, so the binary counter compacts at level 5 after 32 scans (8 days).

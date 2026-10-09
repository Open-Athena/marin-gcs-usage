# cw's static name index

Status: base generation `2026-10-09cw` **built and verified** (2026-10-09 18:40 UTC). The light index (suffix shards + catalog) is **on R2** (`oa-cw-s3-usage-index`, `static-names/2026-10-09cw/`). The drill is built and verified on GCS only; it was **not copied** (≈ $17 of egress), pending the hex-run rule (specs/static-hex-runs.md), which shrinks it. The pipeline and the readers are generic, on `cloud` (`cw-static`). This branch (`cw-static-site`, off `cw-s3`) carries cw's driver, its dev-stack config and an opt-in per-scan stage. Not deployed.

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

## Build commands

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
# verify: terms from the census, brute force per scan, the reader's answers
job/static-names/cw-catalog-terms.py <local copy of catalog/census/> 100000 > job/static-names/cw-catalog-terms.txt   # then gcloud storage cp → $G/catalog-terms.txt
SPOT=1 MODULE=static_catalog job/static-names.sh run brute 5 -g $G -d <scan> … -t /gcs/oa-gcs-usage-dvx/static-names/$G/catalog-terms.txt
dt-cloud static-names catalog query -g $G -x -d <scan> … -t job/static-names/cw-catalog-terms.txt > catalog-answers.jsonl
dt-cloud static-names catalog verify -V 100000 brute-answers.jsonl catalog-answers.jsonl
# light index → R2, as cw-s3-job (≤ 8 streams: R2 throttles more), `scans.json` last
for o in sx/ sidecar shards.json catalog/ scans.json; do job/static-names.sh r2-batch $G -o $o -w 8; done
# drill (GCS)
SPOT=1 MODULE=static_roots job/static-names.sh run measure-short 16 -g $G -n 16      # ∥ digest
SPOT=1 MODULE=static_roots job/static-names.sh run digest 16 -g $G && dt-cloud static-names roots alias-plan -g $G
SPOT=1 MODULE=static_roots job/static-names.sh run build 16 -g $G -R 100000 -K 256   # then check every canonical shard's rollups-index exists
dt-cloud static-names roots short-plan -g $G -r 250000000
SPOT=1 MODULE=static_roots job/static-names.sh run short-map 16 -g $G -n 16
PARALLELISM=11 SPOT=1 MODULE=static_roots job/static-names.sh run short-reduce 11 -g $G -n 11 -R 100000 -K 256
SPOT=1 MODULE=static_roots job/static-names.sh run index 1 -g $G -R 100000 -K 256
# drill verify: measure the case terms' shards, cases, the reader's answers, brute force
SPOT=1 MODULE=static_roots job/static-names.sh run measure N -g $G -o <shards>
dt-cloud static-names roots drill-cases -g $G -n 1 -R 100000 -s <shards> -t job/static-names/cw-drill-terms.txt > cases.jsonl
dt-cloud static-names roots drill-query -g $G -c cases.jsonl -d <scan> … > answers.jsonl            # cases + answers → $G/verify/
SPOT=1 MODULE=static_roots job/static-names.sh run drill-brute 5 -g $G -d <scan> … -c /gcs/…/verify/cases.jsonl -k /gcs/…/verify/answers.jsonl
dt-cloud static-names roots drill-verify brute.jsonl answers.jsonl
```

## The build

All on GCS `gs://oa-gcs-usage-dvx/static-names/2026-10-09cw/`, spot n2-highmem-16 in us-east1:

| Stage | Output | Run |
|---|---|---|
| `scans.json` | 130 scans, `layouts` recorded | laptop, footers |
| `ranges.json` | k = 64 | laptop, 8 s |
| `intervals -C` | `cintervals/` 1.31 GB: **93,208,980 coalesced versions, 3,139,635,637 suffix rows**; `chist/`, `cdone/` ×64 | `sn-cw-civ-131614`, 16 tasks, 44.7 min wall (spot capacity gave it 1–2 VMs at a time) |
| `shards.json` | 65 shards ≤ 50M rows, 16 task groups | laptop |
| `suffix-map` + `shards` | `sx/` **65 files, 118.7 GB (37.8 B/row)**; shuffle 108.5 GB in the scratch bucket (7-day expiry) | 4.9 + 20.5 min, 16 tasks each |
| `sidecar` | `sidecar.parquet` 14.9 MB, 383,288 row groups | laptop, 32 s |
| `census` (floor 50K) ∥ `short` | `catalog/census/` 259 KB; short events in the scratch bucket | 2.5 min ∥ 1.1 min, 16 tasks |
| `members -V 100000` | **11,926 long members** (5,127 of 3 characters, mostly hex trigrams; longest 46) | laptop, 12 s |
| `answers` → `assemble` | `catalog/cells.parquet` **1,302,085 cell rows (11,926 long + 1,803 short members), 10.8 MB, 318 row groups**; `index.parquet` 25 KB | 4.9 min (16 tasks) → 0.3 min |
| drill `digest` → `alias-plan` | 11,926 members → **7,734 canonical root sets**, 4,690,684,782 canonical roots (7,264,942,572 with aliases) | 3.6 min, 16 tasks |
| drill `measure-short` → `short-plan` → `short-map` → `short-reduce` | 1,803 short literals, 2,639,880,477 roots, 11 q-groups | 7.7 + 14.4 + 20.8 min |
| drill `build` (long) → `index` | `drill/`: long roots 4,690,684,782 (84.5 GB, 18 B/row), short roots 2,639,880,477 (57.7 GB), rollups 7.3M + 2.8M rows; **142.3 GB** in all | 20 min (16 tasks) + a 3-shard rerun (below) |

**Drill incident: a silent hole, fixed.** Spot preemptions at ~17:00 killed the tasks building long shards 35, 63 and 64 mid-shard. The retried tasks found those shards' claims (`claims/drill-build/s####` in the scratch bucket) younger than the 90-minute lease and skipped them, so the job **succeeded** without them, and `index` wrote a `meta.json` over 62 of 65 shards (286M roots missing; kept as `tmp/` evidence only). Caught by comparing the built roots per shard with `drill/aliases.parquet`. Fixed by `build -o 35,63,64 -l 600` and re-running `index`, which **rewrote** `drill/*-index*.parquet` and `drill/meta.json` on GCS. Nothing under `drill/` had been copied or served. Generic follow-ups for the drill code: a queue task must not exit while another task's claim is unfinished, even if it is unexpired (wait, or fail the task so the job fails), and `index` must refuse unless every shard holding a canonical member has its `rollups-index` file.

## Verification

- **Catalog + static reader** (`verify/verify-catalog.json`): `catalog query -x` against `catalog brute` straight from each scan file. **510 / 510 (term, scan) pairs equal**. 102 terms (`job/static-names/cw-catalog-terms.txt`: 20 named, 15 short, 3 inside hashes, 9 bucket literals, 40 hash-sampled members across lengths, 15 hash-sampled non-members just under V) × 5 scans: `2026-10-09T1201`, `2026-10-05T0601`, `2026-10-01T0003` (the first v2), `2026-09-30T1203` (the last v1), `2026-08-19T0303`. 58 terms answered from the catalog, 43 statically, 1 absent. Every static range ≤ V (max 96,471 rows); ≤ 106,496 rows / 2.9 MB read per term (laptop → GCS 0.6–37 s per term over 5 scans).
- **Drill** (`verify/verify-drill.json`, `verify-drill2.json`): `roots drill-query` (the Worker's dispatch over the two-level indexes) against `roots drill-brute` from each scan file, the same 5 scans. **1,405 / 1,405 (case, scan) equal**: 281 cases, 159 read from roots and 122 from rollups, and 842 non-empty answers. Terms: `job/static-names/cw-drill-terms.txt`, including `acts`, `uet` and `work`, whose canonicals are in the three rebuilt shards. The first 218 cases were re-run against the re-cut index: still 1,090 / 1,090. Max read 114,688 rows / 3.0 MB.

## R2

Light index copied as `cw-s3-job` with cw's R2 key (`job/static-names.sh r2-batch 2026-10-09cw -o …`, prefixes in order `sx/`, `sidecar`, `shards.json`, `catalog/`, `scans.json`). That's 137 objects, 118.7 GB, ~22 min. The first `sx/` attempt at `-w 16` failed: R2 answered `ServiceUnavailable: Reduce your concurrent request rate for the same object` on multipart parts. It reran clean at `-w 8`, skipping what was there. A final dry run lists only `drill/` (156 objects, 138.4 GB) as left to copy.

## Cost

| | |
|---|---:|
| Batch (all stages above, spot) | ≤ 38.6 VM-h counting tasks × job wall (an upper bound), ≈ **$6–8** |
| GCS → R2 egress, light index | 118.7 GB (+ the failed attempt's partial parts) ≈ **$14–15** |
| **Total, one-time** | **≈ $21–23** |
| Not spent: the drill copy | 142.3 GB ≈ $17 |

**Is it one-time?** The build and this first copy are one-time. Turning on per-scan runs costs, per 6-hourly scan:
- Batch: about 1.5 spot VM-h ≈ $0.40, from gcs's measured run at cw's size;
- GCS → R2 egress for the run: 0.4–3 GB ≈ $0.05–0.36;
- every tier merge re-uploads its rows: ~5 uploads per row over 32 scans, so ≈ 5× a run's bytes amortized ≈ $0.25–1.8;
- each compaction re-uploads the base (~$14 every 32 scans = 8 days).

That's ≈ **$3–9/day** for cw at today's format, before any drill runs. The hex-run rule (~3.7× fewer rows), merging on a CoreWeave node and less frequent merging cut it (specs/static-append.md, "Egress: merges re-upload").

## Why 3.8× gcs's bytes per row

Footer stats of three cw shards against three gcs shards, same writer (zstd default, 8K rows per group):

| | cw | gcs |
|---|---:|---:|
| bytes per row | 36.5–45.9 | 2.7–14.1 |
| `path` | 28.4–31.4 B (69–78%); 199–220 B raw, 6.4–7.7× compression | 1.2–9.4 B; 119–133 B raw, 13–96× |
| `s` | 5.8–10.5 B; 25–37 B raw, 3.5–4.4× | 0.02–2.2 B; 12–26 B raw, 12–945× |
| `size` | 1.2–2.3 B | 0.7–1.7 B |
| `usr`, `depth`, `vf`, `vt`, `n_files` | < 1.5 B together | < 0.7 B |

Over sampled coalesced-version ranges: mean name 45 characters (gcs 16), mean path 172 (gcs 119), and **79% of cw's suffix rows come from names holding a run of ≥ 16 hex digits** (gcs: 1.1%). Those are content-addressed `_objects/<64-hex>` stores and ray-log ids, most under two `users/` trees. A hash's ~60 suffixes are random hex strings, so in `s` order each row sits next to unrelated files. Neighbours share almost nothing for zstd to find, and `path` repeats the whole hash (~32 B of entropy) on every one of its suffix rows. Row-group size and `usr` (all empty on cw) don't matter, and the timestamps are small.

Levers, measured on 8 blocks of 64K rows from each of three cw shards (`s0010`, `s0030`, `s0052`; bytes per row):

| Writer | s0010 | s0030 | s0052 |
|---|---:|---:|---:|
| today (zstd default) | 39.08 | 47.28 | 34.81 |
| zstd 9 | 36.17 (−7.4%) | 43.67 | 33.03 |
| zstd 19 (~150× slower to write) | 33.60 (−14%) | 40.55 | 30.48 |
| zstd 9 + DELTA_BYTE_ARRAY / 16K–32K groups / dictionaries | within ±1% of zstd 9 | | |
| **split `path`** → `parent` (dict) + `head` (name before the suffix) + rare case `tail`, zstd 9 | **26.58 (−32%)** | **31.69 (−33%)** | **24.31 (−30%)** |

Nothing inside the current format moves it more than 14%. The split `path` is a reader/writer format change, planned for the next compaction (specs/static-append.md, "Shard size: the split `path`"). The hex-run rule removes the rows themselves (specs/static-hex-runs.md). The copy went ahead at today's format.

## Turning it on (dev), for the cw-s3 session

Light literals (≥ 3 characters, range ≤ V) are answered statically. Catalog members (1–2 characters and literals over V) are answered statically by `/names` and `/api/name-summary` (per bucket). The **map filter's drilldown for members stays on the path-store fallback**: the drill is not on R2, and `FILTER_STATIC_HEAVY` stays unset.

1. Merge this branch into `wt/cw-s3` (`cw-s3` has moved on since the fork: scan-run records in `cw-run.sh`; expect that file to conflict with stage 4f).
2. `site/wrangler.toml` `[env.preview]` (already on this branch): `FILTER_STATIC = "1"`, `NAME_SUMMARY_STATIC = "1"`, `STATIC_GEN = "2026-10-09cw"`, and `[[env.preview.r2_buckets]]` `binding = "INDEX_R2"`, `bucket_name = "oa-cw-s3-usage-index"`. Leave `FILTER_STATIC_HEAVY` unset. Production is unchanged.
3. `site/deploy --dev` → dev.cw-s3.oa.dev. Check with a signed-in tab:
   - `/api/name-summary?date=2026-10-09T1201&name=checkpoint`: static, and it should equal `verify/catalog-answers.jsonl`'s `checkpoint` on `2026-10-09T1201`;
   - `/api/name-summary` for `.json`: catalog;
   - the map at `?d=261009-1201&f=qwen`: `tier: static…`;
   - `f=.json` falls back (a member: path store).
   Scans after `2026-10-09T1201` are `scan-not-indexed` until the per-scan runs below.
4. Per-scan runs (catch-up, then every scan): `STATIC_NAMES_PROFILE=cw dt-cloud static-names runs add -c <latest scan id>`. Run it from a laptop or VM with the `gcs-usage-job`-capable credentials the build used until the IAM decision below; `-n` first. It writes runs and manifests under `static-names/2026-10-09cw/` and copies them to R2 (manifest last). With `drill-runner`'s drill stage merged, set cw's profile `drill` off (no base drill on R2 to append to) until a cw drill is copied.

## Per-scan runs

`job/cw-run.sh` stage 4f, opt-in (`STATIC_NAMES=1`): `STATIC_NAMES_PROFILE=cw dt-cloud static-names runs add -c "$SNAP_ID"`. Before it can be turned on:

1. **IAM, Ryan's call.** Two options:
   - (i) `cw-s3-job` gets `iam.serviceAccountUser` on `gcs-usage-job`, so it can submit Batch jobs that run as it. Nothing else changes, but cw's job can then act with gcs's data-bucket rights.
   - (ii) The profile's `sa` becomes `cw-s3-job`, with `objectAdmin` on the scratch bucket and write access to `static-names/2026-10-09cw*` in the data bucket. Narrower, but two new grants.

   (ii) is the tighter blast radius. Either way, the R2 copy already runs as `cw-s3-job` with cw's existing R2 key.
2. The stages' code: the job image must carry this `dt_cloud` + `pyrmts` (set `STATIC_NAMES_IMAGE` to it), or `STATIC_NAMES_SRC` points at a staged tree.
3. Each run is ~4 scans a day here, so the binary counter compacts at level 5 after 32 scans (8 days).

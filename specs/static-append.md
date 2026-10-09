# Static name search: per-scan append

Status: 2026-10-09 appended, verified (175/175 against brute force), on R2 and served by the dev site. Code on `cloud` (2026-10-09: the pipeline, the reader behind `FILTER_STATIC` / `NAME_SUMMARY_STATIC`, default off); the entry point `dt-cloud static-names runs add` (`static_runner.py`; it replaces `job/static-daily.sh` on `gcs-static`). Not yet scheduled; not on prod.


## State (2026-10-09 15:00 UTC)

- **First sub-daily run, gcs `2026-10-09T1236`** (`runs add -t …`, staged `cloud` tree `9d852323…` + pyrmts 541bc8e as `STATIC_NAMES_SRC`, `STATIC_NAMES_PROFILE=gcs`): prepare 12 s, append 6.4 min (16 spot tasks × 16 ranges), shards ∥ catalog 10.4 min, publish 8.4 min, R2 2.4 min. The run: 85,705,437 suffix rows, 2.37 GB. Verify (the 30 terms of `deltas/2026-10-09/terms.txt`) ran after: see `deltas/2026-10-09T1236/verify.json`.
- **Incident:** the counter merged `deltas/2026-10-09` (which carries the drilldown's `drill/`) with T1236 into `deltas/2026-10-09_2026-10-09T1236`, which had no `drill/` and no `catalog/meta.json`. Published at 14:32:33, it made prod's heavy filter 500 for a few minutes and dropped 10-09's drill coverage. Fix, by hand at 14:35–14:39: `deltas/2026-10-09T1236` copied to R2, then `manifests/2026-10-09T1236.json` **rewritten** to list the two level-0 runs (the merged version is kept at `gs://oa-gcs-usage-dvx/static-names/2026-10-08c/manifests-superseded/2026-10-09T1236.merged.json`). That rewrite broke rule 2 below, once, on the coordinator's call. The merged dir `deltas/2026-10-09_2026-10-09T1236/` on GCS is orphaned: nothing lists it, and it was never copied to R2. It is kept, not deleted.
- **Fixed in code** (`9a4e5c29`):
  - the counter never merges a run holding a tier it can't carry (`pinned_runs`, `MERGED_ENTRIES`);
  - a merged run's catalog gets its `meta.json`;
  - `publish` refuses a manifest whose runs lack a reader file (`RUN_FILES`).
- **Rules:**
  1. Write a manifest only after every file of every run it lists exists on that store, R2 included. The runner copies the runs first and the manifest last, in one job chained with `&&`.
  2. Manifests are immutable. Never rewrite a published key; fix forward with a new manifest instead (the next scan's).
- Next gcs scan: `2026-10-10` (cron 07:00 UTC) → `runs add -c 2026-10-10`. With 10-09 pinned, the counter merges T1236 + 10-10 into level 1.

## Why

The static name index (`specs/architecture/static-name-search.md`) answers `/names` and the map's `?f=<literal>` filter from R2 alone, but only for the scans of its generation. `2026-10-08c` ends at 2026-10-08. Every newer scan gets a 400 from `/api/name-summary` and the filter falls back to the per-scan path-store walk. Rebuilding a whole generation per scan (~$10 Batch, 136 GB re-copied to R2, ~$16 egress) and keeping a full copy per scan is the cost this design avoids.

The goal: fast, exact `?f=` text search on the main map (treemap, table, diff) at any drill path and any date, so each scan has to join the index when it lands.

## Shape

The base generation stays immutable. Each scan `D` adds one small **run** under new keys. A small immutable **manifest** per scan lists the base plus the live runs. Readers query the base and every listed run in parallel and merge the results. Under a binary-counter policy, runs merge into bigger runs, so a lookup reads at most ⌊log₂ n⌋ + 1 runs after n scans. Periodically, the base and its runs compact into a new generation.

Nothing is overwritten or deleted in place. A day's outputs are new keys, and the manifest that publishes them is a new key too.

## Per scan D: the stages

### 1. Coalesced append, per key range (256 ranges, Batch)

The base generation's coalesced versions (`cintervals/r####.parquet`, `CINTERVAL_SCHEMA`) are runs of `(depth, path, usr)` keyed on `size` and `n_files` alone (`ANSWER_COLS`). A rebuild from the scans with only those change columns gives the same files byte for byte (`build_range(coalesced=True)`, `test_coalesced_from_scans_equals_two_step`). So the append can skip the full intervals:

- `pyrmts.intervals.append_intervals(prev = the range's open coalesced versions, new = scan D's merged rows in the range projected to (depth, path, usr, size, n_files), key_cols = KEY_COLS, state_cols = ANSWER_COLS, stamp = D)`.
- **Only open versions are state.** Closed versions never change, and `append_intervals` only reads `prev`'s open rows and the newest `vf`. The state carried from day to day is therefore `copen/r####.parquet`: the open coalesced versions, sorted `(depth, path, usr, vf)`. That is about one row per live key of the newest scan, not the whole history.
- Outputs per range:
  - `cdelta/r####.parquet`: `CINTERVAL_SCHEMA` + `op int8` (1 = opened at D, `vt` = OPEN; −1 = closed at D, the version's own `vf`/`size`/`n_files` with `vt` = D), sorted `(depth, path, usr, vf, op)`. This is exactly `coalesce_append`'s `cdelta` and the rows `delta_sql` returns.
  - the next `copen/r####.parquet`;
  - `dhist/r####.parquet`: the delta's suffix rows per three-character prefix, for the shard plan.
- The first append reads `prev` from the base's `cintervals/` (`WHERE vt = OPEN`).
- **Where the state lives:**
  - `copen/` goes to the scratch bucket (`gs://oa-gcs-usage-scratch/static-names/<gen>/state/<id>/copen/`, beside `state/<id>/done/r####.json`, each range's marker, written after its `copen`). The bucket has a 7-day age-based delete rule and no soft delete.
  - `cdelta/` goes to the data bucket under the run (`gs://oa-gcs-usage-dvx/static-names/<gen>/deltas/<id>/cdelta/`), kept permanently: it is small (21.9 MB on 2026-10-09), and tier merges write new run dirs without touching the per-scan ones.
- **Only the newest complete state is kept** (`runs prune -g GEN -d ID`, the chain's last stage). A scan's state is a full copy of the open versions (4.4 GB on 2026-10-09), and the next append reads only the newest. Once `state/D/` is complete (every range's `copen/r####.parquet` and `done/r####.json`, for all `ranges.json` `k`) and D's run is published (`manifests/D.json`), `prune` deletes `state/<prev>/` for every prev < D. It never deletes before D is complete, so the only complete state is never deleted, and a rerun of D's append still finds its `prev`. It deletes nothing outside the scratch bucket's `static-names/<gen>/state/` (an object under there whose dir isn't a scan id is an error), and it is idempotent: a rerun finds nothing before D. Without it, the 7-day rule alone would keep about a week of states (~31 GB).
- **A lost state is rebuilt, not stored** (`runs rebuild-state -g GEN -d ID -m MOUNT`, per task like `append`). The open versions after D are the base's `cintervals` open rows, minus every version a run's `cdelta` closed (`op` −1), plus every version one opened (`op` 1), over D's manifest's runs oldest first. A version's identity is `(depth, path, usr, vf)`, and one opened by a run has that run's `vf`, past every base version's. The rebuilt `copen` matches the appended one byte for byte (`test_rebuild_open_equals_the_appended_state`). This is the recovery when the newest state expires: the 7-day rule removes it if the chain stalls for a week. It reads the base's `cintervals` (4.72 GiB over 256 ranges for `2026-10-08c`) plus the runs' `cdelta`s (~22 MB a scan), about what one scan's `append` reads, and writes the same 4.4 GB `copen` the append would. That is cheap enough as a recovery path, so no state needs to be kept beyond the newest.

### 2. Delta suffix rows → the run's shards (1 Batch task)

- Every `cdelta` row at depth ≥ 1 is expanded to its suffix rows (`suffix_sql`), opens and closes alike.
- A close record is the closing version's suffix rows with their final `vt`. It carries the same `size` and `n_files` as the version, so it is self-contained: a reader holding only the record can answer for it.
- DuckDB sorts the rows `(s, path, usr, vf)`. `write_run_shards` streams them into shards in the base's format (format below), closing a shard at the first three-character-prefix boundary past ~50M rows, so no plan pass is needed. The run gets its own `shards.json` (same shape as the base's), `sx/`, `sidecar/` and `sidecar.parquet`.

### 3. Catalog delta (1 Batch task)

- `static_catalog.append(prev, base, deltas, V)` already equals a rebuild byte for byte (`test_append_equals_rebuild`).
- Its `prev` here is the merged catalog of the base plus every live run (the reader's merge, materialized locally: ~25 MB). `deltas` is every run's `cdelta`, oldest first.
- The run's catalog is the rows of the new full catalog that are not in `prev`:
  - the cells dated D, for existing members and one- or two-character literals;
  - every cell of a literal that became a member at D (its whole history);
  - the header row of every literal whose header changed or is new.
- Nothing else changes, because a cell is a running total at its `vf` and appending D only adds events at D.

### 4. Tier merge (when the counter carries) and manifest

- **Binary counter.** Runs carry a level; a day's run is level 0. After adding it, while the two newest runs have the same level `k`, they merge into one run of level `k + 1` spanning both. So after n days the live runs are the binary digits of n: at most ⌊log₂ n⌋ + 1 runs, and each day's rows are rewritten O(log n) times in all.
- **Merge.** `pyrmts.runs.merge_sorted` (pyrmts Phase 2, a streaming k-way merge) runs over the runs' `sx` (each run's shard files in prefix order), with `reduce={'vt': 'min'}` on `(s, path, usr, vf)`. The merged stream is re-cut into shards at three-character-prefix boundaries (`write_run_shards`).
  - The catalogs merge with `merge_parquets(tiers, ['q', 'bucket', 'vf'], reduce='newest')`.
  - The `cdelta`s merge with `identity=(depth, path, usr, vf)`, `reduce={'vt': 'min', 'op': 'max'}` (`merge_cdeltas`). The per-scan `cdelta`s are kept anyway: the catalog append consumes one per scan, as point events.
- **Compaction into a new base generation.** When the counter would reach level 5 (32 days), or on demand, the base plus its runs fold into a new generation.
  - The same merge (base first) is cut to the new generation's shard plan (`merge_shards(..., plan=…)`: shard `i` holds prefixes `[lo, hi)`, empty ones written as the build writes them). It is then the full build byte for byte (`test_compaction_equals_full_build`).
  - The catalog merge of base ⊕ runs is likewise the rebuilt catalog byte for byte.
  - At fleet scale this runs per base shard (a shard's prefix range of every run), in parallel on Batch.
- **Publish.** Every new file goes to R2 (`r2-copy`) before `manifests/<D>.json`; the manifest is uploaded last, so a reader never sees a run that is not all there.

## Formats

All files are written through pyarrow from a total sort order, in fixed row-group sizes, so a rerun on the same inputs (and image) is byte-identical. Timestamps are UTC.

### Run layout

`static-names/<gen>/deltas/<first>/` for a level-0 run (`<first>` = its scan id), or `static-names/<gen>/deltas/<first>_<last>/` for a merged run covering scans `first … last`:

| Key | What |
|---|---|
| `meta.json` | `{gen, first, last, level, scans: [ids], rows, bytes, shards, catalog: {cells_rows, row_groups, bytes}}` |
| `scans.json` | the run's scans, same shape as the base's (`{bucket, scans: [{id, src, generation, size, md5, crc32c, ts, version}]}`) |
| `shards.json` | the run's own shard plan, same shape as the base's (`shards: [{i, lo, hi, rows, prefixes}]`; `[lo, hi)` over three-character prefixes, never split) |
| `sx/s####.parquet` | suffix rows (below) |
| `sidecar/s####.parquet`, `sidecar.parquet` | per row group `(file, rg, s_min, s_max, offset, length, rows)`, `SIDECAR_SCHEMA`, exactly as the base |
| `catalog/{cells,index}.parquet`, `catalog/meta.json` | the catalog delta (below) |
| `cdelta/r####.parquet` | version-level delta per key range (GCS only; the refold source and the merge input) |

### Suffix rows (`sx/`), base and runs alike

| Column | Type |
|---|---|
| `s` | string (the lowercase name's suffix from a position, ≥ 3 characters, untruncated) |
| `depth` | uint8 |
| `path` | string |
| `usr` | string, dictionary-encoded (`''` = unattributed) |
| `vf` | timestamp(ms, UTC), required |
| `vt` | timestamp(ms, UTC), required; 2106-01-01 (`OPEN` × 1000) while open |
| `size` | int64 |
| `n_files` | int64 |

- Sort order: `(s, path, usr, vf)` in code-point order.
- Row groups: 8,192 rows (`SX_RG`). Compression: zstd. Statistics: untruncated.
- The file is split by three-character prefix, so a literal's rows are one contiguous byte range of one file per run.

**Version identity and close records.** A row is one suffix of one coalesced version. The version is identified by `(depth, path, usr, vf)`; `depth` is a function of `path`. A row's identity is `(s, path, usr, vf)`, which is unique within any one run. A row in a run is one of three things:

- **opened** in the run and still open at its end: `vf` ∈ the run's scans, `vt` = OPEN;
- **opened and closed** in the run: `vf` and `vt` both its scans;
- a **close record**: `vf` before the run's first scan (the version lives in the base or an older run), and `vt` = the run's scan that closed it. The record repeats the version's `size` and `n_files`.

There is no tombstone and no `op` column at this level: a close record is the version's row with its final `vt`.

**Combine rule (the merge's hook):** rows with equal `(s, path, usr, vf)` are the same version's row; the merged row is the one with the **smallest `vt`**. A version's `vt` only ever moves once, from OPEN to a scan, so "smallest" is "newest information". The other columns are equal by construction.

- Applied over base + runs, the combine gives exactly the rows a rebuild through the last run's scan would hold.
- The rule is commutative and associative, so runs can merge in any grouping (the tiers) and the reader can fold them in any order.
- The reader's liveness test `vf ≤ D < vt` is applied after the combine.

### Version delta (`cdelta/r####.parquet`)

- Columns: `CINTERVAL_SCHEMA` (`depth` uint8, `path`, `usr` dictionary, `vf` int64 epoch s, `vt` int64 epoch s, `size`, `n_files`) + `op` int8.
- Sort order: `(depth, path, usr, vf, op)`. Row groups: 65,536 rows. Compression: zstd.
- `op` = 1 is a version opened at the scan (`vt` = OPEN, or the closing scan after a merge). `op` = −1 is a close record (`vt` = the closing scan).
- A merged run's `cdelta` combines on `(depth, path, usr, vf)` with the smallest `vt`, keeping `op` = 1 when the version opened inside the run.

### Catalog delta (`catalog/`)

- `cells.parquet`: `CELL_SCHEMA` `(q, bucket, vf, b, o)`, sorted `(q, bucket, vf)`, 4,096-row groups. `index.parquet` and `meta.json` are as the base's.
- Each literal touched by the run is led by its header `(q, '', 0, rows, n)`:
  - `rows` is its suffix-range rows as of the run's last scan (−1 for one or two characters);
  - `n` is its cell count across the base and every run through this one, so a reader can check a merged lookup.
- Then come its new cells: for an existing member, the cells dated in the run (a bucket's running total at each scan where it changed); for a new member, its whole history.
- **Combine:** union the cells (never duplicated across tiers: a cell is keyed by `(q, bucket, vf)` and each `vf` belongs to one tier), and take the header from the newest tier holding `q`.
- A literal is a member iff any tier holds its header. Membership is monotone: once a member, always one.

### Manifest

`static-names/<gen>/manifests/<id>.json`, one per scan (by scan id), never rewritten:

```json
{"gen": "2026-10-08c", "date": "2026-10-09", "scans": ["2026-07-30", "…", "2026-10-09"],
 "runs": [{"key": "deltas/2026-10-09", "first": "2026-10-09", "last": "2026-10-09", "level": 0, "rows": 0, "bytes": 0}]}
```

- `runs` is oldest first.
- The reader lists `manifests/` (R2 `list`, held per isolate for 5 minutes) and takes the greatest key. The manifest's `date` versions its caches.
- With no manifest, the reader is today's base-only reader.

## Readers

**Suffix reader** (`staticNames.ts` `StaticNames`, the Python `static_names.Reader`):

- One `StaticNames` per tier (base, then each run), each over its own key prefix. Each run has its own `shards.json` and group indexes, cached under its own prefix.
- `extent(key)` is the sum over tiers. It is an upper bound on rows read, and since rows read ≥ range rows (closes only add rows), a sum ≤ V still proves `key` is not a member.
- `read(key)` fetches every tier's range in parallel and folds each into `FirstHits`. Then the hits are combined by `(path, usr, vf)` with the smallest `vt`.
- Combining at the hit level equals combining the rows. A hit is decided per row by `s` and `path` alone (name match, depth, parent test), so a version's rows are hits in every tier or in none.

**Catalog** (`staticCatalog.ts` `StaticCatalog`, the Python `Catalog`):

- `lookup(q)` runs on every tier in parallel. The header comes from the newest tier holding `q`. The cells are the union, sorted `(bucket, vf)`, and their count must equal the header's `n`.
- `catalogAnswer` is unchanged (the newest cell with `vf ≤ D` per bucket).

**Dispatch** (`nameSummaryStatic.ts`, `staticFilter.ts`):

- The scan list is the manifest's `scans` (else `scans.json`).
- An indexed date's answers never change when a run lands: a later close sets a `vt` after that date. So response cache keys (`staticTag`) stay per generation.
- A literal's hit list spans every date, so it is held and cached per manifest date (`SuffixHits`: `<key>@<date>`; the base alone keeps the bare key).
- `MAX_ROWS` (the filter's bound) applies to the summed extent. A literal near V whose runs add close records can then go over it and fall back, which is correct but slower. Compaction resets this.
- Heavy literals (`HitSource` `heavy`, the per-member roots index being built on `ch-store`) need their own per-run deltas. That is out of scope here; until then a heavy literal declines on a date past the base, as it does today.

## Implementation

- `cloud/src/dt_cloud/static_append.py`: the stages, `dt-cloud static-names runs {prepare,append,shards,catalog,publish,prune,rebuild-state,verify}` (Batch tasks run `python -m dt_cloud.static_append <stage> …`).
- `cloud/src/dt_cloud/static_runner.py`: the chain, `dt-cloud static-names runs add SCAN_ID [-c] [-n] [-t TERMS]` (below).
- `cloud/src/dt_cloud/static_profile.py`: the deployment profile (`Profile`); `static_profile_examples.py`: `gcs` and `cw`, worked examples. `cloud/src/dt_cloud/scan_ids.py`: scan ids (`SCAN_ID`, `scan_epoch`, `check_order`).
- Tier merges use `pyrmts.runs` (pinned 541bc8e).
- Reader: `site/functions/_lib/staticRuns.ts` (`Tiers`, `TieredNames`, `TieredCatalog`), wired into `nameSummaryStatic.ts` and `staticFilter.ts`; the generation is the deployment's `STATIC_GEN` var (required).

### The unit is a scan

A deployment may scan once a day, every 6 h, or once more on demand; nothing here assumes a cadence. Each scan is one run, keyed by its scan id (`YYYY-MM-DDTHHMM`, or a bare `YYYY-MM-DD` from before a deployment went sub-daily; specs/scan-ids-not-dates.md): `deltas/<id>/`, `manifests/<id>.json`, `state/<id>/`. Ids sort in time order (a bare date as its midnight), so "the newest manifest" is the greatest key, and `prune` keeps the newest complete state by id. Two scans of one day are two runs and two manifests.

### Deployment profile

`static_profile.Profile`: layouts (key templates of the scans' `path` sorts, `{id}` and `{gen}`), data and scratch buckets, base generation, R2 bucket and the R2 copy's Secret Manager secret names, GCP project, region, job image, accounts, machine, local SSD, spot, append tasks, an optional staged source tree. `STATIC_NAMES_PROFILE` selects an example (`gcs`, `cw`) or a JSON file; every field's env var (`static_profile.ENV`: `STATIC_NAMES_{LAYOUTS,BUCKET,SCRATCH,GEN,R2_SECRETS,REGION,IMAGE,SA,R2_SA,MACHINE,SSD,SPOT,APPEND_TASKS,SRC}`, `R2_BUCKET`, `R2_ENDPOINT`, `GCP_PROJECT`) goes on top. Nothing defaults to a deployment: a missing field is an error naming its variable. A base's `scans.json` records its layouts; a base from before that (gcs's `2026-10-08c`) uses the profile's.

### Entry point: `dt-cloud static-names runs add SCAN_ID`

```
prepare → append (key ranges, `append_tasks` tasks) → shards ∥ catalog → publish (Batch: tier merges + manifests/<id>.json) → R2 (each run in the manifest, then manifests/) [→ verify, -t] → prune
```

- **Strictly in scan-id order.** The published scans (under the base's layouts) after the generation's newest (base + the newest manifest's runs), through SCAN_ID, are pending. SCAN_ID must be the oldest of them, else exit 3 naming the others; `-c` appends every pending scan in order. A scan already appended reruns only the R2 copy and prune.
- **Idempotent, resumable.** Each stage is skipped when its output is in the data bucket: `deltas/<id>/scans.json` (prepare), all `ranges.json` `k` of `deltas/<id>/dhist/` (append; `append` also skips done ranges within a job), `deltas/<id>/sidecar.parquet` (shards), `deltas/<id>/catalog/meta.json` (catalog), `manifests/<id>.json` (publish), `deltas/<id>/verify.json` (verify). `r2-copy` skips objects already on R2 (size + md5), so the R2 step always runs.
- **Writes only new keys:** the scan's run dir, merged run dirs (`deltas/<first>_<last>/`), `manifests/<id>.json` (`if_generation_match=0`), and the scratch bucket's `state/<id>/`. The one delete is `prune`'s: earlier scans' `state/<prev>/` in the scratch bucket, once `state/<id>/` is complete.
- **Exit status:** 0 done; 3 the scan is not published yet, or an earlier published scan is pending (without `-c`), or `prepare` refuses; 1 a stage failed.
- **GCS and Batch over their APIs** (ADC; no `gcloud`), so it runs inside a scan job's image. Batch jobs are named `sn-<stage>-<id>-<hhmmss>` and labelled `purpose=static-names`, `stage`, `gen`.
- **R2:** one Batch job per scan (every run the manifest lists, then `manifests/`), as the profile's R2 account, with the R2 key from Secret Manager (`secretVariables`) and the endpoint from its secret or `R2_ENDPOINT`.
- **`-n`:** dry run. It reports each stage as done or that it would run; it submits and writes nothing.
- **Scheduling:** after a scan's `path` sort is written, run `dt-cloud static-names runs add -c <id>` (gcs: from `job/run.sh` after `index-sync`; cw: from `job/cw-run.sh`). On exit 3, try again later; it is safe to run on a timer.

Per scan, by hand (what `runs add` submits):

```bash
dt-cloud static-names runs prepare -g GEN -d ID
python -m dt_cloud.static_append append -g GEN -d ID -n N -m /gcs/BUCKET      # Batch, ⌈k/N⌉ tasks
python -m dt_cloud.static_append shards -g GEN -d ID -m /gcs/BUCKET           # Batch ∥ catalog
python -m dt_cloud.static_append catalog -g GEN -d ID -m /gcs/BUCKET
python -m dt_cloud.static_append publish -g GEN -d ID -m /gcs/BUCKET          # merges (if the counter carries), then manifests/ID.json
python -m dt_cloud.static_names r2-copy -g GEN/deltas/ID && python -m dt_cloud.static_names r2-copy -g GEN -o manifests/
dt-cloud static-names runs prune -g GEN -d ID [-n]
```

Dry runs on 2026-10-09 (UTC, gcs, `runs add -n`): `2026-10-10` and `2026-10-09T1236` exit 3 (not published yet), and `2026-10-09` (appended) reruns only the R2 copy and prune.

## Cost and footprint

**Measured on 2026-10-09** (the first run; spot n2-highmem-16, us-east1):

| Stage | Tasks | Run time | Output |
|---|---:|---:|---|
| `append` (256 ranges) | 16 (+ a 1-task smoke run) | 3.5 min each (≤ 19 s per range) | 1,291,160 versions opened, 387,872 closed, 609,231,660 open; `cdelta` 21.9 MB; `copen` 4.4 GB (scratch; only the newest day's is kept) |
| `shards` | 1 | 1.2 min | 44,125,015 suffix rows, one 1.05 GB shard (25 B/row: sparse paths compress worse than the base's 10 B/row) |
| `catalog` | 1 | 5.6 min | 94,549 cell rows (874 KB): 17,811 headers (17,670 changed), 141 new members |
| `publish`, `r2-copy` | laptop, VM | seconds; 17 s copy | 8 objects, 1.12 GB to R2; the manifest last |
| `verify` (30 terms) | 1 | 30.3 min | 175 / 175 checks equal |

- **Cost.** About 1.4 VM-hours a day without `verify` (~2.2 with it), ≈ **$0.40/day** spot (≈ $0.60 with `verify`), plus ~$0.13/day GCS → R2 egress. End to end, ~15 min after the scan publishes.
- **Footprint.** **~1.1 GB/day** on R2, about 0.8% of a full 135.7 GB copy; 30 days ≈ 34 GB ≈ $0.50/month. A day's suffix rows are 0.33% of the base's 13.2B.
- **Tiers.** Tier merges collapse close records against their opens, so merged runs hold at most the sum of their days. Compaction at level 5 (32 days) folds them into a new base.

### Egress: merges re-upload (corrected 2026-10-09)

The `$0.13/day` above counts only a run's own upload. Every tier merge writes a new run that also goes to R2. GCP bills internet egress on every byte that leaves it: a merged run written on a Batch VM is billed in full when uploaded, wherever its inputs were read from. Reading the merge's inputs from R2 instead of GCS saves nothing, because GCS reads in-region are already free. So under the binary counter a row is uploaded once per level it passes through. Over the 32 scans before compaction that is 5 uploads per row (32 runs at level 0, 16 merges of 2 runs at level 1, …, 2 of 16 at level 4: 5 × 32 run-sizes in all). Compaction then uploads the whole new base again.

| | run (per scan) | uploads per scan, amortized | + compaction | ≈ egress |
|---|---:|---:|---:|---:|
| gcs (1 scan/day) | 1.1 GB sx + 2.15 GB drill | ~5 × 3.25 GB ≈ 16 GB/day | base 136 GB sx + 299 GB drill per 32 days ≈ 14 GB/day | **≈ $3.5/day** |
| cw (4 scans/day, estimated from `2026-10-09cw`'s versions: 10–80M suffix rows a scan at 37.8 B/row) | 0.4–3 GB sx (+ drill, unmeasured) | ~5 × that × 4 ≈ 8–60 GB/day | base 119 GB sx per 32 scans = 8 days ≈ 15 GB/day | **≈ $3–9/day** + drill |

Levers (none built):

- **(a) Merge and compact where egress is free.** R2 charges no egress, so a job outside GCP can read runs from R2 and write the merged run back to R2 for free. GCP then pays egress only once per row, on the run's first upload. For cw, a CoreWeave node does this (cw's own compute; CoreWeave doesn't bill internet egress). The merge (`merge_sorted` + `write_run_shards`, `merge_parquets`, `merge_tiers`) is pure Python/DuckDB over parquet, so it ports as is. It needs an R2 read/write key on that node and the GCS-side bookkeeping (the run's `meta.json`, the manifest) written after the R2 copy. `cdelta` merges stay on GCS (they're small and never served).
- **(b) Merge less often.** A base-`b` counter merges `b` runs of a level into one: each row is uploaded about `log_b n` times instead of `log_2 n` (b = 4: 2.5 instead of 5 over 32 scans), and readers read up to `(b − 1) · log_b n` runs (4 → ~7 at n = 32).
- **(c) No R2 merges between compactions.** Readers fan out over every run since the last compaction: each row is uploaded once plus once per compaction. This is fine for gcs's daily cadence with weekly compaction (≤ 7 runs). It's too many runs for cw at 6-hourly unless compaction is ~2-daily, which re-uploads the base each time.
- **(d) Fewer suffix rows per hash name** (approved 2026-10-09; specs/static-hex-runs.md). 79% of cw's suffix rows (1% of gcs's) are suffixes of names holding a run of ≥ 16 hex digits. Indexing such a run only from its start keeps 27% of cw's suffix rows (3.7× fewer), and cuts every upload and merge of them by the same factor.

**Recommended:** (d) for both deployments (cw: a rebuilt generation; gcs: at its next compaction), then (a) for cw on a CoreWeave node, and for gcs (b) with b = 4, plus compaction only when the run count or the summed run bytes cross a bound, not at a fixed level.

### Shard size: the split `path` (planned, with the next compaction)

Measured on `2026-10-09cw` (specs/cw-static-names.md, "Why 3.8× gcs's bytes per row"): the `path` column repeats on every suffix row the name the row's `s` already holds. Storing it as `parent` (dictionary-encoded) + `head` (the name before the suffix) + `tail` (null unless the name's tail differs from `s` in case) gives path = `parent/head + (tail ?? s)` with no second lookup. That is −26 to −33% on cw's shards at zstd 9 against today's writer (39.1 → 26.6, 47.3 → 31.7 and 34.8 → 24.3 B/row on 64K-row samples of three shards); zstd 9 alone gives −7.5%. Nothing else in the format (DELTA_BYTE_ARRAY, 16K/32K groups, dictionaries) moves it by more than 1%.

It is a format change for every `sx` reader and writer (`write_sorted`'s callers, `Reader`, `staticNames.ts` `decodeGroup`/`FirstHits`, the merges' sort key, census/answers, the drill builders). The sort order stays `(s, path, usr, vf)`, with `path` rebuilt for comparison. Land it at a **compaction**, which rewrites the base and would re-upload it anyway, so the re-encode costs no extra egress. Readers dispatch on the file's columns, so old-format tiers (gcs's `2026-10-08c` and its runs) keep working until compacted.

## Verification

**Python** (`cloud/tests/test_static_append.py`, exact equality; the base is the fixture's first three scans, plus two runs, each run separately and merged):

- the open versions after each append equal the rebuild's open `cintervals`; each `cdelta`'s opens and closes are exactly the rebuild's versions opened and closed at D;
- base ⊕ runs under the combine rule holds exactly the rebuild's suffix rows;
- every literal's first hits `(path, usr, vf, vt, size, n_files)` equal the rebuild reader's, so every drill path and date agrees, and per-bucket answers equal brute force on every date;
- base ⊕ runs' catalogs are the rebuilt `cells.parquet` and `index.parquet` byte for byte, including a literal that crossed V on an append;
- a compaction (base ⊕ runs cut to the full build's plan) is the full build's shards and sidecars byte for byte;
- merged `cdelta`s equal the combine;
- `verify_terms` (the real-run check) passes on both runs;
- the counter's levels and merges are a binary counter.

**TypeScript** (`site/functions/_lib/staticRuns.test.ts`, fixture `fixtures/static-runs/gen.py`: a base through 09-01, runs for 10-01 and 10-02 with close records and a literal crossing V). Every literal on every date equals a brute-force oracle through each of:

- the suffix reader, at each manifest;
- the catalog (cells exactly);
- `/api/name-summary`'s dispatch;
- the map filter's per-root live totals.

A new manifest is picked up after the TTL. A mutation that keeps the largest `vt` fails 4 of the 9 tests.

**Real run** (`runs verify`, Batch): brute force from D's scan file versus the base + runs read through the reader logic. Per literal:

- a catalog member: its per-bucket totals;
- otherwise: its live first hits as a list, both whole and under drill roots (`''`, its top buckets, its top depth-2 dirs);
- in both cases: the day before, tiered, equals the base alone.

**Real run, 2026-10-09** (`deltas/2026-10-09/verify.json`): **175 / 175 checks equal**.

- 30 literals: the 22 named build terms, `a`, `_`, `.`, `zz`, `east5`, `us-east1`, `marin-us-c` and `-us-`.
  - 21 were answered statically: **562,054 live first hits** compared list for list, plus **94 drill-root checks** (e.g. `gof` under `marin-us-central1/podcast_audio_top1000_60s_clips`: 9,185 hits).
  - 9 came from the catalog (per-bucket totals).
- The day before (10-08), every literal's tiered answer equals the base's.
- The scan's 609,231,660 keys equal the append's open versions.
- **Dev site** (`dev.gcs.oa.dev`): the registry lists 71 dates through 10-09. `/api/name-summary` on 10-09 reads 2 tiers, and 8 literals' totals equal the Python tiered reader's. The map's `?f=gof` on 10-09 is served by the static filter (`tier: static+fine`), with match counts equal to `verify`'s at the root (9,287), `marin-us-central1` (9,222), its `podcast_audio_top1000_60s_clips` (9,185) and `marin-us-east5` (47).

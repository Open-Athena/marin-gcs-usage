# Static name search: the daily append

Status: in progress (branch `daily-append`, from `ch-store` @ 98ec30a4; reader on `daily-append-site`, from `hot-preview`).

## Why

The static name index (`specs/architecture/static-name-search.md`) answers `/names` and the map's `?f=<literal>` filter from R2 alone, but only for the scans of its generation. `2026-10-08c` ends at 2026-10-08. Every newer scan gets a 400 from `/api/name-summary` and the filter falls back to the per-scan path-store walk. Rebuilding a whole generation per day (~$10 Batch, 136 GB re-copied to R2, ~$16 egress) and keeping a full copy per scan is the cost this design avoids.

The goal: fast, exact `?f=` text search on the main map (treemap, table, diff) at any drill path and any date, so each daily scan has to join the index the day it lands.

## Shape

The base generation stays immutable. Each scan `D` adds one small **run** under new keys. A small immutable **manifest** per day lists the base plus the live runs. Readers query the base and every listed run in parallel and merge the results. Under a binary-counter policy, runs merge into bigger runs, so a lookup reads at most ⌊log₂ n⌋ + 1 runs after n days. Periodically, the base and its runs compact into a new generation.

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
  - `copen/` goes to the scratch bucket (`gs://oa-gcs-usage-scratch/static-names/<gen>/state/<D>/copen/`, 7-day expiry, no soft delete).
  - `cdelta/` goes to the data bucket under the run (`gs://oa-gcs-usage-dvx/static-names/<gen>/deltas/<D>/cdelta/`), kept permanently: it is small, and `copen` can always be refolded from the base's `cintervals` plus every `cdelta` (open rows, minus closes, plus opens) if the scratch copy expires.

### 2. Delta suffix rows → the run's shards (1 Batch task)

- Every `cdelta` row at depth ≥ 1 is expanded to its suffix rows (`suffix_sql`), opens and closes alike.
- A close record is the closing version's suffix rows with their final `vt`. It carries the same `size` and `n_files` as the version, so it is self-contained: a reader holding only the record can answer for it.
- The rows are planned into shards (`plan_shards` over `dhist`, ~50M rows per shard), sorted, and written in the base's format (format below). The run gets its own `shards.json`, `sx/`, `sidecar/` and `sidecar.parquet`.

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
- **Merge.** A k-way merge of the runs' sorted `sx` with the combine rule below, then re-sharded. The catalogs are unioned, keeping the newest header per `q` and its cell count. The `cdelta`s are concatenated with the same combine on `(depth, path, usr, vf)`.
- **Compaction into a new base generation.** When the counter would reach level 5 (32 days), or on demand, the base plus its runs fold into a new generation. Today that is the full build (~45 VM-hours ≈ $10). A streaming base ⊕ runs merge (pyrmts Phase 2) would replace it.
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

`static-names/<gen>/manifests/<D>.json`, one per day, never rewritten:

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
- The cache keys (`staticTag`, `cacheHits`) carry the manifest date, so a new day never serves a cached older hit list.
- `MAX_ROWS` (the filter's bound) applies to the summed extent. A literal near V whose runs add close records can then go over it and fall back, which is correct but slower. Compaction resets this.
- Heavy literals (`HitSource` `heavy`, the per-member roots index being built on `ch-store`) need their own per-run deltas. That is out of scope here; until then a heavy literal declines on a date past the base, as it does today.

## Cost and footprint

Estimated before the 2026-10-09 run, to be replaced by measurements:

- Batch: the 256-range append reads each range's open versions (≈ the newest scan's rows) and the scan's rows once. 16 spot n2-highmem-16 tasks × ~5–10 min, plus 1–2 single-task stages: ~3–5 VM-hours ≈ **$1/day** spot.
- R2: a day's run is the delta's suffix rows (opens + closes, ~1–3% of versions × ~16 suffixes each) at ~10 B/row, **~0.2–1 GB/day**, against 135.7 GB for a full copy. Tier merges rewrite each day's rows O(log n) times, so the live footprint stays ≈ the sum of days since the base.
- GCS: `cdelta` (versions, ~tens of MB/day) kept; `copen` (~GBs) in scratch, expiring.

## Verification

- **Local, exact equality:**
  - the direct coalesced append equals the rebuild's `cintervals` (open rows), and its `cdelta` equals `coalesce_append`'s;
  - for every (term, path, date), base + runs read through the reader logic equal a full rebuild's reader, and equal brute force;
  - the merged catalog equals the rebuilt catalog member for member;
  - tiered merges (1, 2 and 3 runs, merged and unmerged) give the same answers.
- **Real run, 2026-10-09:** 30 terms × several drill paths, answers vs brute force straight from the 10-09 scan file (`catalog brute`, plus a path-restricted variant), exact match counts reported.

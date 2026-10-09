# Static name search: per-scan append

Status: 2026-10-09 appended, verified (175/175 against brute force), on R2 and served by the dev site. Code on `cloud` (2026-10-09: the pipeline, the reader behind `FILTER_STATIC` / `NAME_SUMMARY_STATIC`, default off); the entry point `dt-cloud static-names runs add` (`static_runner.py`; it replaces `job/static-daily.sh` on `gcs-static`). Not yet scheduled; not on prod. The drilldown's run for 2026-10-09 (`deltas/2026-10-09/drill/`, "Drilldown runs" below) is verified (653/653 against brute force) and on R2; the site does not read runs' drills yet.


## State (2026-10-09 15:00 UTC)

- **First sub-daily run, gcs `2026-10-09T1236`** (`runs add -t …`, staged `cloud` tree `9d852323…` + pyrmts 541bc8e as `STATIC_NAMES_SRC`, `STATIC_NAMES_PROFILE=gcs`): prepare 12 s, append 6.4 min (16 spot tasks × 16 ranges), shards ∥ catalog 10.4 min, publish 8.4 min, R2 2.4 min. The run: 85,705,437 suffix rows, 2.37 GB. Verify (the 30 terms of `deltas/2026-10-09/terms.txt`) ran after: see `deltas/2026-10-09T1236/verify.json`.
- **Incident:** the counter merged `deltas/2026-10-09` (which carries the drilldown's `drill/`) with T1236 into `deltas/2026-10-09_2026-10-09T1236`, which had no `drill/` and no `catalog/meta.json`. Published at 14:32:33, it made prod's heavy filter 500 for a few minutes and dropped 10-09's drill coverage. Fix, by hand at 14:35–14:39: `deltas/2026-10-09T1236` copied to R2, then `manifests/2026-10-09T1236.json` **rewritten** to list the two level-0 runs (the merged version is kept at `gs://oa-gcs-usage-dvx/static-names/2026-10-08c/manifests-superseded/2026-10-09T1236.merged.json`). That rewrite broke rule 2 below, once, on the coordinator's call. The merged dir `deltas/2026-10-09_2026-10-09T1236/` on GCS is orphaned: nothing lists it, and it was never copied to R2. It is kept, not deleted.
- **Fixed in code** (`9a4e5c29`):
  - the counter never merges a run holding a tier it can't carry (`pinned_runs`, `MERGED_ENTRIES`);
  - a merged run's catalog gets its `meta.json`;
  - `publish` refuses a manifest whose runs lack a reader file (`RUN_FILES`).
- **Superseded** (`drill-runner`): merges carry `drill/` (`merge_drills` → `merge_tiers`), so the pin rule is replaced by drill parity: `push_run(drilled=…)` merges two runs only when both carry `drill/` or neither does (a drilled run with one without would drop the one's drill: the reader stops at the first run without one), and the merged run is drilled iff its inputs are. `MERGED_ENTRIES` would also have pinned 10-09 for good (its `drill-verify/`).
- **Rules:**
  1. Write a manifest only after every file of every run it lists exists on that store, R2 included. GCS: `publish` checks `RUN_FILES` and, for a run with `drill/meta.json`, every file it implies (`static_drill.tier_files`). R2: the runner's one job copies each listed run (its `drill/meta.json` last), then `r2-verify -m <id>` (every served file of every listed run on R2, same size and md5), then the manifest, chained with `&&`.
  2. Manifests are immutable. Never rewrite a published key; fix forward with a new manifest instead (the next scan's).
  3. A manifest's runs never cover fewer scans with a drill than the last manifest's (`drill_scans`): `publish` refuses otherwise.
- **The drill stage** is in `runs add` (profile `drill`, on in gcs's example): after shards ∥ catalog, before publish (see "Entry point").
- Next gcs scan: `2026-10-10` (cron 07:00 UTC) → `runs add -c 2026-10-10`. With T1236's drill built, the stage builds 10-10's and the counter merges T1236 + 10-10 into level 1, drill included.

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
- Heavy literals (`HitSource` `heavy`, the drilldown of `specs/architecture/static-name-search.md`) read the drill tiers below. Until a run has its `drill/`, a heavy literal declines on a date past the base, as it does today.

## Drilldown runs (heavy terms)

The base generation's `drill/` (spec `static-name-search.md`, "Drilldown for heavy terms") answers a catalog member's filtered view at any directory P: its match roots under P (`roots`), or for a heavy `(q, dir)` (more than R = 100K roots under it) per-child running sums with the K = 256 children of largest peak bytes kept by name and the rest summed into a remainder (`rollups`). Each run gets its own `drill/`, in the base's layout and schemas, read beside the base like the suffix runs.

### Per scan D: the `drill` stage (1 Batch job of 2 tasks, after `catalog`, before `publish`)

`python -m dt_cloud.static_drill build -g GEN -d D -k task`: task 0 builds the long kind, task 1 the short, each on a whole machine; each uploads its files and its part (`meta.<kind>.json`), and the second to finish writes `meta.json` from both (`join_meta`). A rerun task whose part is there only joins. The stage is done once `deltas/<D>/drill/meta.json` exists.

1. **Delta roots.** A member's roots at D are first-hit rows of D's version delta, so they are a function of the run's own rows:
   - long members: `static_roots.member_roots` over the run's suffix rows (`sx/`: opens and close records alike) for every long member as of the previous tier;
   - short literals: `short_roots` over the run's `cdelta` (a literal first seen at D simply has no earlier rows);
   - each row is `(q, path, usr, vf, vt, size, n_files)`: an open (`vf` = D, `vt` = OPEN) or a close record (`vf` < D, `vt` = D, the version's own values). Combine rule as the suffix runs: rows equal on `(q, path, usr, vf)` are one root, the **smallest `vt`** wins.
   - **A literal that became a member at D** (a long header new in the run's catalog): its whole history, `member_roots` over its tiered suffix range (base ⊕ runs combined; at most V rows in the base by definition, plus the runs'), written into this run. It starts a class of its own (below).
2. **Classes (aliases).** Base aliases group members with identical root sets. Two members of a class can diverge at D (`x.js` adds a root for `.js` but not for `.json`). Classes only ever split: a root identity `(path, usr, vf)` in one member's set and not the other's never goes away. So the classes at D are the classes before D refined by the digest of each member's delta (`static_roots`' order-free `(n, h1, h2)`), the canonical being the class's least member in code-point order, as the base's. The run's `aliases.parquet` is that map for every long member; only canonical members' rows are written.
3. **Rollups.** For every heavy `(c, dir)` with delta roots under it (heavy = it has a rollup in some tier; it stays heavy), with the previous state read from the tiers (below):
   - per child X: `Δ` = Σ opens − Σ close records under X (bytes, objects);
   - a kept child's new running cell at D, the remainder's (Σ `Δ` of the rest), each only where it changes — the rebuild's cells at D exactly;
   - **kept set.** The rebuild keeps the top K by `(peak bytes desc, child)`. A child outside the kept set lost to the K-th at D−1, and the K-th only rises (kept peaks only grow), so X can enter only if its value on D, `(v_D(X), X)`, beats the new K-th. Candidates are the non-kept children with positive byte `Δ` (and any child with no earlier roots), pruned by `v_D(X) ≤ min(remainder_{D−1}, K-th peak_{D−1}) + Δ(X)`; the survivors' `v_D` are exact: new children's `Δ`, a root child's path total from D's open versions (`copen`), a heavy child's own updated rollup, else its roots under `dir/X` read from the tiers (≤ the dispatch bound);
   - if an existing child leaves (or one with earlier roots enters), the whole `(c, dir)` is **restated**: header, every kept child's cells and the remainder's, over its whole history (the entrant's series read as above, the leaver's from its cells), under a **full header** (`kind` 0). Otherwise the run holds only the cells dated in it under a **delta header** (`kind` −1, so it sorts first, as a full header does). A child with no earlier roots that joins (fewer than K kept) or goes to the remainder needs no restatement (it has no history to move);
   - **header.** `b` (root rows under `dir`) grows by the opens; `o` (children with roots, ever) by the children new at D: a root child is new iff its path never had a version (`cintervals` ⊕ every run's `cdelta`); a directory child that is kept was not new; a non-kept one is new iff no tier holds a root under it before D (an index probe: a group boundary key inside the range settles it, else the group's `q`, `path` decoded). Dirs with no remainder need no probe (every existing child is kept, by name). `vf` (kept count) is `min(K, o)`.
4. **Heavy dirs.** A `(c, dir)` becomes heavy when `S(c, dir)`, its stored root rows summed over the tiers (base ⊕ every run, close records counted), exceeds R; its rollup is then built in full from its roots read from the tiers (`rollup_sql`'s rule, one directory) under a full header. `S` comes from the base measurement (`roots-measure/`: exact per `(q, dir)` with ≥ 10K rows; below that, an exact count from the base index and at most two decoded groups when the runs could push it over) plus each run's `state/dcount-<kind>.parquet` (rows per non-heavy `(q, dir)` in that run, GCS only).
5. **Write.** Roots sorted `(q, path, usr, vf)` and rollups sorted `(q, dir, kind, child, vf)` in 8,192-row groups, files cut at `q` boundaries, the two-level indexes and top files exactly as the base's (`write_indexed`, `write_index_levels`); `meta.json` last.

### Run layout (`static-names/<gen>/<run>/drill/`)

| Key | What |
|---|---|
| `meta.json` | as the base's (`R`, `K`, `rg`, `idx_rg`, `dispatch_rows`, per set files / row groups / rows / bytes), plus `first`, `last`, `level`, `scans`, `classes`, `new_members`, `heavy_new`, `restated`; written last: a run's drill is live iff it exists |
| `aliases.parquet` | `(q, canonical)` for every long member as of the run's last scan |
| `long/roots/r####.parquet`, `short/roots/r####.parquet` | roots, `ROOT_SCHEMA`, in **2,048-row groups** (`rg` in the meta; the base's are 8,192), so each run adds little to the reader's summed slack |
| `long/rollups/r####.parquet`, `short/rollups/r####.parquet` | rollups, `ROLLUP_SCHEMA`; `kind` 0 full header, **−1 delta header** (`vf` kept count, `b` root rows under `dir`, `o` children ever), 1 kept child's cells, 2 remainder's cells; the header is a `(q, dir)`'s first row either way |
| `{long,short}-{roots,rollups}-index.parquet`, `….top.parquet` | the two-level indexes, as the base's |
| `state/dcount-{long,short}.parquet` | GCS only: `(q, dir, rows)` per non-heavy `(q, dir)` with rows in the run |

### Reader (base ⊕ runs)

Tiers are the base and every manifest run whose `drill/meta.json` exists (oldest first); a date past the last such run declines as today.

1. Long term `t`: per tier, `c_i = aliases_i[t] ?? t` (each tier's own map). Short: `c_i = t`.
2. **Dispatch.** Sum each tier's roots bound for `[(c_i, P/), (c_i, P0))` (the base's two-level `select`). At most `R + 2 · Σ_i rg_i` (each tier's `rg` from its meta: 116,384 for the base alone, 120,480 with one run, 136,864 with five): read the roots of every tier, combine on `(path, usr, vf)` with the smallest `vt`, and answer as the base. Otherwise `(t, P)` is heavy: since each tier's bound is at most its rows + 2·`rg`, the stored rows exceed R, and the builder has given it a rollup.
3. **Rollups.** Each tier's rows for `[(c_i, P), (c_i, P\0))`; walk the tiers newest first, taking each tier's cells (its rows' first is the header: `kind` 0 or −1), and stop after the first tier whose header is `kind` 0 (it holds the whole history). The header is the newest tier's; the cells are the union; per date each `(kind, child)`'s newest cell with `vf ≤ D`, as the base.

The combine is commutative over roots, and rollup tiers only ever add later cells or restate, so runs merge in any grouping.

### Aliases: why each tier keeps its own map

The other choice, dropping an alias forward (writing the diverged member's own rows from D on), would make the run copy the member's whole base root set (up to 227M rows for `.js`) the day it diverges, and divergence is common in the big nested families. Keeping the base aliasing plus per-member deltas instead costs one map per tier and one rule (`c_i = aliases_i[t] ?? t`, applied per tier, the base's lookup unchanged), and the run rows stay deltas; classes that stay together still share one copy.

### Heavy dirs and exactness

- The roots of base ⊕ runs (per member, combined) are exactly a rebuild's.
- Every rollup holds exactly the rebuild's cells, kept set and header for the same `(q, dir)`.
- Heaviness is by `S` (stored rows) rather than the rebuild's distinct root rows: close records count once per tier until a merge collapses them, and a rollup is never dropped once built. So a run can hold a rollup a rebuild would not have (a directory within its closes of R), and the summed bound can send a few directories near R the other way. Such a view is still exact against brute force: roots are the whole answer; a rollup's kept children are exact and its remainder is the total less them. Compaction resets it.

### Tier merges and compaction

- **Merge** (when the binary counter carries, in `publish`; only runs that all carry `drill/`, `push_run(drilled=…)`): the newest input's alias map; each input's rows re-keyed to the merged canonicals (`alias_i(c)` for each merged canonical `c`: its class at input `i` contains `c`'s) and combined (roots: smallest `vt`; rollups: the newest-full-header rule, the merged header `kind` 0 if any included input was full); `dcount` summed.
- **Compaction** into a new base generation reruns the drill build (`roots digest` → `build` / `short-*` → `index`) on the compacted generation (~85 VM-hours ≈ $19 at today's size); a fold of the runs into the base drill is not built.

### The 2026-10-09 run (measured)

`dt-cloud static-names daily drill build -g 2026-10-08c -d 2026-10-09` (now `runs drill build`) on one spot n2-highmem-16 (`MODULE=static_drill`), first to the scratch bucket, then copied to `deltas/2026-10-09/drill/` (server side) and to R2 with `r2-batch` (`meta.json` in a last pass).

| Step | long | short |
|---|---:|---:|
| delta roots | 84,479,040 (14,143,806 of them the whole history of the 141 new members) | 49,330,377 |
| classes | 35,915 → 35,916 (one split), +141 new | — |
| heavy dirs touched / newly heavy | 13,612 / 423 | 9,345 / 144 |
| probes (children new or not) | 84,381 (26,611 had earlier roots) | 338,492 (94,643) |
| candidates to enter a kept set / dirs settled / restated | 26,691 / 629 / 618 | 98,053 / 648 / 600 |
| rollup rows / `dcount` rows | 242,088 / 10,369,274 | 241,139 / 1,998,647 |
| time | 23.8 min | 38.6 min |

- **Footprint.** 2.15 GB served (long roots 1.28 GB in 2 files, short roots 0.85 GB, rollups 6.6 MB, indexes 7.7 MB, aliases), plus 43 MB of `state/` on GCS only: 0.7% of the base drill's 299 GB. Roots compress to 15 B/row for long and 17 B/row for short, against the base's 6.3 B/row: a day's rows are scattered paths. That is ≈ 2× the light run's 1.1 GB/day, and ≈ 65 GB per 30 days ≈ $1/month on R2.
- **Cost.** The build ran 62 min (one spot VM ≈ $0.25). The steps behind it, at that run's code: history 55 s; probes 7.5 + 20 min, `CachedGroupFile` group reads 0.35 + 0.62 GB from the base files through the mount; settling 7.5 + 14.7 min; newly heavy dirs' earlier roots 38.8M + 12.7M rows. Since then, probes bisect within cached groups and settling preloads its inputs; not yet re-measured. R2 egress ≈ 2.15 GB × $0.12 ≈ $0.26/day. All told ≈ **$0.5/day**, about the light run's.
- **The first attempt** (`sn-drill-build-20261009-a`) was stopped after 85 min. New members' history went through the Python reader (27 min for 12.2M suffix rows; now DuckDB, `history_table`), settling looked up `dr` per directory, and newly heavy rows were inserted one by one.

**Verification** (`deltas/2026-10-09/drill-verify/`):
- **Cases.** 654 `(term, P)` pairs over 46 terms (34 long, 12 short): the base's 455 `drill-cases.jsonl` plus 215 from `drill cases` (what the run changed: per term, its run rollups with a full header first, then a delta header, then parents of its run roots; plus 8 new members). 95 of the cases read a rollup the run restated or built, 37 a delta-header rollup, and 48 a new member.
- **10-09 vs brute force** from the 10-09 scan file (`roots drill-brute -S <run scans.json>`, 1 spot task, 9 min): **653 / 653 equal** (1 plain). 343 were read from roots, up to 110,731 rows; 310 from rollups, 91 of them with a remainder. 507 are non-empty, comparing 1,306,746 children, and 44 cases have more than 200K children.
- **10-08, tiered vs the base alone.** All 605 comparable cases are equal: every child both name is equal, and so are the totals (kept + remainder). That leaves out the 48 new-member cases, which the base drill cannot answer. 546 views are identical. In the other 59, a restated kept set names other children on every date, exactly as a rebuild through 10-09 does.
- **Read cost** (laptop over GCS): 0.40 s median and 5.0 s max per case for both dates.

### R2 layout for the reader (`oa-gcs-usage-index`)

- `static-names/2026-10-08c/drill/…`: the base (unchanged).
- `static-names/2026-10-08c/deltas/2026-10-09/drill/`:
  - `meta.json` (`rg` 2048, `R`, `K`, `first`/`last`/`level`/`scans`);
  - `aliases.parquet` (all 82,757 long members → their canonical at 10-09);
  - `long-roots-index.parquet` + `.top.parquet` (41 top entries);
  - `long-rollups-index…`, `short-roots-index…`, `short-rollups-index…`;
  - `long/roots/r0000.parquet`, `r0001.parquet`;
  - `long/rollups/r0000.parquet`, `short/roots/r0000.parquet`, `short/rollups/r0000.parquet`.
- Manifest `manifests/2026-10-09.json` is unchanged (it predates the drill). A reader takes a run's drill as live iff `<run>/drill/meta.json` exists. Index `file` paths are relative to the tier's `drill/`, as the base's.

## Implementation

- `cloud/src/dt_cloud/static_append.py`: the stages, `dt-cloud static-names runs {prepare,append,shards,catalog,publish,prune,rebuild-state,verify}` (Batch tasks run `python -m dt_cloud.static_append <stage> …`).
- `cloud/src/dt_cloud/static_runner.py`: the chain, `dt-cloud static-names runs add SCAN_ID [-c] [-n] [-t TERMS]` (below).
- `cloud/src/dt_cloud/static_profile.py`: the deployment profile (`Profile`); `static_profile_examples.py`: `gcs` and `cw`, worked examples. `cloud/src/dt_cloud/scan_ids.py`: scan ids (`SCAN_ID`, `scan_epoch`, `check_order`).
- `cloud/src/dt_cloud/static_drill.py`: `dt-cloud static-names runs drill {build,query,cases}` (Batch: `python -m dt_cloud.static_drill build …`), tests `cloud/tests/test_static_drill.py`; `merge_tiers` for the binary counter's merges (not yet called from `publish`, nor from `runs add`).
- Tier merges use `pyrmts.runs` (pinned 541bc8e).
- Reader: `site/functions/_lib/staticRuns.ts` (`Tiers`, `TieredNames`, `TieredCatalog`), wired into `nameSummaryStatic.ts` and `staticFilter.ts`; the generation is the deployment's `STATIC_GEN` var (required).

### The unit is a scan

A deployment may scan once a day, every 6 h, or once more on demand; nothing here assumes a cadence. Each scan is one run, keyed by its scan id (`YYYY-MM-DDTHHMM`, or a bare `YYYY-MM-DD` from before a deployment went sub-daily; specs/scan-ids-not-dates.md): `deltas/<id>/`, `manifests/<id>.json`, `state/<id>/`. Ids sort in time order (a bare date as its midnight), so "the newest manifest" is the greatest key, and `prune` keeps the newest complete state by id. Two scans of one day are two runs and two manifests.

### Deployment profile

`static_profile.Profile`: layouts (key templates of the scans' `path` sorts, `{id}` and `{gen}`), data and scratch buckets, base generation, R2 bucket and the R2 copy's Secret Manager secret names, GCP project, region, job image, accounts, machine, local SSD, spot, append tasks, an optional staged source tree. `STATIC_NAMES_PROFILE` selects an example (`gcs`, `cw`) or a JSON file; every field's env var (`static_profile.ENV`: `STATIC_NAMES_{LAYOUTS,BUCKET,SCRATCH,GEN,R2_SECRETS,REGION,IMAGE,SA,R2_SA,MACHINE,SSD,SPOT,APPEND_TASKS,SRC}`, `R2_BUCKET`, `R2_ENDPOINT`, `GCP_PROJECT`) goes on top. Nothing defaults to a deployment: a missing field is an error naming its variable. A base's `scans.json` records its layouts; a base from before that (gcs's `2026-10-08c`) uses the profile's.

### Entry point: `dt-cloud static-names runs add SCAN_ID`

```
prepare → append (key ranges, `append_tasks` tasks) → shards ∥ catalog → [drill: long ∥ short] → publish (Batch: tier merges + manifests/<id>.json) → R2 (each run in the manifest, its drill/meta.json last; r2-verify; then manifests/) [→ verify, -t] → prune
```

- **Strictly in scan-id order.** The published scans (under the base's layouts) after the generation's newest (base + the newest manifest's runs), through SCAN_ID, are pending. SCAN_ID must be the oldest of them, else exit 3 naming the others; `-c` appends every pending scan in order. A scan already appended reruns only the R2 copy and prune (and, with `drill`, builds its drill first when the newest manifest lists its own level-0 run without one: T1236's case).
- **Idempotent, resumable.** Each stage is skipped when its output is in the data bucket: `deltas/<id>/scans.json` (prepare), all `ranges.json` `k` of `deltas/<id>/dhist/` (append; `append` also skips done ranges within a job), `deltas/<id>/sidecar.parquet` (shards), `deltas/<id>/catalog/meta.json` (catalog), `deltas/<id>/drill/meta.json` (drill), `manifests/<id>.json` (publish), `deltas/<id>/verify.json` (verify). `r2-copy` skips objects already on R2 (size + md5), so the R2 step always runs.
- **Writes only new keys:** the scan's run dir, merged run dirs (`deltas/<first>_<last>/`), `manifests/<id>.json` (`if_generation_match=0`), and the scratch bucket's `state/<id>/`. The one delete is `prune`'s: earlier scans' `state/<prev>/` in the scratch bucket, once `state/<id>/` is complete.
- **Exit status:** 0 done; 3 the scan is not published yet, or an earlier published scan is pending (without `-c`), or `prepare` refuses; 1 a stage failed.
- **GCS and Batch over their APIs** (ADC; no `gcloud`), so it runs inside a scan job's image. Batch jobs are named `sn-<stage>-<id>-<hhmmss>` and labelled `purpose=static-names`, `stage`, `gen`.
- **R2:** one Batch job per scan (every run the manifest lists, each with its `drill/meta.json` last; `r2-verify`; then `manifests/`), as the profile's R2 account, with the R2 key from Secret Manager (`secretVariables`) and the endpoint from its secret or `R2_ENDPOINT`.
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
MODULE=static_drill SPOT=1 job/static-names.sh run build 1 -g 2026-10-08c -d D [-t gs://<scratch>/…/drill]          # the run's drill/ (meta.json last)
dt-cloud static-names runs drill cases -g 2026-10-08c -r deltas/D -t job/static-names/drill-terms.txt > cases.jsonl     # + drill-cases.jsonl
dt-cloud static-names runs drill query -g 2026-10-08c -r deltas/D -c cases.jsonl -d D > answers.jsonl
MODULE=static_roots SPOT=1 job/static-names.sh run drill-brute 1 -g 2026-10-08c -d D -c gs://…/cases.jsonl -k gs://…/answers.jsonl -S gs://…/deltas/D/scans.json -O …/deltas/D/drill-verify/brute.jsonl
dt-cloud static-names roots drill-verify brute.jsonl answers.jsonl
job/static-names.sh r2-batch 2026-10-08c/deltas/D -o drill/{l,s,a}; then -o drill/meta.json                            # wt/gcs-static's r2-batch
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

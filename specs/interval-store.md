# Interval store: change intervals in place of per-scan path stores

**Status:** draft v4, 2026-10-09. Base work: on `cloud` (opt-in, `PATH_STORE=opt-in` + `ps=iv`).
- v1 (2026-10-07, unreviewed) recommended object-only intervals plus keyframes.
- v2 decided for objects and directories as one interval table, with no keyframes.
- v3 reports the prototype: every gcs scan built, verified and served from R2 on dev (§6). It also folds `last_read` into the version key, on that evidence (§2.2).
- v4: closed versions in dyadic time segments (§2.4), the owner-slice table and its reader (§2.1, §2.6), shared footer and group decodes in the reader (§2.6), generation `2026-10-09b` (§6).

Nothing here deletes anything. §4 is a proposal for Ryan.

**Goal (Ryan, 2026-10-09):** *"I'm more concerned about parts of the stack where we're storing a mostly-redundant full scan's worth of data for every new scan that comes in, as opposed to leveraging the mostly-constant-over-time nature of our subject fleets."* A scan in which little changed should cost little: the store should grow with the fleet's changes, not with its size.

**Units.** A scan is identified by its id, `YYYY-MM-DD[THHMM]` (gcs has sub-daily scans now, e.g. `2026-10-09T1236`), and stamped with its epoch. Nothing assumes one scan per day. Deployment specifics (bucket, the scans the parity check samples) live in a profile, `cloud/src/dt_cloud/interval_profiles/<name>.json`; `gcs.json` is the example.

## 0. Where we are

Every gcs scan writes a full copy under `gs://oa-gcs-usage-dvx/listing/<scan>/`, kept forever:

| Artifact | Bytes per scan | What reads it |
|---|---:|---|
| path store `index/<gen>/path-index{,-bysize,-bysize-by-user}.parquet`, plus `.groups.{json,parquet}` and `attr.tsv` | 21.7 GB | the site: subtree/treemap, diff, series, filtered views' phase 2, age and read lenses, `/staged`. Footers in D1 for the latest 30 scans, `.groups.parquet` for older ones |
| `dir-cache/` | 19.1 GB | the next scan's attribution |
| raw listings `<bucket>/*.parquet` | 10.7 GB | `path-index` (the store is cut from them), sweep manifests |

- `listing/` holds 3.32 TB today (71 scans, 2026-07-30 → 10-09) and grows ~51 GB per scan: ~$66/month now, ~$370/month after a year of daily scans at GCS Standard.
- D1 holds ~218 MB of footer rows per path-store scan. That's why only 30 scans fit there.

## 1. Evidence before the prototype

`static-names/2026-10-08` (`specs/architecture/static-name-search.md`) ran pyrmts' gaps-and-islands kernel over every gcs scan's `path` sort:
- keyed by `(depth, path, usr)`;
- objects and directories, with every value a change column.

What it found:
- 1,153,480,980 versions for 70 scans, against ~780M rows for one scan. That's ~1.6 versions per key.
- Coalesced on `(size, n_files)`: 797,994,947 versions.
- Every scan's opened and closed versions equal ClickHouse's independent ingest.

The **per-scan append** is already solved (`specs/static-daily-append.md`, `static_append.py`):
- `append_intervals` over the open versions only;
- each scan's opens and close records as a run;
- runs merged on a binary counter (`pyrmts.runs`, min-`vt` combine);
- immutable manifests, ~$0.40 per scan.

## 2. Decisions

### 2.1 Rows: one table of objects *and* directories, one row per `(depth, path)`

**Directories are stored as intervals, beside objects.** v1 rebuilt directory totals at read time from keyframes plus signed object deltas, to avoid "multiplying churn by the ancestors touched". Measured (§6): over the store's 71 scans, directories hold 536M versions over 223M paths, 2.4 versions per directory. Ancestors are shared, so the fan-out is bounded by the directories that hold the changes, not by changes × depth. In exchange:
- a read at any scan is one range read of stored totals, as today: no keyframe, and no summing of every changed descendant's delta up the tree;
- the treemap's query stays a 3-sided range query over one sorted file (`path-store.md` §1.3), with a fourth side: `vf ≤ D < vt`;
- no scan is a full copy.

**Row identity is `(depth, path)`, one row per path.** A path's owner slices fold into the row (`us`, below). A path that is both an object and a prefix is one row whose `kind` is `dir`. Today's store keeps one row per `(path, usr)` slice. Folding the slices changes two things:
- **Thresholds are exact.** Today's `bysize` read thresholds *per slice* (`readSizeRects` tests `size ≥ thrAt(depth)` on each row). So a drawn directory counts only its slices over the threshold, and a directory whose slices are each under it goes missing. This is a live prod bug, measured in §7.
- **The user lens needs slices.** A lens reads U's slice of each path with every value. So the store has a second interval table, the owner slices (`build -S`, `sv/`): one row per `(depth, path, usr)` — the per-scan store's own rows, a v1 index's duplicates summed — with every value and `last_read` in the key, `usr` NULL where no owner is named. It is served in the per-scan store's three sorts:
  - `slices` `(depth, path, usr, vf)`;
  - `slices-bytotal` `(⌊log2 tot⌋ desc, path, usr, vf)`: keyed on the path's total, as the per-scan `bysize` is since `bysize-path-total.md`. `fold -S` splits each slice version where its path's total changes (`svt/`, the pieces checked to tile the slices);
  - `slices-bysize-user` `(usr, ⌊log2 size⌋ desc, path, vf)`.
  
  Their group indexes carry `u_min`/`u_max`. A slice sort *is* the per-scan sort as intervals, so the per-scan reader's owner logic (lens rects, `ownerOk`, owner totals) runs on it unchanged.
- **Slices barely add versions.** Most paths are one slice: 1,156,339,350 slice versions against 1,156,324,516 folded path versions (609.2M open in both).

### 2.2 Version key

| Column(s) | Role | Why |
|---|---|---|
| `kind`, `size`, `n_files`, `n_children`, `n_desc`, `mtime`, `wb`, `c2..c4`, `us`, `last_read` | **change** | a different value is a different tile |
| `dr` = `round(wts / wb)`, the size-weighted mean stamp to the second | **change** | the age lens's `d` |
| `wts` = Σ `mtime_mean · mtime_w` | **carried** (`first`) | the exact float sum drifts in its last bits between scans when nothing changed |

`us` holds a path's owner slices as one string:
- `''` when no owner is named;
- the owner's name when the path is exactly one row and it is named;
- otherwise JSON `[["usr", bytes], …]`, every named owner including zero-byte ones.

**`last_read` is in the key (decided on measurement).** v2 kept read days in their own interval table, to keep read churn out of the versions:
- **The separate table is small:** 40.0M read-day versions against 1.118B path versions.
- **Its lookup is slow:** a view's ~1–2K tiles are scattered across hundreds of the read sort's groups. On dev that lookup cost 1.8–8.7 s per view.
- **Folding is cheap:** folding the read days into the path versions (`interval-store fold`: split each version where its read day changes) costs **+3.4%** (1,118,487,204 → 1,156,324,516 versions).
- **It removes the second read entirely.**

Carrying `last_read` with pyrmts' `carried=` instead would make a mid-run scan's read day wrong.

### 2.3 Encoding

- Each version holds `[vf, vt)` in epoch seconds of the scan. `vt` is `OPEN` (2106-01-01) while the version is open.
- A version opens at its first scan and closes at the first scan after its run. A path absent from a scan closes; a path that comes back opens a new version.
- `scans.json` lists the scans in order: id, source, epoch. A missing scan (gcs 2026-08-10) has no stamp and is never asked for.
- A read at `D` takes the rows with `vf ≤ D < vt`.

### 2.4 Layout: sorted files, open versions then dyadic time segments

| File | Order | Serves |
|---|---|---|
| `served/path.parquet` | `(depth, path, vf)` | P's own row, children by name, small subtrees, the diff's point lookups, change scans |
| `served/bysize.parquet` | `(⌊log2 size⌋ desc, path, vf)`, size 0 last | every thresholded subtree read (the treemap at any P), top-K |

- **Segments.** Each file holds the versions still open at the generation's last scan (segment 0), then the closed ones in **dyadic time segments** (`seg_sql`). A closed version live at scans `i..j` (by index in `scans.json`) goes to the smallest aligned block of scans holding both: level `k = bit_length(i ^ j)`, block `i >> k`. Each segment is sorted as above and cut at its boundary, so no row group mixes segments.
  - A read at the newest scan prunes every closed group by `vt_max ≤ D`, and decodes what a per-scan copy would.
  - A historical read at D touches one block per level (⌈log2 n⌉ + 1); every other block's groups fail `vf_min ≤ D < vt_max`, because a block's versions live only inside it. Within a block a version crosses the block's midpoint, so it is live at most of the block's scans.
  - Measured on the 71 gcs scans (`path`): open 609.2M rows; L0 70 blocks / 28.4M; L1 34 / 1.8M; L2 17 / 3.9M; L3 9 / 8.0M; L4 4 / 13.8M; L5 2 / 85.9M; L6 1 / 219.6M (the v1 era, closed at the 09-30 switch); L7 1 / 185.7M (the sweeps, which straddle scan 64).
  - The cost: `path` 16.79 → 19.24 GB, `bysize` 14.82 → 16.75 GB (+14%; a path's versions no longer sit together in one segment).
- **8K-row groups,** zstd, dictionary on `kind`/`us`.
- **No per-scan bysize copy.** A version's bucket is fixed, because its size is. So one bysize file serves every scan: the same predicate, with liveness added.
- **Columns:** `depth, path, vf, vt, kind, size, n_files, n_children, n_desc, mtime, wts, wb, c2, c3, c4, us, last_read`. `-1` marks a value a v1 scan didn't have.

### 2.5 The group index: `.groups.parquet` on R2, no D1

Beside each sorted file is `<sort>.groups.parquet`: one row per row group, with
- exact bounds (`d`, `p`, `b`, `vf`, `vt` min and max);
- its segment and row span;
- the compact column-chunk metadata (`rg_json`).

This is the site's cold-footer format with four time columns added, read in `pq` mode. There is one index per sort per generation, not one per scan (~141K groups, ~21 MB), and nothing goes in D1. A Worker reads a footer group's bounds columns to plan, and `rg_json` only for the groups it selects (`readFooterJson`). The per-scan cold tier benefits from that change too.
- An interval store's footer serves every scan, so its decoded footer groups are cached per isolate without the scan in the key, and a decode in flight is shared (`footerOnce`).
- Decoded footer groups have an LRU of their own (12 MB). In the row groups' LRU they evicted the groups a warm read wanted.

### 2.6 Reads

Every read is today's `index.ts` plan, with one more conjunct on each group (`vf_min ≤ D < vt_max`) and on each row (`vf ≤ D < vt`):
- **Subtree / treemap at (P, D):** P's row, then `bysize` (`readSizeRects`). Planning `path` too is skipped for subtrees over `IV_PATH_PLAN_ROWS` (256K rows): on R2 footers, planning `path` decodes most footer groups for a read it never picks.
- **Diff (D1, D2):** both sides are as-of reads at one threshold; the walk's lookups are `path` point reads, one plan per side per level (`readAsks`).
  - Both sides read the same files. A row group being fetched for one side is waited for by the other (`groupsInFlight`, keyed without the scan), and each side caches its own scan's live rows. The same holds for a view and its walk.
  - Version boundaries also define a **change-only read**: the paths under P that differ between the two scans are exactly the versions with `vf` or `vt` in `(D1, D2]`. The `path` sort's layout can't prune by time, though: its open segment's `vf` ranges span every scan. Measured over 52 diffs, that read selects a median of 2,818 groups (117 MB), against 260 for the two as-of reads; at the root, 38,803 against 359.
  - The change list that is cheap is the per-scan runs (§2.7): each run *is* one scan's changes. So a change-only diff reads the runs for the scans in `(D1, D2]`, and needs a `vf`-ordered cut only for spans older than the runs.
- **Series:** P's versions *are* its history, so one `path` point read can replace one read per scan. Not wired in.
- **Filtered views:** phase 1 searches the scan's own search sidecars; phase 2 reads the interval store.
- **Lenses:** age (`d`) and read recency (`a`) come from the row.
- **User lens, owner pools, owner totals, owner prefixes, class scopes:** the slice sorts (`slices(env)` maps `path`/`bysize`/`bysize-user` to `slices`/`slices-bytotal`/`slices-bysize-user`).
  - A lens view's assigned regions take their totals from the folded `bysize` (one exact row per path). A path-first read of the slices selects a run per depth per dyadic segment, and a big user's root regions went past `decodeSpans`' cap. A held date's v1 `user` and coarse sorts are refused, so an owner read of a held date never mixes in per-scan rows. A generation without slice sorts still falls back to per-scan stores.
  - `intervalSlices.test.ts`: over three scans built from the per-scan fixtures (`v2-slices`, `v2-lens`, `v2-slices`), every lens view, owner pool, root total and lens or pool diff equals the per-scan reader's, with no per-scan read. One deliberate difference: a path that is both an object and a prefix is `dir` (the per-scan reader takes whichever row it merged last).

### 2.7 Per-scan append and compaction (`static_append`'s tiering)

For each scan D, after its per-scan sort is cut, as today:

1. **Append** (256 key ranges, Batch): `append_intervals(prev = the range's open versions, new = D's per-path rows)`, with the §2.2 key. This writes the next open state (scratch bucket, newest only) and D's delta: opened versions plus close records (the closed version's row with its final `vt`).
2. **Run:** cut the delta into both sorts, with `.groups.parquet`, under `interval-store/<gen>/deltas/<scan>/`.
3. **Merge runs on a binary counter.** When the two newest runs have the same level they merge (`pyrmts.runs.merge_parquets`; identity `(depth, path, vf)`, `vt` = min).
   - A reader plans the base and every live run, and combines rows of equal identity by the smallest `vt` before testing liveness (`interval_read.combine`).
   - A close record keeps its version's size, so the same plan finds both the record and the row it closes.
4. **Manifest** `manifests/<scan>.json`, uploaded last.
5. **Compaction** into a new generation at level 5, or on demand.

Measured size of a scan's delta (§6): ~2–3M opens plus ~1–2M close records. That's ~4M rows, ~0.12 GB in both sorts, against 21.7 GB for a per-scan path store.

## 3. Not built

- The change-only diff, and the one-read series.
- **A floor on the dyadic level.** A historical root still reads one group per small block (L0–L3: ~4 groups of 8K rows for a few rows over the threshold). Putting levels below 4 into L4 would cut that to one; untested, needs a re-cut.
- A parity sample (`interval-store verify`) over the slice sorts at gcs scale; the fixture parity above covers the reader.
- Shrinking close records. Today a close record is the full row. Identity + `vt` + `size` would do; a sweep scan closes 58–126M versions.
- The per-scan append in the job (§2.7), for the slice versions as well as the folded ones, and cw.

## 4. Retention of per-scan artifacts (proposal — Ryan decides)

**The gate.** A scan's per-scan artifacts become deletable only once all three hold:
- its reconstruction digest equals that scan's per-path rows (§6; every scan passes today);
- the view parity sample passes;
- the site reads the interval store (§5 phase 3).

| Artifact | Proposal | Why |
|---|---|---|
| per-scan path store (`index/<gen>/path-index*.parquet`, `.groups.*`, `attr.tsv`) | keep the **latest 7 scans**, delete older | the interval store serves every scan; the newest stay as fallback while it is new |
| D1 `index_row_groups` for scans the store serves | stop syncing; retire existing rows with `index-gc` once nothing reads them | the store's group index is on R2 |
| `dir-cache/` | keep the latest 7 scans | only the next scan's attribution reads it |
| raw listings (10.7 GB per scan) | **keep**, Coldline after 30 days | ground truth: a store bug is only recoverable from them |

**What that saves:**
- per scan, ~10.7 GB of listings plus the store's ~0.12 GB delta, instead of 51 GB;
- the 3.3 TB backlog falls to ~0.76 TB of listings plus 31.6 GB of store, ~0.33 TB of it in recent per-scan copies.

**How deletion runs:**
- `oa-gcs-usage-dvx` has 7-day soft delete, so a deletion is recoverable for a week.
- Deletion is an explicit job step gated on the verify marker, never a blind lifecycle rule. A lifecycle rule at ≥ 60 days can backstop it.

## 5. Migration

1. **Build and verify** (done for 71 scans, §6).
2. **Dual-read behind a flag** (done on dev; on `cloud` since v4). The current generation is `INTERVAL_STORE_GEN="2026-10-09b"` with `INTERVAL_STORE_REV` unset; `2026-10-09` + rev 2 is the v3 generation, still on R2.
   - `PATH_STORE=opt-in`: only requests with `ps=iv` read the store (`withPathStore`). `PATH_STORE=intervals` makes it the default.
   - Cache keys carry `iv:<gen>[@rev]` (`pathGens`). `INTERVAL_STORE_REV` re-keys every cache after an in-place re-cut.
   - Anything the store can't serve falls back to per-scan stores: a lens, an owner pool, a scan the store doesn't hold.
   - A parity probe (`interval-store verify`) runs per scan.
3. **Per-scan append in the job,** then the flag on in prod. The per-scan write continues meanwhile.
4. **Retire per-scan writes and apply §4,** after ≥ 14 scans of parity passing. The change-only diff and the one-read series land here.

## 6. Prototype, measured (2026-10-09)

**Generation `interval-store/2026-10-09`:** 71 scans, 2026-07-30 → 10-09; v1 indexes through 09-29, v2 store generations after.

**Build** (`dt-cloud interval-store build`, 32 spot n2-highmem-16 tasks × 256 key ranges, ~41 task-hours; then `fold`, `cut`):

| | Rows | Bytes |
|---|---:|---:|
| per-scan per-path rows, all 71 scans | 20,231,739,106 | ~1.5 TB of path stores |
| path versions (`pv`) | 1,118,487,204 | 14.1 GB (range files) |
| read-day versions (`rd`) | 40,026,364 | 0.22 GB |
| folded versions (served) | 1,156,324,516 | `path` 16.79 GB, `bysize` 14.82 GB, groups 2 × 21.6 MB |
| open at 10-09 (= its per-path rows) | 609,228,304 | |

- **Versions by kind:** objects, 583M versions over 576M paths (1.01 per path; objects are only in the v2 scans, 9/30 onwards); directories, 536M over 223M (2.4 per path).
- **Churn per scan,** opened / closed path versions:
  - the v1 era: median 149K / 123K;
  - 09-30, the v1 → v2 switch: 777M / 217M (every row reopened with objects and structural counts);
  - v2 scans: 1.9–4.5M opened, 0.6–1.9M closed;
  - sweep scans: 10-05 closed 58.1M, 10-06 closed 125.7M.
- **Per-scan reconstruction:** for every key range and every scan, the count and Σ hash of the scan's per-path rows equal those of the versions live at it. **256/256 ranges, 18,176/18,176 (range, scan) pairs equal**, for path versions and read days. The fold equals versioning with `last_read` in the key on the fixtures (`test_fold_equals_versioning_with_read_days`).

**View parity** (`interval-store verify -P gcs`, 4 tasks). Each sampled scan's views are computed twice:
- from that scan's own per-scan `path` sort (independent SQL, exact per-path totals);
- by the reference reader over the served store.

Sample: 20 scans; per scan, the root, a depth-1 band, two buckets, and dirs at depths 2–9. Compared: every tile, every `(other)`, every field (`k b o d a cb us`, and `f` where the scan knows its children). Plus diffs between consecutive sampled scans.
- **Views: 240 / 240 equal**, over all 20 sampled scans (07-30 → 10-09; v1 and v2), ~330K tiles. 216 of them (292,733 tiles) are in `verify/t*.jsonl`; the rest are 10-03 and 10-05 from the task logs, whose task was cancelled on a slow reference lookup. 36 views on 10-06 / 10-08 / 10-09 ran against the folded store (read days from the rows).
- **Diffs: 64 / 64 equal** between consecutive sampled scans, over ~200K diff rows and ~10K changed paths. The sweep scan 10-05's diff was left out: its reference does one SQL lookup per one-sided path, and it was the cancelled task.

**Read cost per view,** row groups / rows / bytes decoded. The per-scan figures are the per-scan Worker's plan from that scan's footer index; the interval figures are the store reader's actual reads.
- 180 views with both costs, across 15 scans. Medians: row groups 35 (interval) / 22 (per-scan); rows decoded 287K / 180K; **bytes fetched 3.8 MB / 5.6 MB** (sums 731 MB / 860 MB).
- By era, median bytes (interval / per-scan): v1 root 1.7 / 4.0 MB, v1 subtrees 3.8 / 5.7 MB; v2 root 5.0 / 0.8 MB, v2 subtrees 6.8 / 2.2 MB.
- Rows decoded, v2 roots: 598K / 33K.
- **Where the interval store reads more:** historical v2 reads, which decode closed versions of their paths (the time-partitioning in §3).
- **Where it reads less:** v1-era reads. Its rows are per path, with fewer columns, and per-scan v1 coarse/fine tiers can't threshold.

**Dev timings** (dev.gcs.oa.dev; the store on R2 `oa-gcs-usage-index`; Server-Timing `total` in ms, wall in parentheses). Each pair of columns is cold then warm; warm is the same read with the response cache keyed apart.

| View | Interval cold | Interval warm | Per-scan cold | Per-scan warm |
|---|---:|---:|---:|---:|
| root, 10-09 | 2,196 (3,669) | 124 (915) | 731 (1,440) | 337 (485) |
| `marin-us-central2`, 10-09 | 370 (1,151) | 423 (669) | 667 (1,355) | 320 (518) |
| root, 10-03 | 1,136 (3,388) | 148 (272) | 1,230 (1,789) | 331 (461) |
| root, 09-15 (v1) | 880 (3,086) | 109 (284) | **413 query too wide** | 413 |
| `marin-us-east5`, 09-15 (v1) | 546 (2,228) | 163 (401) | 2,544 (5,273) | 884 (1,757) |
| `marin-us-central1/checkpoints`, 10-09 | 2,953 (4,709) | 1,114 (2,768) | 1,201 (4,241) | 716 (2,343) |
| diff 10-08 → 10-09, root | 5,170 (6,040) | 1,412 (2,969) | 2,359 (3,508) | 594 (802) |
| diff 10-01 → 10-09, `marin-us-central2` | 5,035 (7,088) | 3,644 (4,489) | 2,698 (4,174) | 1,384 (2,966) |
| filtered `q=checkpoints`, root, 10-09 | 3,589 (5,356) | 1,221 (2,924) | 2,169 (3,086) | 1,238 (1,575) |
| filtered `q=tokenized`, `marin-us-central2` | 1,019 (2,610) | 381 (660) | 646 (1,981) | 265 (527) |

- **Subtree views are at parity or faster once warm,** and they serve v1-era roots that per-scan reads refuse.
- **Diffs are 2–3× slower.** The walk's one-sided point lookups each plan R2 footer groups, where per-scan does them as one D1 query. This is the next lever: batch the lookups per level, or use the change-only read.
- **Cold numbers carry a fresh isolate's footer decodes** (`footer` 0.1–2.3 s summed).

### 6.1 Generation `2026-10-09b` (v4): dyadic segments, slices, reader sharing

**Built from the same range files** (`cut -g 2026-10-09 -O 2026-10-09b`; nothing in `2026-10-09` was rewritten). Same answers: every unscoped view and diff measured below has the same tree as on `2026-10-09`, up to the order of equal-sized siblings.

| File | Rows | Bytes | Groups |
|---|---:|---:|---:|
| `path` | 1,156,324,516 | 19.24 GB | 141,227 |
| `bysize` | 1,156,324,516 | 16.75 GB | 141,227 |
| `slices` | 1,156,339,350 | 19.24 GB | — |
| `slices-bysize` (per slice; superseded by `slices-bytotal`, unread) | 1,156,339,350 | 16.74 GB | — |
| `slices-bytotal` | 1,156,356,048 | 18.34 GB | 141,230 |
| `slices-bysize-user` | 1,156,339,350 | 16.74 GB | — |

- **Slice build** (`build -S`, 32 spot n2-highmem-16 tasks, 18.5 task-hours): 256/256 ranges and **18,176/18,176 (range, scan) pairs reconstruct exactly**.
- **On R2:** 36.0 GB of folded sorts and 71.1 GB of slice sorts, 16.7 GB of which is the unread per-slice `slices-bysize`.
- **`fold -S`** (8 tasks): 1,156,339,350 slice versions → 1,156,356,048 pieces with their path's total.

**Local timings,** in ms: the TS reader (`buildView` / `buildDiff`) run in Node on the laptop, against R2 (the interval store) and GCS (per-scan stores). Per-scan footers come from a local copy of gcs's D1 (row groups through 10-04). Cold means a fresh module graph and colo cache; warm is the same read again. Each figure is the best of 2. The laptop is a long way from both stores, so absolute times are higher than a Worker's; compare within a row.

| View | Interval v3 cold | v3 warm | Interval v4 cold | v4 warm | Per-scan cold | Per-scan warm | v4 R2 MB |
|---|---:|---:|---:|---:|---:|---:|---:|
| root 10-04 | 3,267 | 45 | 1,750 | 22 | 853 | 82 | 4.6 |
| `marin-us-central2` 10-04 | 3,297 | 583 | 1,947 | 378 | 961 | 444 | 6.4 |
| root 10-01 | 3,141 | 135 | 1,738 | 27 | 813 | 93 | 4.3 |
| root 09-15 (v1) | 2,499 | 29 | 1,785 | 27 | 1,079 | 653 | 4.4 |
| `marin-us-east5` 09-15 (v1) | 3,677 | 149 | 2,482 | 32 | 1,035 | 106 | 6.4 |
| `marin-us-central1/checkpoints` 10-04 | 5,577 | 2,095 | 3,921 | 1,770 | 1,502 | 864 | 11.1 |
| diff 10-03 → 10-04, root | 3,736 | 1,735 | 2,388 | 786 | 1,290 | 118 | 8.5 |
| diff 10-01 → 10-04, `marin-us-central2` | 5,685 | 1,689 | 2,973 | 1,199 | 1,583 | 868 | 10.3 |
| root 10-09 | 1,941 | 21 | 1,400 | 21 | — | — | 2.8 |
| diff 10-08 → 10-09, root | 4,962 | 2,676 | 3,216 | 1,337 | — | — | 8.7 |
| lens `user:michael-ryan`, root 10-04 | per-scan | | 5,293 | 2,716 | 7,896 | 8,383 | 24.7 |
| pool `unowned`, root 10-04 | per-scan | | 2,113 | 71 | 901 | 104 | 6.0 |
| lens `user:michael-ryan`, root 09-15 (v1) | per-scan | | 6,290 | 4,065 | 3,520 | 3,082 | 29.6 |
| diff 10-03 → 10-04, lens `user:michael-ryan` | per-scan | | 6,852 | 4,820 | 16,702 | 18,841 | 31.7 |

- **Historical reads now decode about what the newest does.** At root 10-04, 12 row groups are read (v3: 28; the newest scan: 6; per-scan: 6). The extra groups are one per small dyadic block (L0–L3) plus L7.
- **Owner reads are served from the store.** The v2 lens root and lens diff have the per-scan tree and rows exactly, and are faster. The unowned pool differs only in float rounding of one `(other)`'s class bytes.
  - The v1 lens (09-15) has the same tile bytes. Its `(other)` tiles differ in `f`/`cb`/`d`, because per-scan v1 reads coarse tiers.
  - Two of its tiles show `o` doubled: an unread region with no row gets the manifest's objects twice (`readView`'s unread-region loop starts at the region itself). This is a pre-existing reader bug that the store's exact reads expose. Not fixed here.
- **Diffs are still slower than per-scan,** cold 1.8–1.9× and warm well behind. Most of the cold gap is footer decode: the store's footers come from R2 groups (~1 s of a cold diff), where per-scan plans in D1. The warm gap is the row-group LRU (24 MB): a diff's ~40 groups of mostly-live rows don't fit, and in v4 groups hold live rows. Levers:
  - a floor on the dyadic level (§3);
  - a larger LRU for interval handles;
  - the change-only diff over per-scan runs (§2.6).
- **Recurring cost of the slice sorts, per scan, once the append runs (§2.7).** A scan's slice delta is about the folded one (~4M rows), so ~0.12 GB per sort. The five served sorts are then ~0.6 GB of runs per scan to copy GCS → R2, about $0.07 of egress per scan (~$2/month at one scan a day), against ~$0.04 for the folded sorts alone. Writing the runs to R2 directly from the job would avoid the egress.

## 7. The per-scan `bysize` undercount (a prod bug this found)

**The bug.** The per-scan Worker thresholds `bysize` reads per owner-slice row: `readSizeRects` keeps `size ≥ thrAt(depth)` per row, and gcs dir rows are `(path, usr)` slices. Two consequences:
- a drawn directory's `b`/`o`/`a`/`us` count only its slices over the threshold;
- a directory whose slices are each under the threshold, but whose total clears it, isn't drawn at all.

The missing bytes land in the parent's `(other)`. Totals at the root are unaffected (the root reads every slice), but tiles are wrong.

**Measured on dev, which reads prod's per-scan data:** the root view at 1280×768, per-scan `bysize` against exact per-path totals (the interval store, equal to the per-scan reference):

| Scan | Threshold | Tiles | Tiles short | Tiles missing | Σ shortfall over tiles | Bucket tiles short |
|---|---:|---:|---:|---:|---:|---:|
| 09-30 | 41.5 GB | 4,430 | 94 | 5 | 1.9 TB | 197 GB, 434,570 objects |
| 10-01 | 41.6 GB | 4,429 | 94 | 5 | 1.9 TB | 197 GB |
| 10-03 | 40.6 GB | 3,967 | 94 | 6 | 1.9 TB | 197 GB |
| 10-05 | 39.3 GB | 3,657 | 93 | 6 | 1.95 TB | 197 GB, 489,793 objects |
| 10-07 | 39.3 GB | 3,643 | 92 | 6 | 1.80 TB | 197 GB |
| 10-09 | 39.5 GB | 3,653 | 92 | 6 | 1.80 TB | 197 GB, 489,799 objects |

- **Worst tiles:** `marin-us-central1/data/datakit/normalized/nemotron_cc_v2_1` 421 of 2,653 GB short (16%); `…/normalized/cp` 153 of 962 GB; `…/normalized/finepdfs` 135 of 832 GB; `marin-us-west4/checkpoints/delphi-prefix-sources` 157 of 345 GB.
- **By bucket:** `marin-eu-west4` 98 GB, `marin-us-east5` 55 GB (55,633 objects), `marin-us-central2` 16 GB, `marin-us-west4` 11 GB (295,477 objects), `marin-us-east1` 9 GB, `marin-us-central1` 7 GB.
- **Which scans and views:** every v2 scan (09-30 onwards, where the per-scan reader picks `bysize`), and any thresholded view that reads `bysize`: root, buckets and big dirs. The verify sample's emulation found 8 of its first 24 v2 views affected.
- **Not affected:** v1 scans (coarse/fine tiers, read by `path`) and `depth=1` bands read from `path`.
- **Fixes:**
  - in the per-scan store, give each slice row its path's total in a sort column (bucket on the path's total), or read every slice of the selected paths in a second pass;
  - the interval store's one-row-per-path design has no such case for unscoped reads, and its owner-slice size sort is keyed on the path's total (`slices-bytotal`).

## Open questions

- Should ClickHouse (ch-store) feed from this store instead of its own ingest, so there is one history?
- Should the raw listings themselves be interval-coded (objects, `generation` as the change key), ending the per-scan write entirely?

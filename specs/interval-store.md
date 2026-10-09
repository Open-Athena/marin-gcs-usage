# Interval store: change intervals in place of per-scan path stores

**Status:** draft v2, 2026-10-09 (base work, branch `interval-store` off `cloud`). v1 (2026-10-07, unreviewed) recommended object-only intervals plus keyframes; v2 decides for **objects and dirs as one interval table, no keyframes**, on the evidence of the static name index build (§1), and reports a prototype over every gcs scan (§10). Nothing here deletes anything; §8 is a proposal for Ryan.

**Goal (Ryan, 2026-10-09):** *"I'm more concerned about parts of the stack where we're storing a mostly-redundant full scan's worth of data for every new scan that comes in, as opposed to leveraging the mostly-constant-over-time nature of our subject fleets."* A day on which little changed should cost little: the store grows with the fleet's changes, not with its size.

## 0. Where we are

Every gcs daily scan writes a full copy under `gs://oa-gcs-usage-dvx/listing/<date>/`, kept forever:

| per scan | bytes | what reads it |
|---|---:|---|
| path store `index/<gen>/path-index{,-bysize,-bysize-by-user}.parquet` (+ `.groups.{json,parquet}`, `attr.tsv`) | 21.7 GB | the site: subtree/treemap, diff, series, filtered views' phase 2, age and read lenses, `/staged`; footers in D1 (latest 30 scans) or `.groups.parquet` (older) |
| `dir-cache/` | 19.1 GB | the next day's attribution (re-attribution input) |
| raw listings `<bucket>/*.parquet` | 10.7 GB | `path-index` (the store is cut from them), sweep manifests |

`listing/` is 3.32 TB today (71 scans, 2026-07-30 → 10-09), ~51 GB more per day: ~$66/month now, ~$370/month after a year at GCS Standard. D1 holds ~218 MB of footer rows per path-store scan (three sorts at 8K-row groups), which is why only 30 scans fit and the rest read a cold footer.

The fleet barely changes. Over 16 daily scans (2026-09-15..30) the object SCD-2 was 1.027 scan-equivalents (new rows 0.21%/scan), dir slices 1.057 (`path-store.md` §4.7). The static name build (§1) puts the whole 70-scan history of every path, objects and directories, at 1.15B versions where a single scan is ~780M rows.

## 1. Evidence: the static name index build

`static-names/2026-10-08` (`specs/architecture/static-name-search.md`) ran pyrmts' gaps-and-islands kernel over every gcs scan's `path` sort, keyed `(depth, path, usr)`, objects and dirs, with every value a change column (`kind, size, n_files, n_children, n_desc, mtime, mtime_mean, last_read, c2..c4`):

- **1,153,480,980 versions for 70 scans, 11.1 GB** (9.6 B/version, zstd), against ~780M rows (~21.7 GB of path store) for one scan. About **1.6 versions per path** over 70 days.
- Coalesced on `(size, n_files)` alone: **797,994,947** versions (0.69×). The 355M difference is versions opened by a change in something other than bytes and objects: `last_read`, the mean stamp, storage classes, structural counts.
- Built on Batch in 38 min wall (32 spot n2-highmem-16 tasks, 256 key ranges, ~$5); every scan's opened (1.15B) and closed (545M) versions equal ClickHouse's independent ingest, counts and digests.
- **Daily append is solved** (`specs/static-daily-append.md`, `cloud/src/dt_cloud/static_append.py`): `pyrmts.intervals.append_intervals` over the open versions only, a per-day `cdelta` of opens and close records, runs merged on a binary counter by `pyrmts.runs.merge_parquets` (min-`vt` combine), immutable manifests, ~$0.40/day. 2026-10-09 opened 1.29M versions and closed 388K (coalesced).

## 2. Decisions

### 2.1 Rows: one interval table of objects *and* directories, one row per path

**Decided: directories are stored as intervals, beside objects.** v1 proposed object-only intervals with directory totals rebuilt at read time from keyframes plus signed object deltas, to avoid "multiplying daily churn by the ancestors touched". The static build measured that cost: with directories included the whole history is ~1.6 versions per path. A day's churn touches each changed object's ancestors once, and ancestors are shared, so the ancestor fan-out of ~1.3M daily object changes is bounded by the directories that contain them, not by changes × depth. In exchange:

- a read at any date is **one range read of stored totals**, exactly as today: no keyframe, no "sum the deltas of every changed descendant up the tree" (which costs a read of all changed objects under P, unbounded for a hot subtree);
- the treemap's question — every path under P whose total clears a per-depth threshold — stays a 3-sided range query over one sorted file (`path-store.md` §1.3), now with a fourth side, `vf ≤ D < vt`;
- there are no keyframes, so no day is a full copy.

**Row identity is `(depth, path)`**, one row per path: a path's owner slices are folded into the row (`us`, below), and a path that is both an object and a prefix is one row whose `kind` is `dir`. This differs from today's store, which keeps one row per `(path, usr)` slice:

- **Exact thresholds.** Today's `bysize` read thresholds *per slice* (`readSizeRects`: `size ≥ thrAt(depth)` per row), so a directory whose owners each hold less than the threshold is dropped although its total clears it, and a drawn directory's bytes count only its slices over the threshold; the remainder lands in the parent's `(other)`. One row per path has no such case. §10 measures how often the per-scan read is off.
- **Fewer versions.** A slice row versions when that slice changes; a path row versions when any slice does. Slices of one path mostly change together (their path's descendants changed), so folding them removes rows.
- **Lenses still work.** The user lens needs every slice of a user's paths with all values (U's bytes, objects, ages, classes); that is a second interval table keyed `(usr, depth, path)` sorted `(usr, ⌊log2 size⌋ desc, path, vf)`, today's `bysize-by-user` as intervals (§3.6). Not prototyped.

### 2.2 Version key: what opens a version, what is carried, what lives apart

| column | role | why |
|---|---|---|
| `kind`, `size`, `n_files`, `n_children`, `n_desc`, `mtime`, `wb`, `c2..c4`, `us` | **change** columns | a different value means a different tile |
| `dr` = `round(wts / wb)` (the size-weighted mean stamp, to the second) | **change** column | the age lens's `d` |
| `wts` = Σ `mtime_mean · mtime_w` | **carried** (`first`) | the exact sum drifts in its last bits between scans when nothing changed (float sums in a different order); the change test is on `dr`, the value kept is the first scan's. A drawn `d` is `round(wts / wb / 86400)` days: a sub-second difference can't move it in practice (§10 counts) |
| `last_read` | **its own interval table** (`reads`) | read days move daily on read paths without any byte changing; in the key they would reopen every read directory's version every day it is read. A separate `(depth, path) → last_read` table versions only the read paths, and a view looks up its tiles' read days (one point read per tile set, §3.5) |

`us` is the owner slices as one string: `''` (no named owner), the owner's name (exactly one row, named: its bytes are the path's), or JSON `[["usr", bytes], …]` by name, every named owner including zero-byte ones (the reader's per-user map holds them). Unnamed bytes are the total minus the named.

pyrmts' `carried=` is used only for `wts`. Carrying `mtime` or `last_read` (`first` / `last`) would make a mid-run date's value wrong; neither is acceptable for a lens that colors by it.

### 2.3 Encoding

`[vf, vt)` in epoch seconds of the scan date (`vt` = 2106-01-01 while open: `OPEN`, as the static build). A version opens at its first scan and closes at the first scan after its run: a path absent from a scan closes, a path that comes back opens a new version. Scans are an ordered list (`scans.json`: id, source, epoch); a date with no scan (2026-08-10) has no stamp and is never asked for. A read at `D` takes rows with `vf ≤ D < vt`.

### 2.4 Layout: three sorted files per generation, each in two segments

| file | rows | order | serves |
|---|---|---|---|
| `path.parquet` | path versions | `(depth, path, vf)` | P's own row, children by name, small subtrees, the diff's point lookups, change scans |
| `bysize.parquet` | path versions | `(⌊log2 size⌋ desc, path, vf)` (size 0 last) | every thresholded subtree read (the treemap at any P), top-K |
| `reads.parquet` | `last_read` versions | `(depth, path, vf)` | the read lens's tile lookups |

- Each file is **two segments**: the versions still open at the generation's last scan, then the closed ones, each sorted as above and cut at the segment boundary (no row group mixes them). A read at the newest date prunes every closed group by `vt_max ≤ D`, so the common read decodes exactly what a per-scan copy would. A historical read decodes the closed segment's groups too; their waste is the versions of the same paths not live at D (§10 measures it).
- **8K-row groups**, zstd, dictionary on `kind`/`us`. The bucket of a version is fixed (its size is), so `bysize` needs no per-date copy: a size-desc read at any D is the same predicate with liveness added.
- Columns: `depth, path, vf, vt, kind, size, n_files, n_children, n_desc, mtime, wts, wb, c2, c3, c4, us` (`-1` where a v1 scan had no structural counts or `mtime`); `reads`: `depth, path, vf, vt, last_read`.

### 2.5 The group index the Worker plans from: `.groups.parquet` on R2, no D1

Beside each sorted file, `<sort>.groups.parquet`: one row per row group with exact (never truncated) bounds `d_min/d_max, p_min/p_max, b_min/b_max, vf_min/vf_max, vt_min/vt_max`, its segment, row span and the compact column-chunk metadata (`rg_json`) the Worker revives — the cold-footer format the site already reads in `pq` mode (`path-store.md` §1.6), plus the four time columns. 512 footer rows per footer group with statistics on the bound columns, so the Worker decodes only the footer groups whose bounds can pass.

**It lives on R2 beside the store, not in D1.** One generation has one index per sort, not one per scan: ~120K groups per sort ≈ 20 MB, read by range. D1's footer table (~218 MB per scan today; the reason for `INDEX_RETAIN=30` and the cold tier) stops growing with history. A date's pointer is the generation's `scans.json` (or a daily manifest, §4), a few KB.

### 2.6 Reads

All reads are today's `index.ts` plans with one more conjunct on each group (`vf_min ≤ D < vt_max`) and on each row (`vf ≤ D < vt`):

- **Subtree / treemap at (P, D):** P's row (`path` point read; the root sums its depth-1 rows), the threshold from P's total, then `planSubtree`'s choice between `path` (`readRects`) and `bysize` (`readSizeRects`, floor `min(thrAt(dLo), thrAt(dHi))`), whichever plans fewer rows. The row test `size ≥ thrAt(depth)` is exact now (§2.1).
- **Top-K / bysize thresholds:** the per-version `⌊log2 size⌋` sort is the answer to "bysize without a per-scan bysize copy": each version's bucket is fixed, so one file serves every date. Per-directory rollups by date are not needed for the treemap; §1.5's histogram (`size_hist_n`) can be a change column of directory rows when §2.3's top-N is built.
- **Diff (D1, D2):** both sides read at one threshold as today (two as-of reads of the same groups, mostly; the walk's one-sided lookups are `path` point reads at the other date). Version boundaries also give a **change-only read**: the paths under P that differ between D1 and D2 are exactly the versions with `vf ∈ (D1, D2]` or `vt ∈ (D1, D2]` — a diff that lists only changed paths needs no unchanged row at all. §10 measures its size; wiring it into `buildDiff` is phase 4.
- **Series (`/api/series`):** P's versions are P's whole history: one `path` point read returns every `(vf, vt, size, n_files)` of P, instead of one read per scan (today's 8–29 s series reads every scan's footer and groups).
- **Filtered views' phase 2** read subtree rects through `planSubtree` and get the same as-of reads.
- **Age lens:** `d` from `wts / wb` per tile, as today.
- **Read-recency lens:** `a` per tile from `reads` at D: one planned read of the groups holding the tile paths (a view's tiles are a few thousand paths, contiguous within each depth).
- **`/staged`:** reads the latest scan's `path` rows for staged prefixes (point/range reads); same shape.

### 2.7 Daily append and compaction (reusing `static_append`'s tiering)

Per scan D, after the per-scan sort is cut (as today; it is the input until listings are interval-coded directly):

1. **Append** (256 key ranges, Batch): `append_intervals(prev = the range's open path versions, new = D's per-path rows, key = (depth, path), change = §2.2, carried = {wts: first})`, and the same for `reads`. Outputs per range: the next open state (scratch bucket, newest only, as `copen`), and the day's delta: opened versions (`vt = OPEN`) and **close records** (the closed version's row with its final `vt`).
2. **Run**: the day's delta cut into the three sorts (with `.groups.parquet`) as a run under `interval-store/<gen>/deltas/<D>/`. A run is small: ~1.3M opens + ~0.4M closes on 2026-10-09 (coalesced counts; §10 has this keying's).
3. **Binary counter** of runs (`static_append` §4): when the two newest runs have the same level they merge (`pyrmts.runs.merge_parquets`, identity `(depth, path, vf)`, `vt` = min). A read at D fetches the base and every live run's planned groups (⌊log2 n⌋ + 1 runs after n days) and combines rows with equal identity by the smallest `vt` before the liveness test. A close record carries the version's size, so it sits in the same bucket and path range as the row it closes: the same plan finds both.
4. **Manifest** `manifests/<D>.json` (base + runs, scans), uploaded last; the Worker lists `manifests/` and takes the newest (as `staticRuns.ts`).
5. **Compaction** at level 5 (32 days) or on demand: base ⊕ runs re-cut into a new generation's sorts (open segment = the then-open versions). Byte-identical to a rebuild (`static_append`'s `test_compaction_equals_full_build` shape).

Cost: the static chain's append is 16 tasks × 3.5 min; this one carries ~12 value columns instead of 2, so a few × that, still well under $1/day.

### 2.8 Storage and serving cost

Measured in §10 for 71 scans. The shape:

- **Total:** one generation ≈ (versions) × ~10–15 B per sort; two path sorts + `reads`. The static build's 1.15B full-key versions were 11.1 GB in one order.
- **Per day at the margin:** the day's delta (opens + close records) in three sorts ≈ (versions changed) × ~3 × 15 B — tens to low hundreds of MB, against 21.7 GB for a per-scan path store (and 51 GB for everything under `listing/<date>/`).
- **R2:** $0.015/GB-month storage, no egress; Class B reads $0.36/M. A view is 1 footer probe (colo-cached) + ~2–20 range reads; the same as today.
- **GCS → R2 egress** for the initial copy: ~$0.12/GB once; daily runs are MB.

## 3. Not in the prototype

- **3.6 Lens and owner pools:** the per-slice table `(usr, depth, path)` for the user lens (`bysize-by-user` analogue) and owner pools. Until it exists, a lens/pool view reads the per-scan store (the flag falls back).
- **`size_hist_n`** (path-store §2.3) as a directory change column.
- **cw**: same design; its object churn (~3–4%/12 h) is higher than gcs's, its dir churn lower; measure before switching.

## 8. Retention of per-scan artifacts (proposal — Ryan decides)

Once the store is verified for a date (its reconstruction digest equals that scan's per-path rows, §10, and the view parity sample passes) and the site reads it (§9 phase 3):

| artifact | proposal | why |
|---|---|---|
| per-scan path store (`index/<gen>/path-index*.parquet`, `.groups.*`) | keep the **latest 7 scans**, delete older | the interval store serves every date; the newest scans stay as a fallback while the store is new |
| D1 `index_row_groups` for scans the store serves | stop syncing new scans' footers; retire existing ones with `index-gc` once the reader no longer needs them | the store's group index is on R2 |
| `dir-cache/` | keep the latest 7 | only the next day's attribution reads it |
| raw listings (`<bucket>/*.parquet`, 10.7 GB/day) | **keep** (or move to Coldline/Archive after 30 days: ~$1.2 vs $4.0 per TB-month at Coldline) | the ground truth everything else is cut from; a store bug is recoverable only from them. Listings are 21% of today's per-day bytes |
| `attr.tsv` | keep the latest 7 | per-scan provenance sidecar, small |

With that: per day ≈ 10.7 GB of listings + the store's delta (≪ 1 GB) instead of 51 GB; `listing/` stops growing at ~5× the rate it does now, and the 3.3 TB backlog shrinks to ~0.8 TB of listings + the store. `oa-gcs-usage-dvx` already has 7-day soft delete, so a deletion is recoverable for a week. Deletion is an explicit job step gated on the verify marker, never a blind lifecycle rule; a lifecycle rule at ≥ 60 days may backstop it.

## 9. Migration

- **Phase 1 — build and verify (done in the prototype for 71 scans, §10):** the generation for every existing scan; per-scan reconstruction digests (every path, every scan) and view parity on a sample.
- **Phase 2 — dual-read behind a flag (dev only):** `PATH_STORE=intervals` on the dev preview: views, diffs and filtered phase 2 read the store; anything it can't serve yet (a user lens, an owner pool, a date it doesn't hold) falls back to the per-scan store. A parity probe (`dt-cloud interval-store verify`, and per-view body comparison on dev) runs against both on every new scan.
- **Phase 3 — daily append in the job** (§2.7), then the flag on in prod; the per-scan write continues meanwhile.
- **Phase 4 — retire per-scan writes and apply §8**, after ≥ 14 days of daily parity passing; the change-only diff and the one-read series land here.

## 10. Prototype (2026-10-09)

*To be filled with measured numbers.*

## Open questions

- Should ClickHouse (ch-store) be fed from this store instead of its own ingest, so there is one history?
- Should the raw listings themselves be interval-coded (objects only, `generation` as the change key) and the per-scan store cut from those, ending the per-scan write entirely?

[obs-axis-indexing]: obs-axis-indexing.md
[path-store]: path-store.md
[static-daily-append]: static-daily-append.md
[static-name-search]: architecture/static-name-search.md

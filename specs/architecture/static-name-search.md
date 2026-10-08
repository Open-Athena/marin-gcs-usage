# Static name search: rare terms without a server

Can the below-catalog ("rare term") name search run from static files on R2/GCS, read by a Worker, with interactive latency and a bounded worst case? Measured 2026-10-08 on the consolidated store (70 scans, 2026-07-30 → 2026-10-08). **Yes, with a suffix-ordered, denormalized postings layout**: every rare query is one contiguous byte range of at most a few MB, exact on every date, and the only server work left is the daily build.

## The query

Unchanged from `mega_names.answer`, because its first-hit rule is per row: a postings row (one version of one owner slice of a path) counts toward literal `q` on scan `D` when

1. its lowercase basename contains `q`;
2. it is live on `D`: `since(D) ≤ vf ≤ D < vt` (`vt` = its closure, 2106 while open; `since` = the scan's epoch start);
3. its lowercase parent path does not contain `q` (a matching ancestor already covers it);

and the answer is `sum(size)`, `sum(n_files)` per first path segment (bucket). Nothing joins rows to each other, so a reader that holds exactly the rows whose name contains `q` can answer any date by filtering.

## Why today's layout is slow cold

`m_nodes` is sorted by name. A rare term matches thousands to ~100K names scattered across 18 GB, so a cold query touches about one 256-row granule per name (see `mega-index.md`, "Below-T cost"). Granules of `m_nodes` the current plan touches, against the rows the answer needs:

| Term | Matching names | Rows needed (versions × occurrences) | Granules touched today | Smallest trigram list (names) |
|---|---:|---:|---:|---:|
| `gof` | 9,194 | 9,451 | 204 | 9,194 |
| `0.0.73` | 33 | 31,125 | 124 | 5,111 |
| `116.tok` | 1 | 26,226 | 103 | 6,120 |
| `11979` | 3,747 | 14,315 | 808 | 469,073 |
| `bb-` | 56,635 | 123,716 | 32,921 | 56,635 |
| `5418` | 33,932 | 109,281 | 14,639 | 514,133 |
| `48.parquet` | 42,402 | 104,216 | 37,427 | 495,135 |
| `nk080` | 96,312 | 96,724 | 69,936 | 849,154 |
| `rt-0003` | 17,636 | 166,608 | 888 | 1,817,184 |
| `s__marin-us-centr` | 1,830 | 228,729 | 894 | 7,653 |

(Full table for the 27 T-curve and probe terms: `job/ch-store/static-name-term-stats.sh` over `static-name-terms.txt`.) `nk080` needs 97K rows and reads ~70K granules (~18M rows); the cost is the scatter, not the result.

## The layout: suffix-ordered postings

One row per (suffix position of a name, postings version of that name): `(s, depth, path, usr, vf, vt, size, n_files)`, sorted by `s`, where `s` is the name's lowercase suffix from that position. Only suffixes of three or more characters are stored (one- and two-character literals are always in the catalog). The rows for literal `q` are exactly the contiguous range of suffixes starting with `q`; a name containing `q` twice contributes two rows of the same version, which the reader dedups by `(path, usr, vf)`.

Closures are folded in as `vt` (the version's interval `[vf, vt)`), so there is no second table and no join.

### Measured (prototype)

Prototype: every suffix starting with `541`, `nk0`, `48.` or `gof` (complete for any query starting with those three characters): 31.0M rows, built in ~105 s on the 32-vCPU VM (`static-name-proto.sql`), exported as zstd parquet, read from GCS by `static-name-query.py`, which fetches the footer once (in production a sidecar, below) and then **one ranged GET per query**:

| Term | Row groups (4K rows) | Bytes read | Rows read | Rows matching | Fetch (laptop → GCS) | Filter + sum (Python) | Exact on 4 dates |
|---|---:|---:|---:|---:|---:|---:|---|
| `5418` | 25 | 1.40 MB | 110,686 | 109,281 | 0.28 s | 0.62 s | yes |
| `nk080` | 24 | 3.07 MB | 104,658 | 96,724 | 0.33 s | 0.66 s | yes |
| `48.parquet` | 24 | 1.28 MB | 108,920 | 104,216 | 0.32 s | 0.59 s | yes |
| `gof` | 3 | 0.17 MB | 13,083 | 9,451 | 0.19 s | 0.07 s | yes |
| `54181` | 3 | 0.18 MB | 13,083 | 6,274 | 0.21 s | 0.07 s | yes |
| `48.parquet.crc` | 1 | 0.03 MB | 4,579 | 0 | 0.09 s | 0.01 s | yes |

"Exact" = equal per-bucket bytes and objects to `mega_names.answer(..., postings='m')` on 2026-10-06, 10-01, 09-15 and 08-15 (24/24). Today's ClickHouse path takes 3.8–3.9 s cold for `nk080`/`48.parquet` and over 5 s for `5418`. With 16K-row groups the over-read is larger (`gof` 0.35 MB) and the footer 5× smaller; 4–8K rows per group is the sweet spot.

### Full-scale size

From `name_spans` (all 139.6M names, 1.15B versions):

| | |
|---|---:|
| Name characters | 5.89B (mean 42, p99 125) |
| Suffix positions ≥ 3 characters | 5.61B |
| Suffix rows (Σ positions × versions) | **16.5B** (14.3 per version) |
| Parquet zstd, measured on the prototype | 25–28 B/row |
| **Estimated total** | ~410–460 GB at the prototype's 25–28 B/row; **built: 150.6 GB** (9.1 B/row, pyarrow zstd) |
| R2 storage at $0.015/GB-month | ~$2.3/month (as built) |

Almost all of each row is the `path` string. Storing a parent-path id with a dictionary would shrink it, but needs a second lookup for the first-hit test; not worth it at these prices.

### Bounding every query

The read for `q` is `Σ over names containing q of versions × occurrences` rows, all time. The cost-weighted census (`ch-mega-catalog-build -w rows`, `mega-index.md`) registers exactly the literals at or above a weight `V`; with the weight defined as these suffix-range rows, **every literal outside the catalog reads fewer than `V` rows**, about `V × 28` bytes: 2.8 MB at `V` = 100K, one range read. Two details:

- **Long queries.** The prototype truncates `s` to 24 characters; a non-member `q` longer than 24 could have a common 24-character prefix and an unbounded range. Store the full suffix (or ≥ 128 characters; names p99 is 125), which sorted zstd compresses well, so the range is always exactly `q`'s.
- **Multiplicity.** The weight counts a name containing `q` twice as two rows, matching what the range holds.

### Reading it from a Worker

- **Sidecar.** At 4K rows per group, 16.5B rows is ~4M row groups; their parquet footer metadata is ~1.3 KB each (measured), so files are split by suffix prefix (e.g. first two characters, a few hundred files of ~1–2 GB) and the per-group `(s_min, s_max, file, byte range)` index goes to D1 (or KV shards). That is the same footers-in-D1 pattern the path store already uses (`index-sync`), so a query is: one D1 lookup (the group range for `q`) → one R2 ranged GET (≤ a few MB) → decode, filter, sum.
- **Decode.** ~100K rows / 3 MB compressed decodes to ~25 MB with hyparquet, within a Worker's 128 MB; the CPU is the filter over ~100K rows (the prototype's 0.6 s is pure Python). Lambda or Cloud Run are not needed.
- **Worker → R2, measured 2026-10-08** (dev Pages Functions `/api/static-bench`, colo EWR, bucket `oa-gcs-usage-index` in ENAM; the prototype files under `proto/static/`):
  - one ranged GET, 56 KB–3 MB: first touch 130–840 ms, repeat typically 60–280 ms (outliers to ~840 ms); 4–6 ranges issued in parallel: ~230 ms total;
  - hyparquet decode (fzstd, 6 columns) of ~100–125K rows (`5418`, `48.parquet`, `nk080`): typically 150–430 ms, with spikes to 1.2–1.7 s; 4–22K rows: 10–75 ms;
  - footer parse: 9.0 MB (4K-row groups) takes ~0.9 s, 1.8 MB (16K) 0.2 s, which is why production reads the per-group index from D1 and never parses a footer;
  - so a ~100K-row term is ~0.3–0.7 s end to end (vs 3.8–5 s cold on ClickHouse); decode, not the fetch, dominates, and it scales with `V`.

### Daily upkeep

LSM-style, no server:

- **Delta files.** Each scan's opened versions (~2M/day) become suffix rows (×14.3 ≈ 30M rows, ~0.8 GB) in a small sorted delta file with its own sidecar; its closures (~1M/day, ≈15M suffix rows) are `(s, path, usr, vf, vt)` close records in the same file. A query reads its range in the base and in each delta (independent reads, issued in parallel, not dependent), and applies close records over base rows.
- **Compaction.** Weekly, merge deltas into the base for the affected prefixes (a sorted merge; DuckDB on GCP Batch), keeping at most ~7 deltas.
- **Inputs.** The day's opened and closed versions are what `ch-ingest` already computes; the same diff can come from consecutive path-index files with DuckDB on Batch, so ClickHouse is not required for upkeep either.
- **Initial build.** The prototype's 31M rows took ~105 s; the full 16.5B is ~530× that, roughly 3–6 hours of sort and export on the 32-vCPU VM or a Batch job, once.

## Pipeline: rebuilt from primary sources on GCP Batch

`dt-cloud static-names` (`cloud/src/dt_cloud/static_names.py`), driven by `job/static-names.sh` (us-east1, beside the data; the gcs job image pinned by digest; this checkout's committed `dt_cloud` tree staged content-addressed under `gs://oa-gcs-usage-dvx/static-names/src/<tree>/`), tracked by DVX stages in `static-names/<gen>/`. Nothing reads ClickHouse except the verification stages.

| Stage (DVX out) | What | Where it runs |
|---|---|---|
| `scans.json` | each scan date's newest `path` sort (`listing/<date>/[index/<gen>/]path-index.parquet`, the rule `ch-ingest` uses), pinned by GCS generation, size, md5/crc32c, with its source format (61 v1, 9 v2) | laptop (footer reads) |
| `ranges.json` | 256 `(depth, path)` key ranges of about equal input rows (row-group starts of the newest v1 and v2 scans, weighted by scan count), each a few conjunctive filters so DuckDB prunes every scan's row groups | laptop |
| `intervals.json` | per range: every scan's rows in the range, merged per key as `ch-ingest` merges them, then pyrmts' gaps-and-islands (a new version when any value changes, the weighted mean stamp compared to the second with banker's rounding, or the key was absent in between) → `intervals/r####.parquet` (`depth, path, usr, vf, vt` + values, sorted), `hist/` (suffix rows per 3-character prefix) and `digest/` (per scan: opened/closed counts and md5 sums) | Batch: 32 tasks × 8 ranges |
| `verify-intervals.json` | per scan, opened and closed counts and order-insensitive digests vs ClickHouse `m_nodes`/`m_closures` | the ch-store VM, read-only |
| `shards.json` | shard plan from the histograms: runs of 3-character prefixes of ≤ 50M suffix rows (a prefix is never split, so a literal's range is in one file), in 32 contiguous task groups | laptop |
| `suffixes.json` | `suffix-map` expands each range's intervals once into `(s, …, shard)` rows written per reduce task; `shards` partitions its task's rows by shard and writes each sorted `(s, path, usr, vf)` as `sx/s####.parquet` (8K-row groups, zstd, `usr` dictionary) plus its sidecar; `sidecar` concatenates `sidecar.parquet` `(file, rg, s_min, s_max, offset, length, rows)` (from the rows written, not truncated statistics) and checks the shards don't overlap | Batch: 32 + 32 tasks |
| `verify-answers.json` | `query` (sidecar → one ranged read per file → first-hit filter → per-bucket sums) vs `mega_names.answer(…, postings='m')` per (term, date) | laptop + VM |
| `r2.dvc` (side effect) | `r2-copy`: `sx/`, `sidecar.parquet`, `shards.json`, `scans.json` → R2 `oa-gcs-usage-index` under the same keys, skipping what is there with the same size and md5 | the ch-store VM (holds the R2 keys), `nice`d |

Outputs: `gs://oa-gcs-usage-dvx/static-names/<gen>/{scans,ranges,shards}.json, intervals/, hist/, digest/, sxmap/ (intermediate: 180 GB for 2026-10-08, deleted with its `sxmap-done/` markers once the shards were verified; the suffixes stage reruns map and reduce together), sx/, sidecar/, sidecar.parquet`. Everything is written through pyarrow in fixed row-group sizes under a total sort order, so a rerun over the same inputs and image is byte-identical. `append -f GEN` adds the next scan to a range's intervals (open versions × the scan, a full join) and writes the day's opened/closed versions as `delta/<date>/r####.parquet`, the daily delta source.

Run: `cd static-names/<gen> && PATH=$REPO/.venv/bin:$PATH dvx run verify-answers.json.dvc` (then `r2.dvc`).

### Validation runs (2026-10-08)

- **Local** (`cloud/tests/test_static_names.py`): intervals equal a sequential-ingest oracle (v1/v2 mix, duplicate v1 rows, absence gaps, rounding ties) at 1 and 4 ranges; ranges partition the key space; `append` is byte-identical to a rebuild (intervals and histogram files) with matching digests and delta; suffix rows equal brute force; the reader's per-bucket first-hit answers equal brute force for every date; the restated kernel equals pyrmts' `_intervals_sql`.
- **Intervals, real data**: ranges 0, 100, 200 (one task, n2-highmem-16) — 7.59M, 2.92M and 3.03M intervals in 383 s (first range: includes parsing all 70 footers), 247 s and 232 s. Per scan opened and closed counts and digests **equal ClickHouse on all 70 scans for all three ranges** (`nodes`/`closures` restricted to each range).
- **Append, real data**: ranges 0 and 100 built over 69 scans then appended 10-08 (range 0: 1,822 opened, 1,557 closed) are byte-identical (md5), intervals and histograms, to the 70-scan build.
- **Reproducibility**: the dev shards built twice by two code paths (one pass per task; then the map/reduce shuffle) are byte-identical, all 11 files and the sidecar.
- **Suffix shards, real data** (the three ranges' intervals, 13.5M versions → 513M suffix rows, 11 shards): 50M-row shards sort and write in ~55 s each; files are 12–17.6 B/row (pyarrow zstd), 6.9 GB for 513M rows; sidecar 62,674 row groups, 2.6 MB.

### Full build, 2026-10-08 (gen `2026-10-08`, 70 scans 07-30 → 10-08)

**Intervals**: one Batch job, 32 spot n2-highmem-16 tasks × 8 ranges, 38 min wall (2,291 s); 256 ranges at 150–841 s (median 193 s), 14.9 task-hours ≈ **$5 spot** (~$17 on demand). 1,153,480,980 intervals, 11.1 GB (9.6 B/version). **Verification: every scan's opened (1,153,480,980 in all) and closed (545,152,608) versions equal ClickHouse `m_nodes`/`m_closures` exactly, counts and digests, all 70 scans** (`verify-intervals.json`, 60 s on the VM).

**Suffix shards**: `suffix-map` (32 spot tasks × 8 ranges) 33 min wall (6,238 s of mapping in all; one range, 1,042 s, set the tail: tasks take 8 consecutive ranges, so the long-named podcast ranges cluster), then `shards` (32 spot tasks) 14 min wall; plan + sidecar merge on the laptop in seconds. **16,501,534,337 suffix rows** (= the plan's histogram total and the map's row count), **322 files, 150.6 GB (9.1 B/row)**, largest 1.47 GB (prefix `son`, 232M rows, the one prefix over the 50M target), 2,014,505 row groups; `sidecar.parquet` 38.9 MB. Batch for the whole build: at most ~45 spot VM-hours (32 VMs × each job's wall time, an upper bound) ≈ **$10–15**, plus ~3 on-demand VM-hours of dev runs.

**Answers**: `verify-answers.json`: **259 of 259 (term, date) pairs equal** `mega_names.answer(…, postings='m')` bucket by bucket (bytes and objects; buckets with zero on both sides omitted) — 37 literals (`5418`, `nk080`, `48.parquet`, `gof`, `11979`, `pio`, `54181`, `48.parquet.crc`, `0.0.73`, `116.tok`, `bb-`, `rt-0003`, `s__marin-us-centr`, `xican`, `_hypocris`, and 22 random 4–8-character substrings of hash-sampled names, `job/static-names/terms.txt`) on 2026-10-08, 10-06, 10-01, 09-30, 09-15, 08-15 and 07-30. Every literal's rows were one ranged read of one file: median 0.39 MB, max 3.07 MB (`nk080`); e.g. `5418` 15 row groups / 1.44 MB / 122,880 rows read for 109,281 matching, `s__marin-us-centr` 28 / 1.13 MB / 229,376 for 228,729, `48.parquet.crc` 1 / 65 KB.

**R2**: `r2.dvc` copies `sx/`, `sidecar.parquet`, `shards.json`, `scans.json` (325 objects, 150.6 GB) to `oa-gcs-usage-index` under `static-names/2026-10-08/`.

Gotcha: DVX `git_deps` make a stage stale when the file changes. The Batch driver (`job/static-names.sh`) was a git dep of the intervals/suffix stages, and a one-line fix to it made a later `dvx run r2.dvc` start rebuilding from intervals (it was caught after an all-skip intervals job). Only `static_names.py` is a content dep now; the image pin rides in the driver's committed text.

### Extrapolation written before the full build

- Intervals: 256 ranges × ~250 s ≈ 18 task-hours of n2-highmem-16 → 32 tasks ≈ 35–45 min wall; ~$20 on demand, ~$6 spot. Output ~10–15 GB (8 B/version measured).
- Suffix rows: the three dev ranges average 38 suffix rows per version (long podcast names); `name_spans` gives 16.5B in all. Files at 12–15 B/row → **~200–250 GB** (the prototype's ClickHouse export was 25–28 B/row; the estimate above of 410–460 GB is superseded).
- Suffix map: one expansion of all 1.15B versions; reduce: ~330 shards × ~1 min sort/write plus each task's partition pass, 32 tasks ≈ 30–45 min wall; ~$15–30 on demand.
- R2: ~200–250 GB at GCS egress ≈ $25–30 once; ~$3–4/month stored.

## Alternatives compared

| Design | Size | Round trips per rare query | Bytes per rare query | Verdict |
|---|---|---|---|---|
| **Suffix-ordered denormalized postings** (above) | ~430 GB | 2 (D1 sidecar, then one range per file) | ≤ `V` × 28 B (~3 MB) | **Recommended** |
| Suffix → name id, plus name-sorted postings | ~25 GB + 16 GB | 2 + one read per matching name (34K for `5418`) | small, but scattered | Today's cold problem again |
| Trigram posting lists (plain, or SQLite FTS5 `trigram` via HTTP-range VFS or D1) | ~5.9B positions; FTS5 in D1 needs 10+ shards at 10 GB each | 2–3, then postings scatter | Smallest trigram list is 0.5–1.8M names for `5418`, `48.parquet`, `rt-0003` (MBs of doclists), plus candidate verification | Not bounded by the result; still needs postings |
| FM-index over the names | ~3–6 GB | ≈ len(q) dependent rank lookups, then one per occurrence to locate | small per step | Fine in memory on a server; sequential round trips on R2 |
| Minimizer- or k-sampled suffixes (store 1 in k positions) | ~1/k | k ranges | each range is a shorter, possibly common, suffix: unbounded | Breaks the bound |

## What stays on a server

Nothing on the query path: catalog lookups are static (`mega-index.md`), rare terms read this layout. The daily build (ingest diff, catalog census over 139.6M names, delta files, weekly compaction) is batch work, ~15–30 minutes a day, which fits a GCP Batch job like the daily scan's; the census needs ~10–20 GB RAM briefly.

## Plan to production

1. **Weight and bound.** Switch the cost-weighted census's weight to suffix-range rows (versions × occurrences) and pick `V` from a byte budget (e.g. ≤ 4 MB per query → `V` ≈ 140K). Rebuild the catalog with it.
2. **Full base build.** Full suffixes, 4–8K-row groups, files split by suffix prefix, on GCS then R2; sidecar rows into D1. Verify against `mega_names.answer` on a broad term/date sample.
3. **Worker reader.** `site/functions`: catalog first, else D1 sidecar → R2 range → hyparquet decode → filter/sum. Behind the existing `/api/name-summary` contract; measure Worker latency cold and warm on R2.
4. **Daily deltas + compaction** as a Batch stage after the daily scan; drop the ClickHouse query service.
5. Retire the always-on VM (keep ClickHouse only for ad-hoc experiments, if at all).

## Files

- `job/ch-store/static-name-proto.sql`: the prototype build (prefixes `541`, `nk0`, `48.`, `gof`).
- `job/ch-store/static-name-query.py`: the one-range reader and per-date answers.
- `job/ch-store/static-name-term-stats.sh`, `static-name-terms.txt`: the per-term table.
- `cloud/src/dt_cloud/static_names.py` (`dt-cloud static-names …`), `cloud/tests/test_static_names.py`, `job/static-names.sh`, `job/static-names/{ch-answers.py,terms.txt}`, `static-names/<gen>/*.dvc` (the DVX stages).
- Built data: `gs://oa-gcs-usage-dvx/static-names/2026-10-08/` and R2 `oa-gcs-usage-index` `static-names/2026-10-08/`.
- Prototype data: `default.sx_names`, `default.sx_rows`, `default.sx_proto` on the VM; `gs://oa-gcs-usage-dvx/scratch/bench/ch-store/static/sx-{4096,16384}.parquet` (1.5 GiB).

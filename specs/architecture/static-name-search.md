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
| **Estimated total** | **~410–460 GB** |
| R2 storage at $0.015/GB-month | ~$6–7/month |

Almost all of each row is the `path` string. Storing a parent-path id with a dictionary would shrink it, but needs a second lookup for the first-hit test; not worth it at these prices.

### Bounding every query

The read for `q` is `Σ over names containing q of versions × occurrences` rows, all time. The cost-weighted census (`ch-mega-catalog-build -w rows`, `mega-index.md`) registers exactly the literals at or above a weight `V`; with the weight defined as these suffix-range rows, **every literal outside the catalog reads fewer than `V` rows**, about `V × 28` bytes: 2.8 MB at `V` = 100K, one range read. Two details:

- **Long queries.** The prototype truncates `s` to 24 characters; a non-member `q` longer than 24 could have a common 24-character prefix and an unbounded range. Store the full suffix (or ≥ 128 characters; names p99 is 125), which sorted zstd compresses well, so the range is always exactly `q`'s.
- **Multiplicity.** The weight counts a name containing `q` twice as two rows, matching what the range holds.

### Reading it from a Worker

- **Sidecar.** At 4K rows per group, 16.5B rows is ~4M row groups; their parquet footer metadata is ~1.3 KB each (measured), so files are split by suffix prefix (e.g. first two characters, a few hundred files of ~1–2 GB) and the per-group `(s_min, s_max, file, byte range)` index goes to D1 (or KV shards). That is the same footers-in-D1 pattern the path store already uses (`index-sync`), so a query is: one D1 lookup (the group range for `q`) → one R2 ranged GET (≤ a few MB) → decode, filter, sum.
- **Decode.** ~100K rows / 3 MB compressed decodes to ~25 MB with hyparquet, within a Worker's 128 MB; the CPU is the filter over ~100K rows (the prototype's 0.6 s is pure Python). Lambda or Cloud Run are not needed.
- Not measured: Worker → R2 latency (needs an R2 bucket, not created). Laptop → GCS already shows 0.1–0.4 s for the range read.

### Daily upkeep

LSM-style, no server:

- **Delta files.** Each scan's opened versions (~2M/day) become suffix rows (×14.3 ≈ 30M rows, ~0.8 GB) in a small sorted delta file with its own sidecar; its closures (~1M/day, ≈15M suffix rows) are `(s, path, usr, vf, vt)` close records in the same file. A query reads its range in the base and in each delta (independent reads, issued in parallel, not dependent), and applies close records over base rows.
- **Compaction.** Weekly, merge deltas into the base for the affected prefixes (a sorted merge; DuckDB on GCP Batch), keeping at most ~7 deltas.
- **Inputs.** The day's opened and closed versions are what `ch-ingest` already computes; the same diff can come from consecutive path-index files with DuckDB on Batch, so ClickHouse is not required for upkeep either.
- **Initial build.** The prototype's 31M rows took ~105 s; the full 16.5B is ~530× that, roughly 3–6 hours of sort and export on the 32-vCPU VM or a Batch job, once.

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
- Prototype data: `default.sx_names`, `default.sx_rows`, `default.sx_proto` on the VM; `gs://oa-gcs-usage-dvx/scratch/bench/ch-store/static/sx-{4096,16384}.parquet` (1.5 GiB).

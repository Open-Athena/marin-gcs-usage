# One consolidated store and name index ("mega-index")

Evidence checkpoint: 2026-10-08, ch-store VM resized to n2-highmem-32 (32 vCPU, 251 GB, 2 TB pd-ssd). The goal: every scan, base tree and text search, in one interval-coded store, so a day costs O(changes) and any historical scan stays queryable without its own index.

## What exists

ClickHouse `default` is the append-only history (`ingest.py`, [clickhouse.md] §1): `nodes` versions of `(depth, path, usr)` opened at `vf`, `closures` closing them at `vt`, `names` (every lowercase basename ever seen, trigram index), `scans`. It holds 68 scans, Jul 30 – Oct 6. Scans before 09-30 are an older source format; 09-30 is a complete rebaseline (`scan_epochs`), so v2 queries read from there.

A path is one row per owner slice: `usr = ''` is the **unowned** slice, not a total. Path totals are the sum over slices (Oct 5 `marin-us-central1`: 608.8 T unowned + 166.9 T owned = 775.7 T).

## Daily append

| Scan | Machine | Threads / pairs | Wall | Opened | Closed |
| --- | --- | --- | ---: | ---: | ---: |
| 10-01 … 10-04 | n2-standard-8 | 8 / 4 | 548–595 s | 2.7–3.1M | 1.3–1.5M |
| 10-06 | n2-highmem-32 (cold cache after resize) | 32 / 16 | 807 s | 2.64M | 125.9M |

More cores did not help: 10-06 is the deletion day (125.9M closures, the Oct 5→6 drop of ~123M paths) and ran on a cold page cache, so this is not a like-for-like speedup measurement. The append is range-parallel FULL JOINs; the next lever is fewer, larger pairs on a warm cache, not more threads.

**Verification:** Oct 6 reconstructed as-of from `default` equals the standalone `daily_scalar_oct06_fleet_a` exactly: 606,128,523 paths, 25,862,761,745,080,647 bytes summed over paths, 3,396,993,072 objects summed, and the order-insensitive digest `sum(cityHash64(path, b, o))` 12515680132438215233 on both. (The full as-of reconstruction is a 705 s scan; it is a verification, not a serving path.)

## Name search over every scan

`chstore/mega_names.py`, `ch-mega-names`, `ch-mega-names-build`. A literal on scan D:

1. vocabulary: names containing it whose live span covers D (`name_spans`);
2. postings: rows with those names live on D (opened by D, not closed by D);
3. first hits: name matches and the parent path does not (a slash-free literal sits in one segment, so this is exactly "no ancestor matched"; the same rule as `hot_l1.oracle`);
4. bucket totals over first hits, summed across owner slices.

Exactness: a brute-force first-hit oracle over every 1–3-character substring of the fixture's names, on every ingested day, with the store's projections, all-time postings and span postings, each with and without `name_spans` (`cloud/tests/test_chmega_names.py`). On real data, 30/30 `(date, literal)` answers equal the per-scan index (Oct 6) and the frozen union (Oct 4/5).

Two findings shaped the index:

- **The vocabulary must be filtered by live span.** `names` is all-time: Oct 6 `3p` drew 10,429,581 names (33.6 s) where Oct 6 itself has 26,696. `name_spans(l, first, last)` (earliest version → last closure, or never while any is open) is a superset filter built from two per-name aggregations, no join: 136,869,079 names, 90,131,651 open (exactly Oct 6's distinct names), 147 s.
- **The store's own `by_name` projections are too coarse.** `nodes` is partitioned per scan (343 active parts) in 8,192-row granules, so each name costs a granule per part: Oct 6 `5418` (33K names, 108K versions) read 228M rows / 32 GB in 9–12 s. Name-sorted postings in 256-row granules, unpartitioned, fix the layout.

### Postings size per span

| Span | `_nodes` rows | `_nodes` bytes | `_closures` rows | `_closures` bytes | Insert | With `OPTIMIZE FINAL` |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| all time (68 scans) | 1,149,549,991 | 12.36 GB | 543,418,112 | 4.59 GB | 128 s | 858 s |
| 10-01 … 10-06 (6 scans) | 794,642,579 | 7.61 GB | 188,510,700 | 1.12 GB | 467 s | 1,240 s |
| 10-04 … 10-06 (3 scans) | 790,449,380 | 7.39 GB | 184,317,501 | 1.02 GB | 466 s | 762 s |

For comparison, Oct 6's per-scan name index alone is 9.5 GB (`daily_name_index`, 4m43s). A span costs one rebaselined snapshot plus each day's churn: three scans and six scans differ by 0.5%; all 68 scans (with the pre-09-30 format's dirs-only rows and the rebaseline) cost 1.9× one span. The span builds pay a sorted-merge anti-join against earlier closures (466 s); the all-time build copies without one (128 s). Daily upkeep would append the day's opened versions and closures, which the ingest already isolates.

### Latency (8 threads unless noted; warm second run; n2-highmem-32)

Every `(date, literal)` with a reference equals it: 120/120 across runs (Oct 6 vs its per-scan index, Oct 4/5 vs the frozen union).

| Literal | Names / rows on Oct 6 | Per-scan index (Oct 6) | Store projections | Postings 10/4–6 | Postings all time | All time, 32 threads |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `3p` | 26,696 / 38,363 | 1.56 s | 35.7 s | 1.23 s | 1.18 s | 0.73 s |
| `6h` | 20,321 / 26,830 | 1.48 s | 0.97 s | 1.20 s | 1.14 s | 0.70 s |
| `5418` | 19,774 / 28,000 | 1.32 s | 10.5 s | 3.49 s | 2.90 s | 2.07 s |
| `nce_in` | 27,253 / 27,348 | 1.30 s | 1.89 s | 0.47 s | 0.45 s | 0.41 s |
| 36-char UUID literal | 2,000 / 10,638 | 0.29 s | 0.33 s | 0.08 s | 0.06 s | — |
| `58.index.npy` | 1,045 / 90,426 | 0.29 s | 0.47 s | 0.19 s | 0.14 s | — |
| `tensorstore` | 30 / 38 | 0.15 s | 0.36 s | 0.07 s | 0.07 s | — |
| `zzqx` (no match) | 0 / 0 | 0.08 s | 0.04 s | 0.05 s | 0.05 s | — |

The per-scan column is the reference build (`hot_l1.build(daily=True)`, uncapped, no serving overhead), not the served lane. The 10/4–6 and earlier all-time runs used a tuple `NOT IN` for the closure filter; the all-time column is the shipped hash-join form. Older scans answer the same way: Sep 15 (pre-rebaseline format) 0.07–1.95 s.

What remains slow:

- **1–2 character literals** spend ~0.8 s (0.38 s at 32 threads) scanning `name_spans` without the trigram index. Precomputing every 1–2 character literal into the catalog (workstream A) removes this.
- **Literals whose names carry many versions** (`5418`: 19.8K names, 108K versions over all time) read a 256-row granule per name: 2–3 s. Fewer parts (the 10/4–6 table kept 19 after `OPTIMIZE`) and smaller granules are the next levers.
- **Hot-on-the-day literals** are slow by construction (`3p` on Oct 4: 10.4M live names, 19.6M rows, ~10 s); they are catalog literals there and never reach this path.

## Precomputed catalogs day to day

Bucket answers (`(literal, bucket)` cells of the dated L1 catalogs) change a lot between consecutive scans, because a bucket total moves whenever anything under a busy literal changes:

| From → to | Literals in both | Cells unchanged | Nonzero cells unchanged | Literals entirely unchanged |
| --- | ---: | ---: | ---: | ---: |
| 10-04 → 10-05 | 71,182 | 74.4% | 53.5% | 46.8% |
| 10-05 → 10-06 | 48,657 | 77.3% | 63.1% | 67.2% |

Interval-coding cells would save about 4× on a 22 MB/day artifact: not worth it at bucket level. Deeper drill tiles (smaller scopes) should be far more stable and are the better candidate.

## Recommendation

Ship the consolidated store as the one source for below-threshold name search, dropping per-scan name indexes and per-scan scalar databases:

1. Daily: append the scan (`ch-ingest`, ~9–13 min), then append its opened versions/closures to the name postings and refresh `name_spans` for the names they carry (both O(changes); to implement).
2. Serve below-T literals for any scan with `mega_names.answer(..., postings=…)` behind the existing bounded lane (name and row budgets, deadline).
3. Precompute 1–2 character literals with the catalog, so on-demand reads always use the trigram index.
4. Keep catalogs per scan for now; revisit interval-coding at drill-tile granularity.

Storage: one all-time name index (17 GB) replaces 9.5 GB per scan, and grows by a day's churn.

[clickhouse.md]: clickhouse.md

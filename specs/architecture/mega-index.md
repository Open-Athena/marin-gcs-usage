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

## Daily upkeep

`ch-mega-names-append DATE STEM…` (`mega_names.append`) adds one ingested scan to the name index: the `nodes` partition `vf = D` (versions it opened) and the `closures` partition `vt = D` (versions it closed) go into each postings stem as a staged side table attached whole, and their per-name deltas into `name_spans`. `name_spans` is an `AggregatingMergeTree` of `(l, min first, sum versions, sum closed, max closed_last)`; a name is live on D when `min(first) <= D` and either `sum(versions) > sum(closed)` or `max(closed_last) > D`, so readers `GROUP BY l` and appended rows need no rewrite. `name_index_log` records what each target covers; an append at or before it is refused, so a scan is never added twice.

Verification on the VM (2026-10-08): `name_spans` and postings `m` built through Oct 5 (91 s and 117 s unmerged), then Oct 6 appended:

| Target | Rows appended | Wall |
| --- | ---: | ---: |
| `name_spans` | 44,208,945 name deltas | 13.6 s |
| `m_nodes` / `m_closures` | 2,635,102 / 125,927,390 | 9.7 s |

`m` then equals the all-time build `all` exactly (`ch-mega-names-digest`: 1,149,549,991 / 543,418,112 rows, identical order-insensitive digests), and `name_spans` equals a full rebuild per name (136,869,079 names). Oct 6 is the heaviest day there is (126M closures); an ordinary day is ~3M opened and ~1.5M closed. A final merge after the append (`OPTIMIZE … FINAL`) took 152 s + 374 s; it is optional (readers are correct over any number of parts).

## Serving

`serve-query -P STEM` (`MEGA_POSTINGS=STEM job/ch-store.sh serve`) binds the dated name-summary runtime to the consolidated index (`mega_names.binding`: the stem's and `name_spans`' coverage, and each covered scan's bucket geometry from depth-1 `n_desc`). In the existing bounded cold lane (one slot, 5 s, 16 threads, 200K-name budget):

- a daily scan's unregistered literal answers from the consolidated index instead of the scan's own name index (its geometry must equal the catalog's at boot);
- every scan without a frozen or daily catalog becomes available as `consolidated-store-v1`: every literal on demand, no catalog. Scans that have catalogs keep them.

### Served latency (Oct 6, the workstream-A T-curve picks, 8 per band, through `/api/name-summary`)

Same box and lane (16 threads, 5 s); "per-day" is the scan's own name index (`ch-daily-name-index`), "consolidated" is `m` (all 68 scans, merged). Cold drops the OS page cache before each request. Wall seconds, p50 / max:

| Paths per literal | per-day cold | consolidated cold | per-day warm | consolidated warm |
| --- | ---: | ---: | ---: | ---: |
| 8.5K–10K | 1.99 / 2.21 | 0.80 / 0.90 | 0.64 / 1.78 | 0.48 / 0.72 |
| 25K–30K | 1.50 / 2.86 | 0.61 / 2.92 | 0.49 / 1.04 | 0.31 / 2.69 |
| 85K–100K | 2.25 / 3.49 | 0.60 / 3.83 | 0.65 / 1.39 | 0.37 / 3.39 |

All 48 answered. The consolidated index removes the cold floor (the per-scan `nodes_by_name` page misses): median cold latency drops 2.5–3.7×. Its tail is the same shape as before, driven by distinct matching names (`48.parquet` 3.4 s, `nk080` 2.9 s, `bb-` 2.7 s warm: the postings join over every version those names ever had).

Scans with no catalog (every literal on demand):

| Literal | Sep 15 cold / warm | Oct 1 cold / warm |
| --- | ---: | ---: |
| `3p` | 3.58 / 3.59 | refused: > 200K names |
| `6h` | 1.52 / 0.81 | 1.33 / 1.05 |
| `5418` | 4.78 / 4.20 | deadline (5 s) |
| `gof`, `nk080`, `116.tok` | 0.30–0.83 / 0.09–0.44 | not run: lane quarantined after the deadline |

A request that hits the 5 s deadline is cancelled and its HTTP transport can't be proven idle, so the lane quarantines and recovers ~7 s later; requests in that window get 503 (busy), never a partial answer. A finished query that lingered in `system.processes` used to quarantine the lane the same way after a *successful* answer (`bb-`, every time); verification now polls for up to 1 s.

## Precomputed catalogs day to day

Bucket answers (`(literal, bucket)` cells of the dated L1 catalogs) change a lot between consecutive scans, because a bucket total moves whenever anything under a busy literal changes:

| From → to | Literals in both | Cells unchanged | Nonzero cells unchanged | Literals entirely unchanged |
| --- | ---: | ---: | ---: | ---: |
| 10-04 → 10-05 | 71,182 | 74.4% | 53.5% | 46.8% |
| 10-05 → 10-06 | 48,657 | 77.3% | 63.1% | 67.2% |

Interval-coding cells would save about 4× on a 22 MB/day artifact: not worth it at bucket level. Deeper drill tiles (smaller scopes) should be far more stable and are the better candidate.

## Recommendation

Ship the consolidated store as the one source for below-threshold name search, dropping per-scan name indexes and per-scan scalar databases:

1. Daily: append the scan (`ch-ingest`, ~9–13 min), then `ch-mega-names-append` (O(changes): 23 s for the 126M-closure Oct 6).
2. Serve below-T literals for any scan from the consolidated index (`serve-query -P`) behind the existing bounded lane (name budget, deadline).
3. Precompute 1–2 character literals with the catalog, so on-demand reads always use the trigram index.
4. Keep catalogs per scan for now; revisit interval-coding at drill-tile granularity.

Storage: one all-time name index (17 GB) replaces 9.5 GB per scan, and grows by a day's churn.

[clickhouse.md]: clickhouse.md

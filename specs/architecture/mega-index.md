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

For comparison, Oct 6's per-scan name index alone is 9.5 GB (`daily_name_index`, 4m43s).

[clickhouse.md]: clickhouse.md

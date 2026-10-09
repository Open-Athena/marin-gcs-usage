# `bysize` keyed on the path's total

**Status:** proposed. Interim fix shipped on `cloud`: `completeSlices` (`site/functions/_lib/index.ts`).

## Problem

On a labeled store generation (gcs: one row per `(path, usr)`), the `bysize` sort is ordered by each row's own bucket, `⌊log2 size⌋`. A thresholded read keeps a row when *its* size clears the depth's threshold. That has two effects (specs/interval-store.md §7):
- a drawn directory loses its owner slices under the threshold;
- a directory whose every slice is under the threshold, but whose total clears it, isn't drawn.

On prod 2026-10-09 the root view at 1280×768 (threshold 39.46 GB) had 92 tiles short by 1.80 TB, and 6 tiles missing (1.0 TB). The six bucket tiles were short by 197 GB, `marin-eu-west4` alone by 98 GB. The worst tile was `marin-us-central1/data/datakit/normalized/nemotron_cc_v2_1`, which showed 2,232 of its 2,653 GB.

## The interim fix (shipped): complete the multi-owner slices from `path`

A child's owners are a subset of its parent's, so only a multi-owner path can be short or missing, and only under a multi-owner parent. The `path` sort holds a parent's children band (one depth, one path range) contiguously. `completeSlices` works down from the view root, or from the store root, which always qualifies:
- It reads each multi-owner parent's whole children band.
- It replaces those children's rows with every slice of the children whose total clears the threshold.
- It descends into the multi-owner children that can still hold a child over the threshold.

Paths that the size read already shows with two slices are seeded into the first round, so there are fewer rounds than levels.

The result is exact: on prod 10-09 the root and all six bucket views matched an independent per-path reference (pyarrow over the `path` sort's row groups) tile for tile, in bytes and in per-owner bytes.

**Cost** (read-only, from a laptop against prod D1 and GCS, cold):

| View | Groups | Bytes | Bands | Rounds | Wall |
|---|---:|---:|---:|---:|---:|
| root | 6 → 83 | 1.4 → 13.3 MB | 95 | 3 | 1.5 → 5.6–6.0 s |
| `marin-eu-west4` | 11 → 47 | 3.7 → 8.4 MB | 34 | 5 | 1.0 → 3.5–3.8 s |
| `marin-us-central1` | 12 → 50 | 8.0 → 13.8 MB | 67 | 3 | 0.9–1.3 → 3.2–3.5 s |
| `marin-us-central2` | 12 → 45 | 3.5 → 9.0 MB | 30 | 3 | 0.6–0.7 → 2.8–2.9 s |
| `marin-us-east1` | 18 → 37 | 1.5 → 5.5 MB | 12 | 4 | 0.8 → 2.6–2.8 s |
| `marin-us-east5` | 13 → 55 | 0.5 → 3.7 MB | 42 | 5 | 0.7 → 2.9–4.1 s |
| `marin-us-west4` | 11 → 17 | 0.7 → 3.1 MB | 4 | 2 | 0.7–0.8 → 1.7 s |

The rounds run in turn, and each one is a span query plus a fetch. Most of the bytes are the bands of wide multi-owner directories (`marin-us-central2/experiments` alone spans 11 groups). Re-reading only the drawn paths' slices would cost more: at the root that is 304 groups and 2.5M rows, past `decodeSpans`' caps, and it would still miss the 6 tiles.

## Proposal: bucket each row on its path's total

Cut `bysize` with the path's total beside every slice, and sort and bucket on that total:
- `tot = SUM(size) OVER (PARTITION BY depth, path)`, stored as an INT64 column. It is equal on every slice of a path, so it run-length encodes well.
- The sort key is `(⌊log2 tot⌋ desc, path, …labels)`. All slices of a path are then adjacent, in one bucket run.
- Group stats: `b_max` becomes `MAX(tot)`, the planner's floor test. Both `disk-tree tiers plan`'s `select_bysize` and `index_footer.extract` follow it.

The reader keeps a row when `tot ≥ thrAt(depth)` and returns every slice of every qualifying path, in one pass at today's cost. It detects the column in the schema (`tot` present) and skips `completeSlices` for those generations.

**`bysize-by-user` stays per slice.** A lens reads one owner's rows, so its threshold is on the slice and is already exact.

**Unlabeled stores (cw, the r2 demo)** have `tot = size`. They can skip the column: the reader already skips completion where there is no `usr`.

## Work

1. `find/tiers.py`: the `tot` window column and the `tot` bucket for `bysize` when label columns exist (`BUCKET_SQL` over `tot`). Write `bucket = log2(tot)` in the kv metadata.
2. `index_footer.extract` / `.groups.parquet`: `b_max` from `tot` for `bysize`, plus `tiers plan` parity (`gen.py` `PLANS` over the `v2-slices` fixture).
3. Reader: `rowColumns` projects `tot`, and `readSizeRects` tests `tot`. `completeSlices` stays for generations without it.
4. Re-cut every labeled v2 generation's `bysize` (gcs 09-30 onward: ~7 GB per scan), then `index-sync` its D1 footer rows. Batch, not the laptop. Run `refview`-style parity on a sample before switching each pointer.
5. Once every served generation has `tot`, drop `completeSlices`, or keep it as the fallback for old generations.

## Open

- Whether the interval store (specs/interval-store.md, one row per path) replaces the per-scan sorts before this re-cut pays off.

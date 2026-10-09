# Anchored name search: `^q`, `q$`, `^q$`

Status: 2026-10-09, in progress on branch `anchors`. Built from option 2 of `specs/search-extensions.md` (2b ends-with heavy, 2c the name index), with Ryan's syntax `^q` / `q$`. The heavy-`^q` drill (2d) is not built: its census decides.

## Semantics

- `^q`: some name (path segment) starts with `q`. `q$`: some name ends with `q`. `^q$`: some name is `q`. Case-insensitive, like every term.
- A path matches when **some segment** matches. That is monotone down the tree, so match roots work as for a contains literal.
- A **match root** under view path P is a path under P whose name matches and **none of whose ancestor segments match**. The test is per segment: `b1/big-tomato/tomato.txt` is a root of `^tomat` (`big-tomato` doesn't start with `tomat`), though a contains-style test on the parent path would drop it.
- P itself matching (some segment of P) is the plain view, as for a contains literal.
- In the predicate (`pathQuery.ts`, the path-store fallback and the client), an anchor is a segment boundary: `^` = the path's start or right after a `/`, `$` = its end or right before a `/`. This also defines anchored globs (`^ckpt*final`) and terms with `/` (`^tmp/ttl`), which only the path-store fallback serves.

## Syntax (`simple`)

- An unquoted `^` opening a term and an unquoted `$` closing it are anchors: `{ kind: 'sub', text, start?: true, end?: true }` (globs alike).
- Quoted, they are literal (`"^a"`, `abc"$"`); anchors around nothing (`^`, `$`, `^$`) are the characters themselves.
- Negatives take anchors (`ckpt -^tmp`).
- In the `regex` syntax, `^` anchors the bucket, not a name; its help says so.
- **Indexed-only** (`FILTER_INDEXED_ONLY=1`) accepts one anchored literal without `/` or `*`: `^q` from 2 characters, `q$` from 3, `^q$` from 1. Shorter ones are `anchor-too-short` ("add the dot: `.gz$`"). Everything else is refused as before.

## Index

Per tier (the base generation, and each run under `deltas/<id>/`), new keys only:

| Key | What |
|---|---|
| `names/` (`shards.json`, `sx/s####.parquet`, `sidecar/`, `sidecar.parquet`) | The **name index**: one row per version (depth ≥ 1), `s` = `/` + the lowercase name, in the suffix shards' layout (`SX_SCHEMA`, sorted `(s, path, usr, vf)`, 8K-row groups, shards cut at three-character prefixes). `/` occurs in no segment, so `^q` is the range `/q…` and `^q$` the key `/q`. A run's rows are its `cdelta`'s versions (opens, and close records with their final `vt`), combined across tiers like suffix rows (equal `(s, path, usr, vf)`: the smallest `vt`). |
| `anchors/{end,exact}-rollups-index[.top].parquet`, `anchors/rollups/{end,exact}-s####.parquet` | Rollups of the heavy `(k, dir)`, in the drill's layout (`ROLLUP_SCHEMA`, two-level index). `end`: `k` an exact suffix (from `sx/`); `exact`: `k` = `/` + a name (from `names/`). `dir` is a directory or `''` (the fleet root; children = buckets). |
| `anchors/meta.json` | `R`, `K`, `rg` (the shards' row-group size), `idx_rg`, per rollup set its `row_groups`; a run's `scans`. Written last: the tier's liveness marker. |
| `anchors/keys-{end,exact}.parquet`, `anchors/keys/`, `anchors/census-start.*` | GCS only: per row group of the base's shards, its `s` and `path` bounds (the run builder's bounds), and the `^q` census. |

**No roots files.** `q$`'s rows are the suffix rows with `s == q` (a suffix of the name equal to `q` is exactly "the name ends with `q`"; at most one per version), sorted by `path`. So the rows under P are the key range `[(q, P/), (q, P0))` of the shards, bounded by whole row groups through the `s` and `path` column statistics, which the writer already stores untruncated. `^q$` is the same over the name index. The Worker cuts the `path` bounds from each shard's footer once (`StaticNames.keys`, cached apart from the group index as `keys-v1`); no key files are served.

**Heavy.** `(k, dir)` is heavy when more than R rows of `k` lie at or under `dir`, over every tier (rows, close records included, not roots: the dispatch bounds rows). Heavy directories are closed upwards. A heavy `(k, dir)`'s rollup: per child of `dir`, the running Σ size, n_files of the first hits under it, one cell per change; K = 256 kept children by peak bytes plus an exact remainder. Header `b` = rows under `dir`, `o` = children holding first hits.

## Dispatch (`staticAnchors.ts` `AnchoredSource`; Python `static_anchors.AnchoredReader`)

Per (key, P):

1. **Light.** The key's whole range (all tiers) ≤ `MAX_ROWS` (V + 2 groups) → read once, folded to first hits, held per isolate and per colo per stack version, cut to P. `q$` reads every tier's suffix shards (so it covers every scan), `^q` / `^q$` the anchored tiers' name index.
2. **Scoped** (`q$`, `^q$`). Over the anchored tiers, the groups meeting `[(k, P/), (k, P0))`: their rows ≤ `R + 4·Σ rg` → read them, keeping the rows under P. Each tier's bound is at most its true rows plus four groups: two straddling P's range, and the two groups at the ends of `k`'s run that hold other keys.
3. **Rollup.** Otherwise the true rows exceed R, so the builder made `(k, P)`'s rollup in some tier. The tiers' rollup rows stack newest first down to a full header (`DRILL_RULES.stack`).

`^q` past step 1 declines with `term-too-common`.

**Tiers.** The base and the manifest's runs, up to the first run without `anchors/meta.json`. Answers carry the scans of the tiers they read (`Found.scans`), so a scan past the anchored stack is `scan-not-indexed` (a light `q$` excepted). A generation without `anchors/meta.json` declines every anchored key past light `q$`.

**Keys.** Anchored keys travel as `termKey`s: `/q` (`^q`), `q/` (`q$`), `/q/` (`^q$`). A static literal never holds `/`, so a key is unambiguous. `SuffixHits` hands them to the anchored source; the drill never sees them. `FILTER_STATIC_ANCHORS=0` turns the source off. Static responses are versioned 7: before this, `^q` / `q$` were plain literals.

## Building

**Base** (`dt-cloud static-names anchors …`, Batch through the deployment's `job/static-names.sh` with `MODULE=static_anchors`):

1. `names`: the base's `cintervals` → `names/` (one task: DuckDB sorts, `write_run_shards` cuts).
2. `census`: name-index prefixes with ≥ 50K rows → `anchors/census-start.{parquet,json}` (the heavy-`^q` count).
3. `rollups -k end` (the suffix shards) and `-k exact` (the name index), from a shared queue, biggest shard first. Per shard: the keys with more than R rows, `heavy_dirs` bottom-up, the first hits, `rollup_cells`, and the shard's key bounds.
4. `index`: both rollup sets' two-level indexes and key tables, then `meta.json`.

**Runs** (`anchors run -d ID`, level 0). The run's `names/` comes from its `cdelta`. Its rollups:

- Candidate keys are those whose rows over every tier can pass R: the run's count plus each prior tier's sidecar bound (cumulative group rows).
- For each `(k, dir)` that was heavy before (some prior tier's rollup) and that the run's rows touch: a delta header (`kind` −1: kept, `b` + the run's rows, `o`) and the cells dated at the scan. Each series' last value moves by the run's first-hit events (an open adds, a close record subtracts). A child no tier has kept is kept while fewer than K are (then every child holding roots is kept, so it is new); otherwise it goes to the remainder.
- For each `(k, dir)` the run makes heavy: candidates are bounded by the prior tiers' key bounds; where `bound + run rows > R` the prior rows are read exactly (≤ R, by stickiness). Past R, a full restatement (`kind` 0) is built from every tier's first hits under `dir`.
- `o` is exact while fewer than K children are kept; after that, a delta adds only the new kept children (a lower bound). It is shown as the "N others" count only.
- Merged runs (the counter): `names/` via `merge_shards`; rollups stacked per `(k, dir)` (`merge_rollups`).

## Tests

- **Python** (`cloud/tests/test_static_anchors.py`). Base + two runs + the runs merged, R = 3, K = 2, small groups. Every key (20: `q$`, `^q`, `^q$`, including buckets, absent keys and short `^q`) at every path on every scan, through the reader at three whole-range bounds (light, scoped, rollups), equals brute force from the versions. The name index is one row per coalesced version. The scan-file brute-force SQL equals the version brute force. Mutations caught: a contains-style parent test, a stack ignoring the runs, a largest-`vt` combine.
- **TypeScript** (`staticAnchors.test.ts` on `fixtures/static-anchors/`, built by the Python builder). The same sweep through `AnchoredSource` for the base, the first run and both runs (16 tests), plus a run without `anchors/meta.json`, a generation without anchors, a mutated parent test, keys, `SuffixHits` routing; syntax, predicate and indexed-only cases.

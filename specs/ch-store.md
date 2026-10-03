# ClickHouse store: one interval table behind the Worker

**Status:** built on branch `ch-store` (code `[cloud]`: `dt_cloud.chstore`, `serve-query -e ch`, `ch-ingest`, `ch-bench`, `site/functions/_lib/queryBox.ts`; gcs's scripts: `job/ch-store.sh`, `job/ch-store/`, the optional stage in `job/run.sh`). Measured on gcs 2026-10-03 (§6). Builds on [serving-options] §4.6 (SCD-2) and §5–6 (suspend/resume, ClickHouse measured), and on [filter-query-service] §6.3 (`serve-query`), §7 (code layout) and §8 (serving tiers).

## 1. Goal

One ClickHouse database holds every scan of a deployment as **change records with intervals**: a row per version of a `(path, owner slice)`, valid from the scan that first saw it until the scan that first didn't. It subsumes the per-scan path store for serving: any scan's subtree, filter, diff and series answers come from it, and history costs about one scan plus the churn.

The Worker stays in front (auth, edge cache, fallback). It forwards the box-served routes to `dt-cloud serve-query -e ch`, which answers in the Worker's exact response shapes, and falls back to its own reads when the box errors, times out or declines.

One small VM, suspendable. RAM page cache → the VM's persistent disk (the big middle tier: every scan, every version) → GCS of record (the per-scan path stores, from which the store can always be rebuilt).

## 2. Schema (`dt_cloud.chstore.schema`)

### 2.1 Tables

- **`nodes`**: one row per version of a `(depth, path, usr)` owner slice: the path store's row (`kind`, `size`, `n_files`, `n_children`, `n_desc`, `mtime`, `mtime_mean`, `mtime_w`, `last_read`, `c2`–`c4`), the lowercase last segment `name`, and the interval `[vf, vt)` as `DateTime`s (a scan id `2026-10-01` or `2026-10-01T0003` maps to its minute).
  - `ORDER BY (depth, path, usr, vf)`: a subtree at one depth is one primary-key range (`path ∈ [P/, P0)`, `'0'` sorting just past `'/'`), which is how the Worker's path store is laid out too.
  - `PARTITION BY toYYYYMM(vt)`: the **open** versions (still present at the newest scan) carry `vt = 2106-01-01` and so sit alone in partition `210601`; **closed** versions sit in the month of their `vt`.
  - `PROJECTION by_name (SELECT * ORDER BY name, depth, path, usr, vf)`: the filter's name → nodes access path. ClickHouse picks it whenever a query's `name IN (…)` prunes more marks than the base order (checked with `EXPLAIN indexes = 1`).
- **`changes`**: every version once at its opening (`sign = 1`, `at = vf`) and once at its closing (`sign = −1`, `at = vt`), with the fields a delta needs (`kind`, `size`, `n_files`, `c2`–`c4`, `name`). `ORDER BY (at, depth, path, usr, sign)`, `PARTITION BY toYYYYMM(at)`. This is the **change-keyed access path** (§2.3).
- **`names`**: every lowercase segment name ever seen, `ReplacingMergeTree ORDER BY l`, with a `text(tokenizer = ngrams(3))` index: the filter's vocabulary. Append-only; a name whose paths are all gone stays (it then matches no node).
- **`scans`**: the ingested scans: `id` as the site names them, the source's version (1: a dirs-only v1 index; 2: a store generation), rows, versions opened and closed, seconds, the source.

Nullable source fields are stored as sentinels, since sort keys can't be `Nullable` and a null map costs a column: `usr = ''` (unattributed), `n_children` / `n_desc` / `mtime` / `last_read = −1`, and a missing `mtime_mean` as `0` with `mtime_w = 0` (the weight the Worker's `merge` skips).

### 2.2 Index types used, and why

- **Sparse primary key** `(depth, path, usr, vf)`: every read the box makes is either one depth's children of a few kept parents (an OR of `path` ranges at one `depth`) or a point read (`(depth, path) IN (…)`); both prune to a few granules.
- **Partitions** on `toYYYYMM(vt)`: the as-of predicate `vf ≤ D AND vt > D` prunes every closed partition whose month is before `D`'s by the partition's min/max `vt`, so the latest scan reads the open partition only. Partitions are also how the open versions are replaced in one atomic step (§3).
- **Projection** `by_name`: a filter's candidates are the nodes of a few thousand names; sorted by name they are a handful of ranges. A projection, not a second table, because it is maintained with every part (the open partition's swap carries it) and the optimizer picks it per query.
- **Text index** (`ngrams(3)`) on `names.l`: the `LIKE '%term%'` / `startsWith` / `endsWith` segment tests read only the granules holding the term's trigrams. Regex name tests (`match`) can't use it; they scan the 127M-name table (a few GB).
- **No `minmax` / `set` / bloom skip indexes on `nodes`.** `vf` and `vt` are uncorrelated with the sort order (a granule holds paths from every era), so a `minmax` on them prunes nothing in the open partition; the partitioning does the interval pruning instead. Bloom filters on `name` are superseded by the projection.

### 2.3 Open versions and their closing, without heavy mutations

- Closed versions are written once, to the month partition of their `vt`, and never touched again.
- The open versions are **rewritten whole** at each ingest into a staging table of the same structure, then swapped in with `ALTER TABLE nodes REPLACE PARTITION ID '210601' FROM …`: a metadata operation (hard links), atomic for readers. No row is ever updated or deleted in place; the old open parts are dropped by the swap.
- The cost is writing the scan's rows once per ingest (the same order of work as loading the scan at all, §6.2). The alternative — appending new versions to the open partition and lightweight-deleting the closed keys — writes only the churn but leaves a delete mask on every open part; not built.
- The only mutation is on the recovery path: a re-run deletes a crashed attempt's appended rows (`vt` or `at` = the scan; a lightweight delete on one small partition).

### 2.4 A scan "as of D"

- A scan is the set of versions with `vf ≤ D < vt`.
- **Between scans** (a missing day), `D` resolves to the newest scan before it: its versions have `vf ≤ D` and `vt` ≥ the next scan. The box serves only ingested scan ids (another date is a 409 and the Worker answers), but the store itself answers any instant.
- **A partial scan** (a bucket's listing missing, a truncated index) is refused at ingest (§3), so it never becomes a version: as-of its date, the store shows the previous complete scan. The daily job already refuses to publish a scan with a bucket missing; this is the store's own guard.
- **A dirs-only (v1) scan** next to a store (v2) one: the diff folds the store side's objects into `(other)`, as the Worker does. The switch from v1 to v2 sources opens a new version of every directory (v2 adds child counts and stamps).

## 3. Daily ingest (`dt-cloud ch-ingest -d DATE [SRC]`)

SRC is the scan's path store `path` sort (a v2 generation's or a v1 index; default the newest generation under `gs://<bucket>/listing/<date>/`), read server-side from the box's `user_files` (`-F`) or streamed to the server as Arrow.

1. **Stage**: the scan's rows (`s = 1`) and the current open versions (`s = 0`, with their `vf`) into one table sorted by key.
2. **Pair**: an in-order `GROUP BY (depth, path, usr)` over the stage, cut into `threads × 4` key ranges (from the stage's primary index) run concurrently (one in-order aggregation is one thread). It inserts into a `Null` table whose four materialized views fan each key out: its open version for the scan (`vf` kept when unchanged, else the scan), the closed old version (`vt` = the scan) when it changed or vanished, and both as `changes` events. A source that lists one slice twice (v1 indexes hold duplicate unattributed rows) is merged the way the Worker's `merge` sums it.
3. **Swap**: the new open versions, plus a sentinel row (`depth = 0`, `vf` = the scan), replace the open partition.
4. **Record**: new names into `names` (from the day's opened versions only), the scan into `scans`.

**Idempotent and re-runnable:** a scan in `scans` is a no-op; one whose swap happened (the sentinel says so) is only recorded; anything else starts over after deleting what a crashed attempt appended. Scans are appended in order: an older scan than the open one is refused (a back-fill is a rebuild).

**Partial-scan guards:** a depth-1 root the open versions have and the scan lacks refuses the ingest (`--allow-drop` closes them), as does a scan of under half the open rows (`--force`).

**In gcs's daily job:** `job/run.sh` runs `ch-ingest` on the local `path` sort right after the index publish when `CH_STORE_URL` is set (off by default; `batch-submit.sh` forwards it on manual submits; the job image wasn't rebuilt). A failure warns and never fails the snapshot: the box 409s the scan it lacks and the Worker answers.

## 4. Serving (`dt-cloud serve-query -e ch <clickhouse-url>`)

`dt_cloud.chstore.serve` answers, in the Worker's response shapes, at any ingested scan:

- `/api/subtree`, plain: P's own slices give the threshold `P.b · minArea / (w·h)`; then one read per depth of the children of the paths kept one level up, grouped per path with `sum(size) ≥ thr(depth)`. Exact: bytes are monotone up the tree, so whatever clears a level's threshold has a parent that cleared the shallower one.
- `/api/subtree?q=`, filtered: the `mem` box's semantics (`dt_cloud.box.view`) in SQL on one session's temporary tables. Candidates by name (`names` → `by_name`), full-path flags, roots = the outermost of `pos ∧ ¬neg` and exclusions = the outermost of `neg` charged to their root, each one window pass in `k` order (the path with `/` as `\0`, so a subtree is contiguous and "nested" is a running max of `k ∥ 0xff`); net aggregates, synthesized ancestors, and the per-root attenuated subtree level by level. Root and match lists stream straight from ClickHouse (`toJSONString`).
- `/api/diff`, plain and filtered: both sides' views at one threshold, the Worker's walk, exact point lookups for the names one side lacks.
- `/api/series`: every version of P's slices (or of `paths=`, or the depth-1 rows for `split=roots`), summed per scan.

Statuses, as the `mem` box: `x-query-engine: box;dur=…` on every response; a scan not in the store 409; a series whose scans differ from the Worker's (`n` / `first` / `last`) 409; a lens 409; owner / class scopes 501; a regex no name filter plans 501; ClickHouse unreachable or failing 503. `/healthz` lists the scans.

**Parity** (`cloud/tests/test_chserve.py`, against `box-parity.json` from `site/functions/_lib/boxParity.test.ts`): 39 filtered subtrees and 3 filtered diffs (also equal to the `mem` box's), 12 plain subtrees, 12 diffs with changes (plain and filtered, against a second fixture generation `v2-search-b`), 7 series; all equal field for field less `tier` / `index` and the Worker's coverage flags. Ingest is tested to reproduce every scan exactly (`test_chstore.py`), including re-runs, crashes at each step, partial scans and v1 sources.

Deliberate differences from the Worker:

- Per-path aggregates always sum every owner slice; the Worker's `bysize` read drops a multi-slice path's slices under the threshold (3,254 such paths on gcs).
- Lookups are uncapped (`lookups_capped` false); the Worker stops at 240.
- On a v1 scan, `(other)`'s `f` counts every folded child; the Worker counts those its coarse tier holds.
- Filters: the `mem` box's (every match found, `folded` past 50K roots, the store-root match covers everything under it).
- No `pv` provenance in the box's trees: the Worker lays it on (§5).

## 5. Worker hand-off (`site/functions/_lib/queryBox.ts`)

- `QUERY_BOX_URL` set (and the primary store, no lens / owner / class scope): `/api/subtree`, `/api/diff` and `/api/series` ask the box after the edge-cache miss, with `Authorization: Bearer $QUERY_BOX_TOKEN` and a `QUERY_BOX_TIMEOUT_MS` budget (default 20 s).
- The box's 200 is the answer, edge-cached under the Worker's key; on a subtree the Worker lays the index extras' `pv` provenance on the tree. The box's 400 / 404 are the answer too.
- A 409 / 501 / 503 / any other status, an unreachable or stalled box, or a body past 64 MiB: the Worker's own read, marked `x-query-engine: worker;fallback=<why>`. A request the box isn't asked: `worker;box=skip`.
- A series carries the Worker's scan list (`n`, `first`, `last`); one whose tier-less scans add `meta.json` points stays the Worker's.
- Unset: nothing runs and no header is added; vitest (`functions/api/queryBox.test.ts`) checks no fetch happens and that every fallback body equals the no-box body.

## 6. Measured

(being filled)

[serving-options]: serving-options.md
[filter-query-service]: filter-query-service.md

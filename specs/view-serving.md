# Every read is a view query: tiered index, server-side scope, objects as leaves, AL as-of

**Status:** design 2026-09-06, from Ryan's review of the 9/5–9/6 OOM thread. Supersedes `path-agnostic-serving.md` (whose §1 audit and §2.1/§2.3 mechanics still hold and are referenced, not repeated) and the "floor" option (#3) in `webdata-memory.md`. Written as "how this looks built from scratch today", then an order that gets there by deleting vestiges rather than wrapping them.

## 0. The rules

1. **The client receives exactly the current view.** One endpoint answers "path + scan + scope + pixel budget" for every path including the root. No artifact is shipped whole because it happens to be the root's answer. Filtering, slicing, mark folding and name search all run server-side; the client only draws and joins two views for a diff.
2. **`tree.json` does not exist.** Not as a file, not as a "coarse tier", not as a cache key. The coarse answer at the root is served from an *index tier*, in the same row format and by the same reader as every other depth.
3. **Floors are tiers, never losses.** A tier is a subset of index rows chosen by an absolute byte floor; sums at every kept row are exact and exhaustive. The query planner picks the coarsest tier whose floor is at or below the query's pixel threshold, so a floor never changes an answer, only which file answers it. The finest tier is the object itself.
4. **Objects are nodes.** A file is addressable, drillable-to, and markable like a dir. The daily job materializes a path-sorted object tier; marks accept exact object keys.
5. **Every derived artifact is reproducible as of its scan.** A re-aggregation of date D reads only inputs that existed before D's listing (access-log rows with `day < D`), so rebuilding history is byte-identical, not "the same plus whatever arrived since".
6. **Caches are path-agnostic.** Edge/LRU cache keyed by the full query (scan, path, scope, budget, ledger head). The root is a hot key, not a special file.

## 1. Artifacts per scan (`gs://oa-gcs-usage-dvx/index/<date>/`)

All parquet, one row schema, row groups of 8k, footers synced to D1 (`index_groups`, shipped), read by the existing `_lib/index.ts` range reader.

| tier | file | rows (9/4 fleet) | floor | sort | variants |
|---|---|---|---|---|---|
| **coarse** | `path-index-coarse<E>.parquet`, E ∈ {16, 20, 24} (48 GiB / 3 GiB / 256 MiB at today's fleet; ~40k / ~500k / ~2M paths) | `F_E = 2^(round(log2(fleet_bytes)) − E)`, recomputed each scan | `(depth, path)` | by-path, by-user |
| **dirs** | `path-index.parquet` | 220M | none | `(depth, path)` | by-path, by-user |
| **objects** | `objects.parquet` | 613M | none (leaf) | `(bucket, name)` = pre-order by path | by-path only |

Row: `path, depth, kind ('d'|'f'), usr, b, o, wts, wb, c2, c3, c4, a` (+ `h`: the log2 size histogram vector, once wanted). Objects rows carry `o = 1`, `created`, `class`; `usr` comes from the same deepest-prefix attribution the dirs get. **There is no group/team column** (excised 2026-09-06): ownership is a person or nobody, and unclaimed = `b` − Σ user slices.

Why the coarse tier fixes the thing `tree.json` was papering over: a pixel-budget query prunes row groups by their `b` max, which works for a narrow path range but at the root the range is the whole file, so a floor-free index makes the root the *slowest* path. A 2M-row tier makes the root a handful of row groups. The threshold at the root is `fleet × minArea / (w·h)` ≈ 40 GB for a 1000×600 canvas, two orders above `F`, and it only drops below `F` several drills down, by which point the path range on the dirs tier is narrow. Bumping the floor exponent by ±1 changes the tier by ~1.5× rows either way (measured: 2.73M at 128 MiB, 1.97M at 256 MiB, 1.17M at 512 MiB); serving cost tracks rows, indexing cost doesn't (the full rollup runs regardless; the floor only gates emission).

Deleted: `snapshots/<date>/tree.json` (and `MIN_FRAC` / `ABS_FLOOR` / `TOP_K` / `build_tree` in the job), `treeFor`/`sliceTree` in `subtree.ts`, `tree.json` reads in `/api/todo` and the og renderer. `age.json` follows once the objects tier serves age strata at any path (`path-agnostic-serving.md` §2.2); `meta.json` stays (scan-level totals, KB).

Also deleted: **`snapshots/series.json`**. Today it is the size-over-time chart's input: every run re-folds *every* archived `tree.json` into one file of per-top-level-path bytes per scan (plus per-user/team from `meta.json`, plus a ledger replay for the mark burn-down). It is a `tree.json` consumer, it is O(scans × tree) per run, and it can only answer for paths that cleared the fold. Replacement: `GET /api/series?path&k&o&t` = one row lookup per scan in that scan's coarse tier (or dirs tier when the path is below `F`), plus the same per-scan ledger fold `k` uses — N scans × one range read, cached per (path, scope, latest scan). The chart then follows every drill and scope exactly like the map.

**The index variants** are the same rows sorted two ways: `(depth, path)` for drills, `(usr, depth, path)` for a user lens (the by-team sort went with the group facet). Parquet has no secondary indexes, so a sorted copy *is* the index (row-group min/max = sparse index). They exist at both the coarse and dirs tiers; the objects tier only needs `(bucket, name)`.

## 2. The one read: `GET /api/subtree`

Keeps its name and its shape (a nested node tree folded to the pixel budget), gains the page's scope axes, and stops reading anything that isn't an index tier:

| param | meaning | how it's answered |
|---|---|---|
| `date, path, w, h, minArea` | scan, root of the view, budget (`/api/series` takes the same scope for a path over scans) | tier choice: the coarsest tier whose `F_E ≤ thr`, else dirs; leaf files from objects when `thr` admits a single object (`b ≥ thr`) |
| `o=<user>` | that user's bytes | by-user variant (shipped as `lens=user:`) |
| `o=unclaimed` | bytes no person owns | by-path rows minus Σ user slices over the same `(depth, path)` ranges (rows are owner slices, so this is exact) |
| `o=claimed` | everything some person owns | Σ user slices per path (the NULL-`usr` slice is the unclaimed pool) |
| `k=` ⊆ `ksu` | mark axis | the ledger fold (`_lib/marks.ts`, cached per `(scan, head)`) gives every live mark its net bytes; per returned node: `val_K(N) = [fate(cover N) ∈ K]·(b(N) − Σ b(m)) + Σ val_K(m)` over the marks directly under N; rows are read by `b` with the threshold computed from the filtered root, so the read is a superset and the fold exact |
| `q=` | name filter | evaluated on the rows under `path` in the chosen tier (root: the 2M coarse rows); a node stays if it or a descendant matches, bytes = matched subtree bytes. From the root this finds matches down to `F`; a drill re-runs it on the dirs tier under that path. A fleet-wide deep search is a separate index (later), not a bigger download |
| `marks=1` | marks under `path` | returned with the view; the client keeps no ledger trie |

Each node carries its resolved mark and claim (`path-agnostic-serving.md` §2.3), so the map colors and rollups are claims-applied like `/users` already is. Cache key = every param + ledger head (only when `k`/`marks` are set). The Diff section joins two such views by child name (`clientDiff.ts`, unchanged). `/api/marks/totals?path=` keeps giving the exact rollup; it is the same fold.

Client deletions: `needFullTree`, `treeQ`, `applyFateFilter`, `applyLensScale`, `applyNodeFilter`, `applyFilter`, `lensNodePred`, `unattrSlice`/`claimedSlice`/`communalSlice`, the lens-broken fallback, `useMarkIndex` for the map (the children table and mark controls read marks off the nodes), `scopeTree`. `App.tsx` becomes: read the URL axes, fetch the view chain, draw.

## 3. Objects as leaves, marks on objects

- **Pattern grammar** (`actions.ts` `PREFIX_RE` → `PATTERN_RE`): `gs://marin-<bucket>/<dir>/` (prefix, as today) or `gs://marin-<bucket>/<dir>/<object>` (exact object; no trailing slash). Ledger rows unchanged: a pattern is a pattern. Resolution (`_lib/resolve.ts`, `sweep_plan.winner`): the newest live row among `ancestorPrefixes(key) ∪ {key}` wins, so an object mark is simply the longest possible match. `keep_last_ckpt` is unchanged.
- **Pricing**: `marks/totals` prices an object pattern by one point lookup in the objects tier (`readAsks` with a `(bucket, name)` ask); net-bytes subtraction works unchanged (an object under a marked dir is a nested mark).
- **Executor**: `execute_plan` already decides per blob via `key_to_prefixes`; it adds the exact key to the covering set. Nothing else changes: the deletion bands and soft-delete guard are per object already.
- **UI/CLI**: a file node in the map or children table (admitted by the pixel budget, or listed under a drilled dir) gets the same `MarkControls`; `gcs-usage mark/status` accept object paths; `TypedPrefixModal` accepts either form.
- **What the objects tier also buys**: exact age strata and size histograms at any path (a `(bucket, name)` range read), `/files`-style browsing of the `marin-*` buckets themselves, and the `resolve` API returning a size for any path, floor or no floor.

## 4. Access log (AL) plane

- **As-of (shipped in this commit):** `webdata` aggregates AL rows with `day < D` only. A scan dated D therefore sees reads through the end of D−1 UTC regardless of which shards exist when it runs, and a REPROC of an old date is exact. (The original daily runs were the *approximation*: usage-log CSVs land hours late, so a 07:00 run had an incomplete D−1. The rule makes the later rebuild the ground truth.)
- **State, not history, per scan:** `access/state/<date>.parquet` = one row per `(bucket, dir)` that exists in D's listing: `last_ts, ro, rb` (and per-op sums). Built incrementally: `state(D) = (state(prev) ⋉ dirs(D)) ⊕ shards(prev ≤ day < D)`. Cost is O(live dirs) + O(new shard rows), not O(every row since 8/13). `webdata` joins the state (≈12M rows today; 81% of the 64M dirs ever read no longer exist) and never materializes a Python dict of it (`amap` deleted; the tree decoration is `coarse ⋈ state`). Raw shards stay as immutable facts and are never re-scanned whole. A dir deleted then recreated starts its read history over, which is the right answer for retention.
- **Granularity:** the shards keep an exact `last_ts`; `a` stops being floored to a day when the wire format is revisited (§6). Writes are already covered by the listing's `wts/wb` (byte-weighted mean created); PUT rows in the AL add nothing except since-deleted objects and stay out.

## 5. Aggregation (job)

Unchanged goal from `webdata-memory.md` #3, restated without the floor-as-loss framing: the rollup runs level-wise (depth max → 1, each level = own dirs ∪ the previous level rolled to its parent) and, when it needs to, prefix-partitioned (a dir and all its descendants share their depth-k prefix, so partitions aggregate independently on any node size and only the k-level stubs roll up at the root). Memory becomes ∝ the largest level (or partition), not the fleet; wall time ∝ fleet / partitions. The coarse tier is one `WHERE pb ≥ F` on the level-wise output; the objects tier is one external sort of the listing (spillable). `DUCKDB_MEM` then drops to what the largest level wants and the node size is a cost knob, which is what a 10× fleet needs.

## 6. Naming

Internal names stop being wire abbreviations: `read_ops`, `read_bytes`, `last_read`, `created_wsum`… in SQL/Python/TS types; the JSON keys (`ro`, `rb`, `a`, `wts`…) survive only in the serializer and the `TreeNode` wire type, or get long names too and lean on gzip. Done as its own pass after §2, when the client's node type is being rewritten anyway.

## 7. Order

1. **AL as-of** (this commit) → rebuild `:latest` → REPROC 9/5 and 9/6 again so history is exact under the rule. *(Cron: point the daily body at the highmem-32 shape until step 5 lands, or the next daily fails the same way.)*
2. **Coarse tiers** — *shipped 2026-09-06* (`43b4b33`, `ca6ab5c`): the job writes `path-index-coarse{16,20,24}[-by-user].parquet` from `ptu` + `tot`, `index-sync` records each floor in D1 (`index_schema.floor_bytes`, migration 0018); `buildView` plans the tier per query; `subtree.ts`, `/api/todo` and the og page no longer read `tree.json` (`treeFor`/`sliceTree` deleted). Still writing `tree.json`/`age.json` until step 3 removes the client's last reads. **Group facet excised** the same day (`79e6ea5`, `99c60a3`).
3. **Server-side scope** — *shipped 2026-09-06* (`e4a43d7`): `o=`, `k=`, `q=` on `/api/subtree` (`_lib/scope.ts`, `_lib/fates.ts`), the home page sends the axes with every view and its client walks are deleted. Verified: scoped roots and their children's sums equal `/api/marks/totals?path=` at every axis. **Remaining `tree.json` readers**: the user page (`/user/:id` — its per-prefix worklist, fates and map still walk the tree; needs a per-user estate endpoint: the user's marks with per-user band bytes in the manifest, plus a `lens=user:&k=u` view for the undecided rows, plus claims applied to the user lens), `series.json` (→ `/api/series`), and the og renderer's users page. `marks=1` on the view (marks returned with nodes) is not done; the client still holds the ledger trie for coloring.
4. **Objects tier + object marks** (§3).
5. **AL state** (§4) — `amap` and the 17 GB go with it.
6. **Level-wise / partitioned rollup** (§5); drop the node to what the measured peak wants.
7. **Naming pass** (§6).

### Follow-ups found while implementing

- **Index rewrite vs D1 footer** (2026-09-06): a REPROC rewrites `listing/<date>/path-index*.parquet` in place, but D1 keeps the *previous* file's row-group offsets until that job's `index-sync` runs ~10 min later — reads in the window decode garbage (`parquet unsupported page type`), and a cancelled job leaves it that way. Fix in the tier rewrite: write tiers to a content-addressed or job-stamped key and switch D1's pointer atomically after the sync (the schema row already is the completeness marker; add the file key to it), never overwrite a key D1 points at.

Scratch for the numbers above: `tmp/subtree-floor-0904.txt` (per-path subtree histogram), `tmp/amap-growth2-0905.txt` (read dirs vs the 9/5 listing), `tmp/file-size-cdf-0905.txt`, `tmp/dir-size-cdf-0905.txt`.

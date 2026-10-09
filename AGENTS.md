# disk-tree

Cloud storage usage analyzer: a scanning/indexing CLI (`disk-tree`), a cloud overlay (`dt-cloud`), and a Cloudflare-hosted site (`site/`) over the indexes.

## Project Vision

Track storage usage across object stores: S3, R2 and GCS buckets.

Key goals:
- **Always-ready index**: Scheduled scans (the r2 demo's daily ingest) so the index is there when you need it
- **Fast indexing**: Shard a bucket's listing across workers (`bulk-list`), aggregate out of core (`import`)
- **Site**: Treemap visualizations, diffs and an age lens over each scan's path index

## Architecture

### Python Backend (`src/disk_tree/`)

**Listing** (`find/bulk*.py`): `bulk-list` shards a bucket's object listing (`gcs://`, `s3://`,
`r2://` via its S3 endpoint) across worker processes into layer-1 listing parquet shards. The
canonical columns are `bucket, name, size_bytes, created, storage_class_id, generation`; generation
is the exact GCS object identity (nullable for S3/R2 and old listings), retained so a reviewed
deletion manifest can never resolve to a replacement object at the same path.

**Aggregation** (`find/import_listing.py`, `find/index.py`, `find/aggregate_{duckdb,stream}.py`):
`import` aggregates a listing bottom-up into the canonical layer-2 frame — columns `path`, `size`,
`mtime`, `kind`, `parent`, `uri`, `n_desc`, `n_children`, `n_files`, `depth` (+ opt-in pivots,
`mtime_mean`, labels), sorted `(depth, path)` so parquet pushdown prunes by depth and path prefix.
Three engines, byte-identical output: `pandas` (in memory), `duckdb` (out of core), `stream`
(O(depth) over sorted listings).

**Output** (`storage/`, `sqla/model.py`): each imported bucket's layer-2 is adopted into the write
dir (`<DISK_TREE_ROOT>/scans/<uuid>.parquet`, or `--to <dir|url>`) and recorded as a `Scan` row
(`path`, `time`, `blob`, root `size`/`n_children`/`n_desc`/`mtime`) in `<DISK_TREE_ROOT>/disk-tree.db`.
cw-s3's jobs read that blob back (`ls $DISK_TREE_ROOT/scans/*.parquet`) as `dt-cloud index-write`'s input.

**CLI** (`cli/`):
```bash
disk-tree bulk-list URI    # Shard URI's object listing (gcs:// | s3:// | r2:// with `-E <endpoint>`) into
                          # layer-1 parquet shards at `-o` (`-a` adaptive range-splitting, `-P` procs)
disk-tree import -l GLOB…  # Aggregate listing(s) into a canonical layer-2 per bucket (`-b`; default every
                          # bucket in the listings): `-e pandas|duckdb|stream`, `-p` pivot sums, `-m`
                          # mtime_mean, `-i` also cut the path-store tiers, `-w/--to` write dir or URL

disk-tree recompress PATH…  # Rewrite v1 layer-2 listings as v2 in place, lossless (spec `listing-slim.md`
                          # phase 2): files, dirs (recursive `*.parquet`, sidecars skipped) or fsspec URLs
                          # (`gs://`, `r2://`, `s3://`). Streams by row group (never a whole file in memory):
                          # drops `uri` (scan root → metadata; refused unless `uri == <root>/<path>` on every
                          # row) and the `sum_*` pivots equal to `size`, re-encodes under
                          # `$DISK_TREE_PARQUET_CODEC` in ≤64K-row groups to a `.v2.tmp` sibling, verifies
                          # (row count + order-insensitive digest of `(path,size,mtime,kind)`), then swaps
                          # (atomic rename; copy + delete on a URL). v2 input is skipped; a failure leaves
                          # the original untouched, exit 1. `-n` dry-run, `-k` keeps `<stem>.v1.parquet`,
                          # `-j` JSON. Per-file old/new size + ratio, and totals
disk-tree listing-format PATH…  # The audit: each parquet's listing format (v1 | v2 | not-a-listing), codec,
                          # row groups, rows, size (+ root/implied for v2) from the footer only; `-j` JSON

disk-tree tiers L2        # Cut the path store's sorts (spec `path-store.md` §1.2/§4.1) from a layer-2 — a
                          # local path or an fsspec URL (copied once through `blobfs.open_read`): `path` =
                          # every row (objects + dirs) sorted `(depth, path, …labels)`; `bysize` = the same
                          # rows sorted `(⌊log2 size⌋ desc, path, …labels)`, size 0 last, the bucket computed
                          # in SQL (never stored). 8K-row groups (`-r`), `tier`/`sort` (+ `bucket: log2`) in
                          # the parquet metadata, the source's listing format inherited. `-t path,bysize`
                          # (default both), `-s STEM` (may be a URL: cut locally, uploaded), `-g` writes the
                          # `.groups.json` footer sidecar beside each (`find/groups.py`; carries `b_min` now),
                          # `-v usr` extra sorted copies led by those columns, `-j` JSON; `-m` DuckDB memory limit (default 8GB — an
                          # external sort, spills to `-T`, default `.duckdb-tmp` beside the stem; unbounded it took
                          # 28 GB for 57M rows), `-p` threads. Prints rows, groups,
                          # bytes, KV per tier. `import -i` does the same at import time (bare `-i` = both;
                          # the `dirs`/`objects`/`coarse` tiers are retired). The cloud overlay's
                          # `dt-cloud index-write` (cw) and `dt-cloud path-index -P` (gcs, the r2 demo)
                          # cut the same two sorts from their bucket unions — objects as rows, L2 column
                          # names — under `path-index[-bysize][-by-user].parquet` (spec §4.2–4.4)
disk-tree tiers plan SIDECAR P THR  # The reader's span selection run offline over a tier's `.groups.json`
                          # (phase 0's instrument): for `path` it mirrors `readRects` exactly (depth rect
                          # `dP+1..`, path range `[P/, P0)`, `b_max ≥ thr·atten^(d−dP−1)` per group); for
                          # `bysize` it is `b_max ≥ thr_min ∧ p_max ≥ P/ ∧ p_min < P0`. Reports groups
                          # selected, rows they hold, bytes (compressed chunks), and — from the parquet —
                          # rows that actually pass, i.e. the decode waste. `P` = `.` for the root; `-a`
                          # attenuation, `-d` max depth, `-t` tier (default: from the name), `-C` skips the
                          # count, `-j` JSON. Measured on a 20K-child flat dir at 2048-row groups: `path`
                          # decodes 10 groups / 20,015 rows for 1,250 answers, `bysize` 1 group / 2,048
```

### Site (`site/`) and widget packages

`site/` is the hosted/serverless app: Vite + React + TypeScript, with Pages Functions over R2 / GCS +
D1 (r2.rbw.sh, the gcs/cw deployments). Its chart-lib-free DIY-SVG/canvas widgets come
from two workspace packages:
- **`@rdub/treemap`** (`packages/treemap/`) — the SOTA treemap core + layout/color primitives:
  `<Treemap>` (SVG + canvas renderers, shared-edge tiling, dust texturing, per-cell `ring`
  emphasis, hover/pin events), `<VoronoiTreemap>` (`/voronoi` subpath), `useHoverPin`, `squarify`,
  `DEFAULT_PALETTE`/`ageFade`/`parseQuery`, `styles.css`. Disk-agnostic; the intended external
  consumer (e.g. file-tree) pins this.
- **`@disk-tree/react`** (`packages/react/`) — `<TimeSeries>`/`<BytesOverTime>` built on the core
  (which it re-exports for back-compat).

### Cloud site auth routes (`site/functions/`)

One gate (`_lib/auth.ts`, `@open-athena/auth` over D1): `identify` → `Identity` (`via: session | grant | public`), `requireViewer` / `requireStager` / `requireAdmin`.
- `/auth/google` (+ `/callback`, `/onetap`) — Google OIDC → email session; `/auth/email/*` — emailed code / magic link → email session
- `/api/auth/*` — the package's routes: `whoami`, `exchange` (`?key=` share link → grant session), `logout`, request-access, admin grant/request/log console
- `/api/token` — a session's personal agent token (a non-expiring Bearer grant: the base scope + assign, never admin; grants can't mint one)
- `/api/me` — the caller's canonical owner id (`_lib/me.ts`): what `user=me` / `lens=user:me` / `owner: '@me'` resolve to

## Development

```bash
# Python setup — one uv workspace: the engine (root) + the cloud overlay
# (`cloud/`, package `dt-cloud`) share ONE `uv.lock` and one `.venv`.
uv sync                                                 # engine only
uv sync --all-packages --all-extras --all-groups        # engine + dt-cloud, every extra, test groups
disk-tree bulk-list -a s3://<bucket> -o work/listing/<bucket>
disk-tree import -e stream -s s3 -b <bucket> -l 'work/listing/<bucket>/shard-*.parquet'

# Site
pnpm install
cd site && pnpm dev
```

## Packaging / Distribution

`uv build` builds the engine's wheel (`src/disk_tree`; CLI only); there is no release workflow.
`dt-cloud` (`cloud/`) is a workspace member installed from the lock, not published.

## Data Flow

1. `disk-tree bulk-list` writes a bucket's sharded object listing
2. `disk-tree import` aggregates it by directory into a layer-2 parquet (+ a `Scan` row), or
   `dt-cloud path-index` aggregates the listings directly (gcs, the r2 demo's daily ingest)
3. `dt-cloud path-index` / `index-write` / `disk-tree tiers` cut the path store; `dt-cloud index-sync`
   publishes its footers to D1
4. `site/`'s Pages Functions read the path store (row-group range reads) and render treemaps / diffs

## Config

Default paths (override with `DISK_TREE_ROOT`):
- `~/.config/disk-tree/disk-tree.db` — SQLite `Scan` rows (`import`)
- `~/.config/disk-tree/scans/` — `import`'s layer-2 blobs
- `~/.config/disk-tree/buckets.yml` — per-bucket `endpoint_url` / `profile`

`import --to <dir|url>` (`config.set_write_target`) writes the blobs elsewhere — a local dir or an **fsspec URL** (`r2://bucket/prefix`, `s3://…`, `gs://…`); `DISK_TREE_SCAN_DIRS` (colon-separated, the first is the write target) sets it from the env. `r2://` rides s3fs with the bucket's endpoint from `DISK_TREE_R2_ENDPOINT_URL` or its `buckets.yml` entry. Every parquet read/write goes through `blobfs.py` (the local-vs-URL seam), and blobs are written in 64K-row groups so a `depth`/`path` pushdown over R2 fetches kilobytes.

**Cross-account credentials** — a `buckets.yml` entry (or `defaults`) may carry a `profile:` naming an AWS credential profile (`blobfs.bucket_profile`), so a source and a target in *different* accounts each authenticate with their own key. It threads to every S3/R2 seam: the `s3fs` blob IO (`_s3fs(endpoint, profile)`) and the `boto3` bulk lister (`S3BulkLister(profile=…)`, `bulk-list -f`). No profile → ambient credentials (env / default profile), the single-account default. Cross-account needs per-bucket endpoints too, so leave `DISK_TREE_R2_ENDPOINT_URL` unset (it globally overrides all per-bucket endpoints).

Stream-engine tuning knobs (env, all with measured defaults — see the constants block in `find/aggregate_stream.py`):
- `DISK_TREE_FLUSH_ROWS` — output row-group size (read-side: smaller = less fetched per directory browse, bigger footer)
- `DISK_TREE_PARALLEL_FINALIZE_MIN_ROWS` — below this the finalize stays serial even at `-j N`
- `DISK_TREE_SCAN_BATCH_ROWS` — pass-1 read-batch size (partition balance / seek granularity)

## Tests

```bash
pytest tests/                    # engine
cd cloud && pytest               # dt-cloud (same venv; sync with --all-packages first)
```

Tests build their listing fixtures inline (`tmp_path` parquets). CI and the job images install `--frozen` from the workspace lock (`deploy/sheet-mirror/Dockerfile` is the reference recipe: `uv sync --frozen --no-dev --no-editable --package dt-cloud --extra …` into `UV_PROJECT_ENVIRONMENT=/usr/local`); a plain `pip install .` resolves fresh and ships pins the tests never ran.

## Performance

- Depth column enables parquet predicate pushdown (only load needed rows)
- The site reads the path store by row-group range reads, planned from footers in D1 (`index-sync`)

### ClickHouse development

Start with `specs/architecture/README.md` for the production serverless design, ClickHouse experiment/read-contract boundaries and RAM/disk alternatives, with Mermaid diagrams. Keep these summaries synchronized when deployment status or accepted read contracts change; the detailed measurement journal remains `specs/ch-store.md`.

The `ch-store` branch explores an append-only historical query service (`cloud/src/dt_cloud/chstore/`; design and measurements in `specs/ch-store.md`). Its local worktree is `wt/ch-store` under the main clone; root-level `git status` does not show that worktree's changes. Run large ingests and benchmarks on the dedicated GCS `ch-store` VM using `job/ch-store.sh`; keep the laptop for source edits, small fixtures, and browser checks. `push` syncs source and node scripts; `push-src` syncs Python packages without re-uploading unchanged scripts. Both replace source on the VM and must wait for active computation to finish. `serve` restarts its query service. The preview at dev.gcs.oa.dev uses this service; production cutover is a separate step.

Run `cloud/tests/test_chstore.py` and `cloud/tests/test_chserve.py` sequentially against a remote ClickHouse server (`CLICKHOUSE_URL`, optionally via an SSH tunnel). Ingestion uploads to one shared server staging file, so separate pytest processes can overwrite each other's fixtures even though their databases are isolated. Preserve the VM's data disk when resizing; record benchmark machine size, service thread count, and whether caches were cold.

Prefer `job/ch-store.sh test cloud/tests/test_chnarrow.py cloud/tests/test_chserve.py -q` for fixture suites: it stages the small tests/fixtures and runs them beside ClickHouse in an ephemeral container, avoiding a WAN round trip per SQL statement. Do not overlap independent test invocations or benchmarks with ingestion/build work. Do not edit `job/ch-store.sh` while an invocation is running; a shell can resume reading a modified script at its old file offset.

For long remote CLI work, use `job/ch-store.sh py-bg TAG ARGS…`; a named detached container survives laptop sleep/SSH disconnection and retains its logs and exit state. Inspect with `py-status TAG` (JSON Docker state) and `py-logs TAG` (save to local `tmp/`). Existing tags are refused, not overwritten. Check both `Running: false` and `ExitCode: 0` before launching dependent stages; source syncs, fixtures and benchmarks must still wait for the active computation to finish. Detachment does not keep local agent orchestration running while the laptop sleeps.

The numeric historical experiment is bounded, not an incremental fleet-wide index: `ch-narrow-build`, `ch-narrow-history`, `ch-narrow-bench -H`, and `ch-narrow-response-bench` keep discovery and complete-body measurements distinct. `serve-query -N TARGET` (or `NARROW_TARGET=TARGET job/ch-store.sh serve`) opts into its exact prefix/selected dates and falls back to canonical serving elsewhere. Keep frozen hierarchy and historical blob coverage limitations explicit; full-body capped-list parity does not establish omitted root identities.

The dev-only `/coarse` viewer and `/api/coarse` are a separate byte/count contract: ordinary leaf modes serve literal exact/substring/suffix matches over leaf basenames, one level per drill, with exact folded remainders and common two-date partitions. Matching directory names are refused in these modes, not silently treated as leaves. Summaries and packed vocabulary have a four-entry process LRU; pattern tables are rebound per request and cleaned up. `ch-narrow-coarse-bench` validates these partitions with an independent complete immediate-child oracle; its subsecond timings are not rich canonical-body or all-history acceptance. Keep `QUERY_BOX_COARSE=1` preview-only. OA Pages deployments need the OA credentials loaded by the `gcs` worktree's direnv, not the root clone's ambient personal-account token.

`levels=2..4` opts into bounded batched refinement at one global byte threshold, with common nested diff partitions (single-date ≤K heavy children per level; diff≤2K). One level remains the default. Preserve exact zero-byte counts and validate every expanded partition. The prepared vocabulary `Set` benchmark is rejected for poor real-data pruning; do not enable it in serving or rerun a broad suite for it.

`ch-coverage-bench` explores positive single-literal full-path substring matches that include directories: deduplicate laminar subtree intervals, sum outer-root weights, and use ordinary rollups inside covered ancestors. Its contract is exact bytes/object counts only, not matching-path counts; an outer-root count is not a matching-path count. The dev viewer's explicit `mode=coverage` uses packed root positions and cumulative byte/object arrays (`-r/--resident-roots` in the benchmark), so requests do not depend on the closed build session's temporary tables. It shares the four-entry LRU, refuses more than 2M outer roots (about 64 MiB of packed locator payload), and serves one level per drill with aligned two-date partitions. Do not mislabel its object totals as matching paths or enable it in production. Neither this prototype nor frozen DFS range summaries solve stable-ID allocation or arbitrary Boolean/owner/age queries.

`ch-coverage-bench -g ancestry` tests outer-root discovery via opaque parent IDs and selected-parent ancestor membership, using the existing numeric-parent/hierarchy indexes and temporary candidate sets. It does not copy a billion-row ancestry table. Snapshot presence must be applied first; missing candidate parents/hierarchy are errors. Subsequent range lookup still uses frozen DFS positions, so this flag is not an incremental serving implementation. The live coverage plan remains interval deduplication unless separately validated and selected.

`chstore/stable_ids.py` and `ch-stable-id-bench` are bounded session-only identity staging, not a durable allocator. Reservations/input must remain immutable across retry; the caller must supply all relevant known path/parent bindings and hold the future single-writer/publication protocol. Do not pass the entire billion-row dictionary into its bounded validation or treat staging success as a committed generation. Original path spelling is identity, independently of lowercase search names. No fleet identities are allocated or changed by the synthetic benchmark.

`chstore/reservations.py` is a single-node persistent reservation journal, not the ClickHouse publication authority. Hold its local process writer lock through external writes, resume a pending batch with identical checkpoint metadata, and pass its exact `reserved_count` to staging. `observe_commit` requires a separately verified committed manifest and binding fingerprint; hashes supplied to that method do not perform verification. Readers must not use journal state as their visibility barrier. No fleet journal/bootstrap is deployed; disk loss and multi-machine coordination remain separate work.

`stable_ids.checkpoint_fingerprint`/`stage_reserved` verify complete bounded identity inputs and root scope, not scalar/rich scan contents. Known bindings must come from the immutable predecessor, not whichever dictionary is latest after publication. `identity_publish.IdentityPublisher` is an isolated identity-batch publication prototype: immutable attempt rows stay hidden until verified markers, generation-pinned readers select one equivalent complete attempt, and partial/conflicting attempts must never be mixed. Its tables use explicit insert/part-directory fsync; no cross-table transaction is assumed. `ch-identity-publish-bench` creates/drops only a fresh owned synthetic database and refuses an existing artifact directory. Do not bootstrap the fleet, enable the daily hook, garbage-collect old data or call this complete scan publication based on these fixtures.

The identity publisher can read a previously audited immutable `(id,depth,path)` baseline plus its committed overlay without copying the baseline. The journal's initial counter must exceed every baseline ID, and commit manifests pin the baseline declaration. That declaration is not an audit or content checksum: the caller must ensure immutability/uniqueness and retain the existing baseline parent/lineage reader. `source()` returns overlay bindings only; `known_source()` includes baseline lookup rows.

`ch-scoped-pattern-bench` explores subtree-first leaf basename substring queries without enumerating global name IDs. It refuses union intervals over 1M nodes and matching nonleaf nodes, validates complete immediate-child byte/count partitions, and never samples accepted contributions. Its temporary posting index is session-owned and must not enter the process LRU. This is not a live fallback or global broad-query acceptance.

`ch-short-query-census` is a sizing instrument, never sampled search acceptance: exact global vocabulary character/window counts, hash-sampled distinct substring support at lengths 1..7, <=100K sampled names, <=2048 characters/name, 1 GiB/30-second query caps. Sample distinct support is a lower bound, not a linearly extrapolated global count. See `specs/architecture/short-query-index.md` for measured envelopes and proposed full-path first-activation/FM alternatives.

`chstore/order_keys.py` explores stable subtree intervals from root-to-node opaque ID sequences: `[key, key[:-1] + [last_id + 1])`. Alphabetical sibling order is not required for range sums/byte-rank discovery. Appending descendants leaves existing keys/bounds unchanged; do not call this an implemented incremental reader. `ch-order-key-bench` is synthetic single-name geometry, with an initial dense-range scalar oracle and an appended-descendant check. `ch-order-key-sample-bench` is an explicitly capped real-data geometry sample, never complete-query/FTS acceptance or a fleet storage estimate. Physical table comparisons must remove the unused lineage column from the scalar baseline. Logical UInt64 lineage values are depth× wider than one UInt64 rank; compression, real name-ordered pruning and historical summary maintenance remain acceptance gates.

`ch-order-key-history-bench` checks signed additive own-leaf history under unchanged bounds against complete synthetic snapshot sums, including deletion/resurrection. It does not sum recursive directory rollups or preserve non-additive maxima, and it does not implement durable publication or maintained query summaries. Keep these scalar screens distinct from complete TM/dTM latency.

`ch-order-key-posting-history-bench` validates complete exact-basename leaf scalar history inside one prefix on two frozen dates; over 300K union leaves is refused, not sampled. `key_history.build_deltas` bounds the state grid before expensive uniqueness checks and rejects duplicate/invalid lineage/state inputs. This is not a TM or a persistent incremental pipeline. Leaf-pattern/coverage vocabulary discovery retains at most budget+1 names and refuses over-budget patterns before posting scans; accepted queries must remain complete.

For fleet snapshot construction, `ch-narrow-build -b N` restricts both snapshot rows and closure membership to sampled disjoint `(depth, path)` ranges. `-R snapshots-partial` may reuse a completed legacy snapshot only when its exact successful creation/root queries and written-row counts match the requested source/prefix/dates; it refuses unverified partial tables. It is not per-range resume. `ch-narrow-hierarchy-stream TARGET` separately tests O(depth) directory ancestor construction over completed parent paths; `ch-narrow-history -a` opts into that construction instead of repeated string ancestor joins. Keep correctness fixtures separate from real-data speedup claims.

---

## Marin GCS usage — API for agents

The user-facing guide (token, HTTP API, recipes for finding, staging and assigning prefixes) lives in [`API.md`](API.md), which the gcs build also serves unauthenticated at <https://gcs.oa.dev/llms.txt> (the `llms-txt` plugin in `site/vite.config.ts`). Document API changes there, not here.

---

### Dev context — gcs deployment

`API.md` is the **user context**; the shared dev guide at the top
covers the engine, site, packages and Python setup. This section is what's
specific to the gcs deployment. The repo is **public**: no emails or other PII
in tracked files or commit messages (name + GitHub handle max); sizes and $
stay behind the site's auth.

#### Branch

`gcs` is a deployment branch (`specs/branch-layout.md`): shared code lands on
`cloud` and merges in (never rebase); this branch carries only gcs's delta —
`job/`, `cf/` (Pulumi stack), `site/wrangler.toml`, the `site/migrations/gcs`
D1 lineage, branding, and this section.

#### Job

`job/` — daily snapshot pipeline on **GCP Batch** (`run.sh` entrypoint,
`batch-submit.sh`, `build.sh` → Cloud Build image). Cloud Scheduler cron
`gcs-usage-snapshot-daily` 07:00 UTC runs the `:latest` image built from this
branch (live in GCP, body edited in place, never regenerated). A rebuild is how
`job/` or pipeline changes reach prod.

#### Dev workflow

- `site/dev` — Vite on **:3253** + `wrangler pages dev` on **:3254** (a stale
  `oa_auth` cookie on localhost disables the DEV_IDENTITY stub → sign-in wall).
- `site/deploy` — **manual** deploy to gcs.oa.dev (pushing git does NOT deploy);
  moves the local `prod` branch pointer to the shipped SHA and pushes the branch
  + `prod` to GitHub (`o`) by default (`-P` to skip). `site/deploy --status`
  compares deployed vs HEAD; `site/deploy --dev` ships to dev.gcs.oa.dev.
- D1 schema: `wrangler d1 migrations apply oa-gcs-usage-auth --remote` (separate
  from deploy; needs `CLOUDFLARE_ACCOUNT_ID` inline).

#### Data flow

Daily Batch job: per-bucket DIY listings → `gs://oa-gcs-usage-dvx/listing/<date>/`
(+ `dir-cache/`) → `dt-cloud path-index` cuts the path store under
`index/<gen>/` (one generation per run, never overwritten) and `index-sync`
publishes its footers to D1. D1 keeps the latest 30 scans' footers
(`INDEX_RETAIN`); older scans are read via each tier's `<tier>.groups.parquet`
cold footer file. D1's `index_schema` row is the pointer. `webdata` writes
`snapshots/<date>/{tree,age,meta}.json` (+ `series.json`, `rules.json`), which
the site reads straight from the bucket (`functions/data/[[path]].ts`; no site
rebuild on new data). Owner assignments live in D1 (actions ledger) and apply on
top of the latest scan. Access logs ingest on the same job for the read-recency
lens.

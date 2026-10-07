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
- `/api/token` — a session's personal agent token (a non-expiring Bearer grant, the base scope only; grants can't mint one)

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

---

## Marin GCS usage — ownership & staged deletion: API for agents

This repo powers <https://gcs.oa.dev>: **who owns what** across the `marin-*`
GCS buckets, and a **staged-deletion** workflow to reclaim space. Anyone
signed in can **stage** prefixes for deletion (the trash gesture in the UI, or
the API below); admins review the shared plan at [`/staged`](https://gcs.oa.dev/staged)
and dispatch the executor. **Nothing is deleted at stage time** — staging is a
proposal, reversible until an admin runs it. Ownership is the other axis:
admins **assign** prefixes to people; anyone can **claim** an unattributed one.

(The earlier opt-out "mark & sweep" model — keep/sweep marks plus a deadline —
was retired 2026-09-28; `dt-cloud mark|status|todo` and `/api/marks*`,
`/api/resolve`, `/api/todo` are gone.)

### 1. Get a token

Every write is authenticated as **you** by a personal bearer token.

- **Browser:** sign in at <https://gcs.oa.dev> with your `@openathena.ai` (or
  whitelisted) email, then reveal/mint your token from the user menu (top-right).
- **API:** `POST /api/token` from a signed-in browser session mints (or rotates)
  it; the raw token is shown **once**. `GET /api/token` reports status (never the
  token); `DELETE /api/token` revokes it.

The token carries only the `gcs` scope (least privilege — it can read and stage,
not administer), and re-checks your email on every request, so revoking it or
removing your access is instant.

```bash
export GCS_USAGE_TOKEN=…        # the token you copied
export GCS_USAGE_URL=https://gcs.oa.dev   # the site
```

Prefixes are always `gs://marin-<bucket>/<dir>/…/` — **directory prefixes only**
(trailing slash), one of the six `marin-*` buckets.

### 2. HTTP API

All under `https://gcs.oa.dev`. Reads are open to any signed-in viewer; writes
need `Authorization: Bearer $GCS_USAGE_TOKEN`.

#### Staged deletion

| Method & path | Purpose |
|---|---|
| `POST /api/plans/stage` | Stage prefixes: `{ prefixes: [...], note? }` → `201 { plan_id, batch_id, staged, covered, absorbed }`. A prefix under an already-staged ancestor comes back as `covered` (nothing to add); staging an ancestor `absorbs` its staged descendants (the plan keeps the ancestor). Any signed-in stager. |
| `GET /api/plans/staged` | The shared open plan, grouped the way `/staged` shows it: items with who/when/note, and the runs against it. |
| `DELETE /api/plans/<id>/items` | Unstage: `{ prefixes: [...] }`. Stagers may remove their own items; admins any. |
| `GET /api/plans`, `GET /api/plans/<id>` | Plans and one plan's items / batches / runs. |
| `POST /api/plans`, `PATCH /api/plans/<id>`, `POST /api/plans/<id>/items` | Admin curation (create, close `{ state: 'closed' }`, add items). |
| `POST /api/sweep/dispatch` | Admin: run the executor over a plan — `{ plan_id, mode: 'dry' \| 'real', date, buckets? }` → a GCP Batch job; `GET /api/sweep/jobs` lists runs, `POST /api/sweep/stop` stops one. |

```bash
# stage two prefixes for deletion
curl -X POST https://gcs.oa.dev/api/plans/stage \
  -H "Authorization: Bearer $GCS_USAGE_TOKEN" -H 'Content-Type: application/json' \
  -d '{"prefixes":["gs://marin-us-east5/scratch/old-run/","gs://marin-us-central2/tmp/2025/"],"note":"superseded by run 42"}'
```

#### Ownership

| Method & path | Purpose |
|---|---|
| `GET /api/owners?date=<scan>` | Per-user owned bytes + storage-class mix for a scan (what `/users` ranks): `{ scan, head, bytes, objects, users: { <id>: { b, mix } } }`. |
| `GET /api/estate?date=<scan>&user=<id>` | One user's estate: `{ user, date, head, bytes, objects, mix, claims }`. |
| `GET /api/assignments?date=<scan>` | The assigner × assignee matrix (who assigned what to whom, in bytes). |
| `GET /api/actions` | The live ownership ledger: `{ owners: [...] }`, every expanded prefix joined to the action that set it. |
| `POST /api/actions` | **Admin.** Append one action or an array: `{ pattern, owner, memo?, scan? }` — `owner: '@me'` resolves to you, `null` clears. Prefix patterns only. |
| `POST /api/claims` | Claim an unattributed prefix as yours: `{ prefix }`; `{ prefix, release: true }` releases. |

#### Data

| Method & path | Purpose |
|---|---|
| `GET /data/scans.json`, `/data/<scan>/{tree,age,meta}.json`, `/data/rules.json` | The published per-scan artifacts the UI renders (tree = size-floored rollup; meta = totals + per-user/class bytes). |
| `GET /api/subtree?date=&path=&w=&h=` | Pixel-budget subtree of any path — UI-shaped `TreeNode`s (`{n,b,o,d,a,us,cb,c}`; unowned = `b` − Σ `us`), folded to what a w×h canvas can draw. What the treemap drills with. |
| `GET/HEAD /api/path-index?date=` | The **floor-free** path index behind `/api/subtree`, as raw parquet with HTTP Range support — bring your own query engine (see below). One row per rolled-up path × owner slice: `(path, depth, usr, b, o, wts, wb, c2, c3, c4, a)`, sorted `(depth, path)`; `usr` NULL = unowned. |
| `GET /v1/files/<path>` | Raw scan-store proxy (range-supporting) over `listing/` + `snapshots/` — the per-object listing parquets, `dir-cache/`, the index tiers (`index/<gen>/path-index*.parquet`; older scans have them at `listing/<date>/` directly), and published snapshot JSONs, addressed by bucket path. |

Human-facing pages, same data: [`/staged`](https://gcs.oa.dev/staged) is the
deletion console, [`/users`](https://gcs.oa.dev/users) the per-user estate
ranking, `/user/<id>` one user's estate, `/assignments` the assignment heatmap,
and `/runs/<id>` one deletion run (progress, logs, what it deleted, recovery).

```sql
-- DuckDB, straight against prod (httpfs sends HEAD + range GETs, so a
-- depth/path predicate only fetches the row groups it needs):
CREATE SECRET (TYPE http, EXTRA_HTTP_HEADERS MAP {'Authorization': 'Bearer <token>'});
SELECT path, sum(b) AS bytes FROM read_parquet('https://gcs.oa.dev/api/path-index?date=2026-08-26')
WHERE depth = 2 GROUP BY path ORDER BY bytes DESC LIMIT 20;
```

### Notes for agents

- **Staging is reversible until dispatch.** Unstage any time (`DELETE
  /api/plans/<id>/items`); only an admin's `real` run deletes, and runs are
  undoable for a window afterwards (see `/staged`).
- **Browse first:** `/api/subtree` (or the parquet index) tells you what a prefix
  holds and who owns it; stage the deepest prefixes that name what you mean,
  not a fat ancestor — an ancestor absorbs everything under it.
- **Explain yourself:** the `note` on a stage batch is what the reviewing admin
  reads on `/staged`.
- There is no client CLI for staging; the surviving `dt-cloud` verbs are the
  pipeline's and the executor's (`healthcheck`, `sweep manifest --plan`, …).

---

### Dev context — gcs deployment

The API guide above is the **user context**; the shared dev guide at the top
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

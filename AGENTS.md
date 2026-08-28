# Marking GCS data for keep / sweep — CLI & API

This repo powers the **mark & sweep** cleanup of the `marin-*` GCS buckets: browse
usage at <https://gcs.oa.dev>, then mark prefixes you want to **keep** (everything
unmarked is swept — deleted — after the deadline). You can mark from the web UI,
but the CLI and HTTP API below let an **agent** do it autonomously: browse the
tree, check a prefix's status, and mark in bulk (including from a regex).

Nothing here deletes on the spot — marking only records a decision in the ledger;
the sweep runs later.

## 1. Get a token

Every write is authenticated as **you** by a personal bearer token.

- **Browser:** sign in at <https://gcs.oa.dev> with your `@openathena.ai` (or
  whitelisted) email, then reveal/mint your token from the user menu (top-right).
- **API:** `POST /api/token` from a signed-in browser session mints (or rotates)
  it; the raw token is shown **once**. `GET /api/token` reports status (never the
  token); `DELETE /api/token` revokes it.

The token carries only the `gcs` scope (least privilege — it can mark, but not
touch admin/cw routes), and re-checks your email on every request, so revoking it
or removing your access is instant.

```bash
export GCS_USAGE_TOKEN=…        # the token you copied
export GCS_USAGE_URL=https://gcs.oa.dev   # optional; this is the default
```

## 2. CLI

Install the `gcs-usage` CLI (Python ≥ 3.12):

```bash
pip install "git+https://github.com/Open-Athena/marin-gcs-usage.git#subdirectory=marin"
```

Prefixes are always `gs://marin-<bucket>/<dir>/…/` — **directory prefixes only**
(trailing slash), one of the six `marin-*` buckets.

### Check a prefix's status

```bash
gcs-usage status gs://marin-us-east5/checkpoints/my-run/
```

Prints the **effective** keep-state and owner of that path (marks are inherited
from ancestors; the most recent mark covering a prefix wins). `-j` for raw JSON.

### List what still needs a decision (the "todo" backlog)

```bash
gcs-usage todo                 # largest undecided prefixes first
gcs-usage todo -p | head       # bare prefixes, one per line (pipe into `mark`)
```

Options: `-n/--limit N`, `-f/--min-frac F` (ignore prefixes below F of total
bytes), `-j/--json`.

### Mark prefixes

```bash
# keep specific prefixes
gcs-usage mark gs://marin-us-east5/checkpoints/keep-me/ gs://marin-us-central2/data/gold/

# keep only the newest checkpoint under each run, sweep the older ones
gcs-usage mark -k keep_last_ckpt gs://marin-us-east5/checkpoints/my-run/

# explicitly sweep (delete) a prefix
gcs-usage mark -k sweep gs://marin-us-east5/scratch/

# from a file / stdin (one prefix per line) — e.g. straight from `todo`
gcs-usage todo -p | gcs-usage mark -k keep -f -
```

Keep actions (`-k`): `keep`, `keep_last_ckpt`, `sweep` (or `none` to leave the
keep axis untouched and only set ownership). Other options: `-o/--owner`
(`@me` by default — claims the prefix as yours; `""` leaves ownership untouched),
`-m/--memo` (note stored with every action), `-n/--dry-run` (print, send
nothing), `-t/--token`, `-u/--url`.

## 3. HTTP API

All under `https://gcs.oa.dev`. Reads are open to any signed-in viewer; writes
need `Authorization: Bearer $GCS_USAGE_TOKEN`.

| Method & path | Purpose |
|---|---|
| `GET /api/resolve?path=<gs://…/>` | Effective keep + owner of a path, with provenance (what the CLI `status` calls). |
| `GET /api/todo?limit=&min_frac=` | Largest prefixes still undecided (the review backlog). |
| `GET /api/actions` | The live ledger: `{ keeps: [...], owners: [...] }` — every expanded prefix joined to the raw action that set it. |
| `POST /api/actions` | Append one action, or an array. Body: `{ pattern, keep?, set_keep?, owner?, set_owner?, memo?, scan? }`. |
| `GET/POST/DELETE /api/token` | Manage your own token (browser SSO session only). |
| `GET /data/scans.json`, `/data/<scan>/{tree,age,meta}.json`, `/data/rules.json` | The published per-scan artifacts the UI renders (tree = size-floored rollup; meta = totals + per-user/class bytes). |
| `GET /api/subtree?date=&path=&w=&h=` | Pixel-budget subtree of any path — UI-shaped `TreeNode`s (`{n,b,o,d,tm,sh,us,cb,c}`), folded to what a w×h canvas can draw. What the treemap drills with. |
| `GET/HEAD /api/path-index?date=` | The **floor-free** path index behind `/api/subtree`, as raw parquet with HTTP Range support — bring your own query engine (see below). One row per rolled-up path × attribution slice: `(path, depth, team, usr, b, o, wts, wb, c2, c3, c4)`, sorted `(depth, path)`. |
| `GET /v1/files/<path>` | Raw scan-store proxy (range-supporting) over `listing/` + `snapshots/` — the per-object listing parquets, `dir-cache/`, `path-index.parquet`, and published snapshot JSONs, addressed by bucket path. |

Human-facing pages, same data: [`/files`](https://gcs.oa.dev/files) browses the
raw store with an in-browser parquet viewer (e.g.
[`/files/listing/2026-08-28/path-index.parquet`](https://gcs.oa.dev/files/listing/2026-08-28/path-index.parquet)
— schema + row-group paging over HTTP ranges), [`/users`](https://gcs.oa.dev/users)
is the per-user mark-status rollup, `/user/<id>` one user's estate, and `/marks`
the recent-actions feed.

```sql
-- DuckDB, straight against prod (httpfs sends HEAD + range GETs, so a
-- depth/path predicate only fetches the row groups it needs):
CREATE SECRET (TYPE http, EXTRA_HTTP_HEADERS MAP {'Authorization': 'Bearer <token>'});
SELECT path, sum(b) AS bytes FROM read_parquet('https://gcs.oa.dev/api/path-index?date=2026-08-26')
WHERE depth = 2 GROUP BY path ORDER BY bytes DESC LIMIT 20;
```

`pattern` is a `gs://marin-<bucket>/<dir>/…/` prefix. `set_keep`/`set_owner`
default to "true if the corresponding field is present", so send `keep` to set the
keep axis and `owner` to set ownership; omit an axis to leave it unchanged. The
ledger keeps the full who-did-what trail — a later action re-paints deeper marks
it covers (recency beats specificity).

```bash
# keep a prefix via the raw API
curl -X POST https://gcs.oa.dev/api/actions \
  -H "Authorization: Bearer $GCS_USAGE_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"pattern":"gs://marin-us-east5/checkpoints/keep-me/","keep":"keep"}'
```

## Notes for agents

- **Marking is reversible until the sweep.** Re-mark or clear (`keep: null`) any
  time before the deadline; nothing is deleted at mark time.
- **Prefer the CLI** — it batches, validates prefixes client-side, and shares one
  token/URL resolution across `status` / `todo` / `mark`.
- **Browse first:** `gcs-usage todo` surfaces the biggest undecided prefixes;
  `status` confirms what a mark will actually affect (inheritance can mean a
  parent already decided it).

---

## Dev context — working on this repo

Everything above is the **www/API user context** (marking data via the site's
CLI/API). This section is for agents *developing* the repo itself. The repo is
**public**: no emails or other PII in tracked files or commit messages (name +
GitHub handle max); sizes and $ stay behind the site's auth.

### Layout

- `site/` — the deployed app: Vite/React SPA (`src/`), Cloudflare Pages Functions
  (`functions/` — auth gating, actions ledger, `/api/subtree`, `/api/path-index`,
  `/data/*` GCS proxy, `/v1/files/*` raw-store browser), D1 migrations
  (`migrations/`), `wrangler.toml`.
- `marin/` — the `gcs-usage` Python CLI (own `pyproject.toml`/venv): attribution
  (`identities.yaml`, rules, W&B mining), `webdata` aggregation, access-log
  ingest, `mark`/`status`/`todo`, `series`, `report`. Runtime-imports the
  `disk_tree` engine below.
- `job/` — daily snapshot pipeline on **GCP Batch** (`run.sh` entrypoint,
  `batch-submit.sh`, `build.sh` → Cloud Build image; Cloud Scheduler crons
  `gcs-usage-snapshot-daily` 07:00 UTC on the `:latest` image built from this
  branch, and `cw-usage-snapshot` 12-hourly on the `:cw` image built from the
  `cw-s3` branch's `job/cw-*` — live in GCP, bodies edited in place, never
  regenerated). One branch per deployment: `specs/branch-parity-discipline.md`.
- `packages/react/` — `@disk-tree/react` widget lib (Treemap etc.); core changes
  here CP upstream to disk-tree (`specs/dt-core-upstreaming.md`).
- `src/`, `ui/`, `tests/` — the vendored **disk-tree** engine + its own app and
  tests (upstream lineage; sessions here rarely touch them, and upstream docs
  describe them).
- `specs/` — design docs; shipped ones move to `specs/done/`.

### Dev workflow

- `site/dev` — Vite on **:3253** + `wrangler pages dev` on **:3254** (a stale
  `oa_auth` cookie on localhost disables the DEV_IDENTITY stub → sign-in wall).
- `site/deploy` — **manual** deploy to gcs.oa.dev (pushing git does NOT deploy);
  moves the local `prod` branch pointer to the shipped SHA and pushes the branch
  + `prod` to GitHub (`o`) by default (`-P` to skip). `site/deploy --status`
  compares deployed vs HEAD.
- D1 schema: `wrangler d1 migrations apply oa-gcs-usage-auth --remote` (separate
  from deploy; needs `CLOUDFLARE_ACCOUNT_ID` inline).
- Python: `cd marin && uv sync && uv run pytest` (viz tests need the root
  `disk_tree` package importable).

### Data flow

Daily Batch job: per-bucket DIY listings → `gs://oa-gcs-usage-dvx/listing/<date>/`
(+ `dir-cache/`, `path-index.parquet`) → `webdata` aggregation →
`snapshots/<date>/{tree,age,meta}.json` (+ `series.json`, `rules.json`). The site
reads the bucket directly via `functions/data/[[path]].ts` — no site rebuild on
new data. Marks/claims live in D1 (actions ledger) and apply on top of the latest
scan ("scan ≈ committed, actions ≈ WAL"). Access logs ingest on the same job for
the read-recency lens.

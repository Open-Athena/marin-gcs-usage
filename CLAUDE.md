# marin-gcs-usage

Storage attribution + **mark & sweep** cleanup for the six `marin-*` GCS buckets.
Live site: <https://gcs.oa.dev> (CF Pages). This repo is **public** — no emails or
other PII in tracked files or commit messages (name + GitHub handle max); sizes
and $ stay behind the site's auth.

**`AGENTS.md` is the API/CLI reference** (marking surface, endpoints, token flow,
DuckDB-over-`/api/path-index` example) — read it before inferring API shapes.

## Layout

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
  `gcs-usage-snapshot-daily` 07:00 UTC and `cw-usage-snapshot` 12-hourly live in
  GCP, bodies edited in place — never regenerated).
- `packages/react/` — `@disk-tree/react` widget lib (Treemap etc.); core changes
  here CP upstream to disk-tree (`specs/dt-core-upstreaming.md`).
- `src/`, `ui/`, `tests/` — the vendored **disk-tree** engine + its own app and
  tests (upstream lineage; sessions here rarely touch them, and upstream docs
  describe them).
- `specs/` — design docs; shipped ones move to `specs/done/`.

## Dev workflow

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

## Data flow

Daily Batch job: per-bucket DIY listings → `gs://oa-gcs-usage-dvx/listing/<date>/`
(+ `dir-cache/`, `path-index.parquet`) → `webdata` aggregation →
`snapshots/<date>/{tree,age,meta}.json` (+ `series.json`, `rules.json`). The site
reads the bucket directly via `functions/data/[[path]].ts` — no site rebuild on
new data. Marks/claims live in D1 (actions ledger) and apply on top of the latest
scan ("scan ≈ committed, actions ≈ WAL"). Access logs ingest on the same job for
the read-recency lens.

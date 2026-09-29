# Marin GCS usage — ownership & staged deletion: API for agents

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

## 1. Get a token

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

## 2. HTTP API

All under `https://gcs.oa.dev`. Reads are open to any signed-in viewer; writes
need `Authorization: Bearer $GCS_USAGE_TOKEN`.

### Staged deletion

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

### Ownership

| Method & path | Purpose |
|---|---|
| `GET /api/owners?date=<scan>` | Per-user owned bytes + storage-class mix for a scan (what `/users` ranks): `{ scan, head, bytes, objects, users: { <id>: { b, mix } } }`. |
| `GET /api/estate?date=<scan>&user=<id>` | One user's estate: `{ user, date, head, bytes, objects, mix, claims }`. |
| `GET /api/assignments?date=<scan>` | The assigner × assignee matrix (who assigned what to whom, in bytes). |
| `GET /api/actions` | The live ownership ledger: `{ owners: [...] }`, every expanded prefix joined to the action that set it. |
| `POST /api/actions` | **Admin.** Append one action or an array: `{ pattern, owner, memo?, scan? }` — `owner: '@me'` resolves to you, `null` clears. Prefix patterns only. |
| `POST /api/claims` | Claim an unattributed prefix as yours: `{ prefix }`; `{ prefix, release: true }` releases. |

### Data

| Method & path | Purpose |
|---|---|
| `GET /data/scans.json`, `/data/<scan>/{tree,age,meta}.json`, `/data/rules.json` | The published per-scan artifacts the UI renders (tree = size-floored rollup; meta = totals + per-user/class bytes). |
| `GET /api/subtree?date=&path=&w=&h=` | Pixel-budget subtree of any path — UI-shaped `TreeNode`s (`{n,b,o,d,a,us,cb,c}`; unowned = `b` − Σ `us`), folded to what a w×h canvas can draw. What the treemap drills with. |
| `GET/HEAD /api/path-index?date=` | The **floor-free** path index behind `/api/subtree`, as raw parquet with HTTP Range support — bring your own query engine (see below). One row per rolled-up path × owner slice: `(path, depth, usr, b, o, wts, wb, c2, c3, c4, a)`, sorted `(depth, path)`; `usr` NULL = unowned. |
| `GET /v1/files/<path>` | Raw scan-store proxy (range-supporting) over `listing/` + `snapshots/` — the per-object listing parquets, `dir-cache/`, the index tiers (`index/<gen>/path-index*.parquet`; older scans have them at `listing/<date>/` directly), and published snapshot JSONs, addressed by bucket path. |

Human-facing pages, same data: [`/staged`](https://gcs.oa.dev/staged) is the
deletion console, [`/users`](https://gcs.oa.dev/users) the per-user estate
ranking, `/user/<id>` one user's estate, `/assignments` the assignment heatmap,
and [`/files`](https://gcs.oa.dev/files) browses the raw store with an
in-browser parquet viewer (e.g.
[`/files/listing/2026-08-28/path-index.parquet`](https://gcs.oa.dev/files/listing/2026-08-28/path-index.parquet)
— schema + row-group paging over HTTP ranges).

```sql
-- DuckDB, straight against prod (httpfs sends HEAD + range GETs, so a
-- depth/path predicate only fetches the row groups it needs):
CREATE SECRET (TYPE http, EXTRA_HTTP_HEADERS MAP {'Authorization': 'Bearer <token>'});
SELECT path, sum(b) AS bytes FROM read_parquet('https://gcs.oa.dev/api/path-index?date=2026-08-26')
WHERE depth = 2 GROUP BY path ORDER BY bytes DESC LIMIT 20;
```

## Notes for agents

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
- `cloud/` — the `dt-cloud` Python CLI (own `pyproject.toml`/venv): attribution
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
- Python: `cd cloud && uv sync && uv run pytest` (viz tests need the root
  `disk_tree` package importable).

### Data flow

Daily Batch job: per-bucket DIY listings → `gs://oa-gcs-usage-dvx/listing/<date>/`
(+ `dir-cache/`, and the index tiers under `index/<gen>/` — one generation per
run, never overwritten, each with a `<tier>.groups.json` group manifest the
site opens once D1 retires the tier's rows; D1's `index_schema` row is the pointer) → `webdata` aggregation →
`snapshots/<date>/{tree,age,meta}.json` (+ `series.json`, `rules.json`). The site
reads the bucket directly via `functions/data/[[path]].ts` — no site rebuild on
new data. Marks and owner assignments live in D1 (actions ledger) and apply on top of the latest
scan ("scan ≈ committed, actions ≈ WAL"). Access logs ingest on the same job for
the read-recency lens.

# Convergence: one codebase, deployments as configuration

**Where this comes from.** The de novo factor (`specs/denovo-factor.md`; ledger §"De novo factor (2026-09-16)") rebuilt gcs's delta on cw-s3 feature by feature with every checkpoint green, and reached the preferred endpoint: commit 12 of `denovo/cw-s3-gcs-2026-09-16` is the **union** — every feature of both deployments, gated so each store behaves as it does today — and the two deployment deltas sit on top as single commits (A = the GCS deployment, B = minus CoreWeave). Everything that could be separated by configuration was, during the build. What remains is four **naming seams** and the deployment plumbing. This spec is the plan to finish: cw-s3 and gcs become the union plus a per-deployment configuration, then one branch.

## State of the two branches (2026-09-16)

- **cw-s3 = the union** (landed 2026-09-16: de novo 1–12 rebased onto `3f7438e` + the D1 lineage guard `afb9027` (`site/migrations/{cw,gcs}`), whoami-source default `19450d5`, legacy `?path=` forwarding `0ea8329`; CI green; cw's prod D1 wants no migrations). Production after Ryan's look at the preview.
- **gcs → union + A** (gcs session): par-construct the generalizations the union introduced — manifest `specs/cp-from-cw-s3-2026-09-16.md` §11 is the exact diff (50 files, +558/−1204): deployment constants → env, `Store.{marks,sweep,lifecycle,peer}`, `EDGE_TRUSTED`/`BASE_SCOPE`/`VITE_AUTH_MODE`, the `plan-sweep`/`cw-digest`/`/api/plan-*` namespacing, deployment-keyed cache keys, and dropping the dead `alert` verb. After this, `cw-s3..gcs` is A and B only.

## The four naming seams — decisions

Each is a real design decision, not a rename; recommendations first, alternatives after.

### 1. Two sweep executors → one plan-first model with two cloud adapters

Today: gcs = owner-slice sweep (marks filtered to `owner == marker`, GCS soft-delete as the safety net); cw = plan-first (marks curated into a named plan, dry → real runs, CAIOS versioning/undo/purge). The union carries both as `sweep` and `plan-sweep`, two D1 schemas, two Batch specs.

**Recommendation:** converge on **plan-first as the general model** — a plan is a curated set of prefixes with runs; gcs's owner slice becomes a *plan builder* ("add everything I marked") rather than a separate executor. Cloud-specific behaviour lives in an adapter: `GcsStore` (delete = soft-delete window, undo = restore), `CaiosStore` (delete = permanent unless versioning, `If-Match` guard, purge). One `/sweep`, one `/api/sweep/*`, one `deletion_runs` table with a `store` column. Alternative: keep both executors under one UI with a per-store switch — cheaper now, but it is exactly the duplication convergence is for.

### 2. Two mark ledgers → gcs's `actions` WAL, with cw's marks as rows

Today: cw's `marks`/`mark_log` (plan-first) vs gcs's actions ledger. The union renamed cw's to `plan_marks` to coexist.

**Recommendation:** one ledger — gcs's `actions` WAL is the more general (append-only, kinds, undo by inverse action); cw's `keep/keep_last_ckpt/sweep` marks become action kinds; `/api/marks` on both stores reads the same table. Plan membership stays a separate table (`plan_items`) referencing prefixes, not marks.

### 3. Two D1 migration lineages → one lineage, applied to both databases

Today: gcs `0001`–`0023`; cw `0001`–`0005` (with `marks` defined twice across the two, and cw's `0005` = gcs's four index migrations folded). Two live databases: `oa-gcs-usage-auth` and `oa-cw-s3-usage-db`.

**Recommendation:** the union's `site/migrations/` = gcs's `0001`–`0023` + cw's plan-first tables renumbered `0024`–`0027` (`plans`, `plan_items`, `plan_marks` or the action kinds from §2, `deletion_runs` extensions), every statement `IF NOT EXISTS`. Bring-up per database: gcs's D1 applies `0024+` (new tables, no-ops for what it has); cw's D1 has cw's `0001`–`0005` recorded under the *old* names — either (a) re-record: insert the gcs lineage's rows into `d1_migrations` as already-applied where the tables exist and apply the rest, or (b) create a fresh D1 for cw from the merged lineage and copy the five small tables (marks, plans, plan_items, deletion_runs, admin_emails — hundreds of rows). **(b) is simpler and reversible** (the old DB stays until the cutover is verified). Until this lands, cw-s3 keeps gcs's lineage isolated so `migrations apply` on cw's D1 wants nothing.

### 4. Two digest contents → one engine, a content profile per store

Today: one thrds mechanism (state/plot hosting, day-keying, converge), two content modules (`digest` Shape C with $/classes; `cw_digest` framing A with `% of 1 PB`).

**Recommendation:** `digest/` with `engine.py` (mechanism) + `profiles/{gcs,cw}.py` (OP body, reply, plot panels), selected by `Store.digest`. The reply variant (`sender`/`body`), day rule, and plot pipeline are already shared. Discord twin + weekly stay gcs-only by config (`Store.discord`).

### 5. Deployment-specific documentation → deployment-neutral

The union carries gcs's `CLAUDE.md`/`AGENTS.md` (the marking-CLI guide, addressed to gcs.oa.dev) as the repo's front door. On a two-deployment repo the top-level docs must describe the app and its deployments (with a short per-deployment section: site URL, store, what marking/sweeping means there), and the deployment-specific how-tos move under `docs/<deploy>/` or into the site's About. Same for README examples and the Slack app READMEs.

## Deployment as configuration

After the seams, a deployment is:
- a **store row** (`stores.ts`: key, label, scheme, base, prices, marks/sweep/lifecycle/peer/digest flags, attribution on/off);
- **wrangler config** (Pages project, D1 binding, vars: `ACCESS_AUD`, `STAFF_DOMAIN`, `EDGE_TRUSTED`, `BASE_SCOPE`, `SITE_URL`, `D1_DB_*`, `SNAPSHOTS_SUBDIR`, `ROOT_LABEL`, `INDEX_VARIANTS`, `WARM_PATHS`) and secrets;
- the **job**: one image, `JOB` selects `run.sh` (GCS) vs `cw-run.sh` (CAIOS) — later one `run.sh` with a store adapter; Scheduler body = env + secrets;
- **IaC**: the Pulumi side (D1, Pages project, Access app, GCP SA/Scheduler/Secret Manager, bucket lifecycle rules via `lifecycle push`) per deployment.

## Branch endgame

1. cw-s3 = union (this week); gcs = union + A (gcs session).
2. Land the four seams on both (one spec each is overkill — this document is the spec; each seam is one PR-sized change, built de novo on the union with the checkpoint protocol).
3. Collapse: one `main`; `cw-s3` and `gcs` become deployment *configs* (store row + wrangler env + job vars), first as branches carrying only config, then as directories/env in `main`. The branch-per-deployment ledger and the CP cursors retire with them.
4. The DT question — whether the union lives upstream as the "cloud app" package — is decided after 3, not before: by then the app is one thing with adapters, which is the shape upstream can take.

## Non-goals here

Renaming the repo; changing the attribution model; the Access seat/allowlist policy (separate); the hidden-bytes tier and lifecycle-effects runs (product work on top of the union, `cw-s3-worktree-session` memory has the list).

# gcs → union + A: what to parallel-construct on this branch

Companion to cw-s3's `specs/convergence.md` (the joint document) and `specs/cp-from-cw-s3-2026-09-16.md` §11. cw-s3 is being fast-forwarded to the union (de novo 1–12); this is the gcs-ward half: what gcs takes so that `cw-s3..gcs` collapses to the two deployment deltas, plus the cw-s3 *features* that turn out to be real features for GCS too, and a position on the "different user notions" question the seams raise. Written 2026-09-16 from the tip `denovo/cw-s3-gcs-2026-09-16` `59433de` diffed against gcs `6d8a782`.

## 1. The generalizations (manifest §11) — take as one adapted CP

`git diff gcs denovo/… -- . ':!specs' ':!pnpm-lock.yaml'` is 44 files, +534/−504, and reads as three groups:

- **Deployment → config.** `Env` grows `EDGE_TRUSTED` / `BASE_SCOPE` / `ROOT_LABEL` / `GCP_SA_KEY`; `requireViewer` = `requireScope(ctx, baseScope(env))` and every API gate uses it (the three `cw/`-prefix scope switches in `subtree`/`diff`/`path-index` go); `view.ts` names the root from `ROOT_LABEL`; `index_footer.py` reads `D1_DB_ID` / `D1_DB_NAME` / `INDEX_VARIANTS`; `warm.py` reads `WARM_PATHS`; `site/deploy` / `dev` / `cf-status` read `wrangler.toml` / `package.json`; the image takes a `JOB` build arg. **Every default is gcs's current value**, so behaviour here is unchanged.
- **Store fields.** `Store.{marks, sweep, lifecycle, peer}`; `SiteNav` / `Root` / `App` / `title.ts` / `About` / `UserPage` read them instead of `store.key === 'gcs'` and hard-coded brand strings; `ChildrenTable` drops the `read` / `owner(s)` columns when no row carries them (no-op on gcs: access logs and attribution both exist).
- **Housekeeping.** `cli.py` loses the dead `alert` verb (undefined `_snapshot_dates` / `_load_meta` — pyflakes would have caught it); the rest of that file's 577-line raw delta is 163 lines after move detection (the index/warm verbs moved next to their siblings). `test_warm.py` gains sub-daily-id cases; `pyproject.toml` lists `sheets` beside `plot`; CI matrixes `[gcs, cw-s3]` and adds the `@rdub/treemap` job.

**One leak to fix rather than take:** the union's `AuthGate` wall copy is cw's ("restricted to Open Athena members", "Sign in with Open Athena"). On gcs that is wrong — the wall explains Google-with-allowlisted-account, one-time PIN, and share links. The wall's restriction sentence and sign-in label belong on the store row (`Store.wall: { restrict, signIn, how? }`), not in the union as cw's text. Everything else in §11 applies as-is.

Order: one commit, adapted from the tip diff, checkpoint = `tsc` (site + functions), vitest, the py suite, and a CIC pass on the dev server (home, `/users`, `/sweep`, `/files`, the login wall) — the union was CIC'd for the cw store only. Cursor marker `cw-s3 <tip>`.

## 2. cw-s3 features that are GCS features too

Checked against the buckets, not assumed:

- **Bucket lifecycle rules as tracked state + the home-page fold.** The six `marin-*` GCS buckets carry the same `tmp/ttl=<N>d/` → Delete-after-N-days rule set that CAIOS has (verified 2026-09-16 with `gcloud storage buckets describe`, e.g. `marin-us-central2`, `marin-us-east5`, `marin-eu-west4`). Nobody looking at gcs.oa.dev can see that today, and the same "TTL blobs and how close they are to purge" question cw-s3 answered applies here verbatim. cw's `dt-cloud lifecycle pull|diff|push|gc-rule` is CAIOS/boto3; the GCS adapter is `gcloud storage buckets describe --format=json(lifecycle_config)` / `update --lifecycle-file`, one tracked file per bucket (`job/lifecycle/<bucket>.json`), `run.sh` snapshotting each bucket's rules to `<scan>/lifecycle.json` (six buckets → keyed by bucket), and `Store.lifecycle` pointing the fold at it. This is the one genuinely new feature on this list; it is also the substrate for a "past TTL / due within N days" breakdown of `tmp/` (cw's `expire-manifest` idea) over the layer-2 parquet, which on GCS is read-only analytics since lifecycle already enforces.
- **`--redo-replies`** on the digest (post new, then delete old; dry-run default). General mechanism, no GCS dependency; low priority until a reply rewrite is needed.
- **`/api/whoami` + `login.ts`, `cw_digest*`, `sweep.py` (plan-first + CAIOS), `cwBatch.ts`, `cw-run.sh`, `cw-webdata.py`, the `If-Match` / versioning guards** — cw's deployment and adapters. They arrive on gcs only as the union's inert modules (gated by store/env), not as features; the plan-first sweep is the convergence seam below, not a port.

## 3. The "different user notions" — three things, not one

The seams look like "cw has no users" but split cleanly:

1. **Identity** (who is viewing). gcs: an app session minted at `/auth/sso`, authorized by the D1 allowlist or a share-link grant, staff by domain. cw: the CF Access edge session for a whole-host, OA-only app. **Resolved by config** in the union (`EDGE_TRUSTED`, `BASE_SCOPE`, `VITE_AUTH_MODE`); nothing left to decide. (Whether `@open-athena/auth` should replace Zero Trust as the IdP is a separate thread.)
2. **Ownership** (whose bytes). gcs attributes bytes to people (identities.yaml, W&B, path signals → the `usr` axis in the path index, `/users`, claims). cw has no attribution. **Resolved by data + one flag**: `hasAttr` is derived from the scan, `Store.marks` gates the ledger pages, `ChildrenTable` hides the columns. A store either publishes attribution or doesn't.
3. **Authority** (who may delete what) — the real question, and it is a *safety model*, not a user model:
   - gcs: anyone may mark; a sweep only touches bytes whose owner is the marker (the owner slice), unmarked bytes are sweep-eligible after the deadline, GCS soft delete is the net.
   - cw: admins curate `sweep`-marked prefixes into a plan; nothing is deleted without an explicit `sweep` mark inside a dispatched plan; CAIOS versioning is the net.

   `convergence.md` recommends plan-first as the general model with gcs's owner slice as a *plan builder*. Agreed, with one sharpening: the owner slice is an **authorization rule**, not just a way to fill a plan. In plan-first terms a plan carries an eligibility policy — `owner == author` for a non-admin's plan, `any` for an admin's — and the executor enforces it at manifest time exactly as gcs's does today. That keeps gcs's "you can sweep your own bytes without an admin" property inside the general model instead of losing it to "plans are for admins". The other divergence to name explicitly: **what unmarked means** — sweep-eligible after a deadline (gcs's whole mark-and-sweep premise, and how the four real sweeps ran) vs untouched (cw). That is a per-store policy (`Store.unmarked: 'eligible' | 'untouched'`), and a plan builder for the gcs model is "everything unmarked in my slice as of the deadline". Both stores' behaviour survives; the code is one executor with a GCS adapter (soft-delete window, restore) and a CAIOS adapter (versioning, `If-Match`, purge).

   The mark ledger (seam 2) follows from this: gcs's `actions` WAL already has kinds and an owner axis; cw's keep-only marks are a subset. One table.

## 4. Order on gcs

1. ~~§1 as one commit (with the wall-copy store field).~~ Done 2026-09-16 (ledger entry "the union's generalizations"). Then `cw-s3..gcs` = A + B.
2. ~~The lifecycle feature (§2)~~ Done 2026-09-16 (ledger entry "bucket lifecycle rules for the GCS fleet"): GCS adapter beside cw's S3 one in `lifecycle.py`, the fold reading either cloud's snapshot, per-bucket map for the fleet.
3. Seams 1–4 per `convergence.md`, jointly.

Not here: the repo/branch collapse itself, the IdP question, attribution changes.

# Handoff: disk-tree is the canonical base; mgu finishes convergence, disk-tree adopts

From: the disk-tree session (`~/c/disk-tree`, branch `cloud`), 2026-09-20.
To: the mgu convergence session.

This is a cross-repo handoff (disk-tree → mgu). It records a decision the user made in the disk-tree session and divides labor so the two repos don't run the *same* convergence twice.

## Decision (user, 2026-09-20)

disk-tree's `cloud` branch is the **canonical base** for every cloud-storage-usage viewer: gcs.oa.dev, cw-s3.oa.dev, r2.rbw.sh, and future rac/oa clouds. gcs/cw will **re-fork off disk-tree** (their branch history retired — the user is fine throwing it away). The user's words: "make this branch the best base for both of them (and our r2 demo, and other rac/oa clouds to come)"; "i really don't want 3 ~separate impls."

This is exactly `convergence.md`'s endgame step 4 ("whether the union lives upstream as the cloud app package") — now answered: **yes, upstream to disk-tree.** Your convergence is not wasted; it's the thing disk-tree adopts.

## What disk-tree has already done (on branch `cloud`)

- **Adopted `m/cw-s3` (the union tip) as the base.** disk-tree `cloud` = disk-tree's `main` (the `ui/` scan-manager superset, `packages/*` with disk-tree's dynamic-OG/diff-index work, `src/disk_tree`) + `site/`+`cloud/` checked out from `m/cw-s3`. Reconcile was tiny: two additive `@rdub/treemap`/`@disk-tree/react` props (`renderTipDefault`, `Series.dots/strokeWidth`) ported into disk-tree's packages, which are a **superset** of mgu's (mgu's `packages/*` are net-negative — they delete `exportImage`/`colors`/`chromeIcons`).
- **Built the r2/public layer `convergence.md` doesn't cover** (OA's two deploys never needed it):
  - **Env-generalized the index-store seam.** `site/functions/_lib/index.ts` now has `storeCreds(env)`/`storeReady(env)` and an env-driven `makeStore` (`STORE_ENDPOINT/BUCKET/REGION/PREFIXES/ACCESS_KEY_ID/SECRET_ACCESS_KEY`, defaulting to GCS + `GCS_HMAC_*` fallback). Every index-serving endpoint gates on `storeReady` instead of the direct `GCS_HMAC_*` check. gcs/cw behavior unchanged.
  - **Public/no-gate auth mode.** `site/functions/_lib/auth.ts`: a `PUBLIC_READ` env flag grants the base viewer scope anonymously at the `requireScope` choke point (reads open; admin/non-base scopes still gated, so mutations stay closed — pinned by `auth.test.ts`). Client: `VITE_AUTH_MODE=public` → `AuthGate` renders children directly.
- **cloud/ made self-contained**: `cloud/pyproject.toml` now declares a path dep on the parent `disk_tree` engine (mgu ran `dt_cloud` from a shared env; the hoisted project needed the dep). disk-tree's `disk_tree.listing.prepare_listing` matches what `dt_cloud` imports — no API divergence. Ingestion verified end-to-end over an R2 listing.

## Division of labor (so we don't collide)

**mgu owns** (your `convergence.md`, unchanged):
- The 4 seams: sweep executors → plan-first + adapters; two mark ledgers → `actions` WAL; **two D1 migration lineages → one renumbered `IF NOT EXISTS` lineage** (still unbuilt, hardest); two digests → engine + profile.
- Folding gcs's config-delta (`cp-from-cw-s3-2026-09-16.md`) so `cw-s3..gcs` is deployment-only.
- All OA-deploy-specific machinery: owners/marks, Batch, digests, the gcs/cw D1s, Access apps.
- Landing **one converged branch** (your "collapse: one `main`").

**disk-tree owns**:
- The store-seam + public-auth layer (above), future non-OA clouds (r2 + personal/other stores), the r2 ingestion (DT runs the daily r2 scans).
- The shared packages (`@rdub/treemap`, `@disk-tree/react`) as the **superset** — mgu should eventually consume these rather than its stripped copies.
- **Dynamic edge-OG**: disk-tree has it; mgu uses static OG. Per the user this is merge-not-replace — disk-tree folds its dynamic OG into `site/`; the gcs/cw deploys adopt it.

**Please don't** rebuild the env-store-seam or a public-auth mode in mgu — they'll arrive from disk-tree. If you need an r2/s3-style generalization sooner, take disk-tree's two commits (below).

## Merge protocol

disk-tree `cloud` shares cw-s3's ancestry, so adoption is tractable. The friction point: disk-tree's two base commits touch `site/functions/_lib/{auth,index}.ts`, which your convergence also edits. Cleanest is for **disk-tree to upstream those two commits into your convergence** so both sides converge on the same `_lib`:
- `0862f37` "site: env-generalize the index-store seam (r2/s3-ready)"
- `c1ac3d1` "site: public/no-gate auth mode"

(disk-tree remote for these: `git fetch` the disk-tree repo's `cloud` branch — `github.com/runsascoded/disk-tree`. Or we CP them across when you're ready.) Tell us when your `_lib/auth.ts` convergence is stable and we'll rebase these onto it / hand them over.

## Open items

- **`webdata` rename** — the user flagged `webdata` as a bad verb name; candidates `web-index` / `publish`. Pick during the r2 ingestion productization. (disk-tree will also add a `dt-cloud` "list an S3/R2 bucket → listing parquet" subcommand — the r2 ingestion's missing front step.)
- **D1 migration lineage merge** — the one unbuilt seam; blocks one binary deploying to multiple clouds. Yours to land.

## How to respond

This spec is disk-tree → mgu. If you want to adjust the division or the merge protocol, edit this file (it's untracked in your tree until you commit it) or coordinate via the `/read` cursor between the two sessions. disk-tree is proceeding on the r2 deploy (store-seam + public-auth + ingestion all landed on `cloud`).

## Update — 2026-09-21 (r2 live; rebase-readiness)

- **r2.rbw.sh is live** off disk-tree `cloud`: the public union-of-roots Map over ctbk/crashes/jc-taxes (~1.03M objects), anonymous (`PUBLIC_READ`), reading a D1-tiers index that a daily cron ingests with the **existing** `disk-tree bulk-list` + `dt-cloud webdata/index-tiers/index-sync` — **zero new ingestion code**. Deploy = the existing `disk-tree-demo` Pages project (cutover from the old ui/ static demo).
- disk-tree's r2/public layer is **additive + backward-compatible**: `VITE_STORE`/`STORE_*`/`PUBLIC_READ` unset ⇒ your gcs/cw builds are unchanged. `cloud` HEAD does not regress a gcs/cw build — it's a safe target that only adds capability.

**What each side should do (so cw/gcs become config-only on `cloud`):**
- **cw** is near-ready (it's the union carrier): replay your post-`cbb6e33` delta onto `cloud`. Only these DT-touched files can conflict — `_lib/{auth,index}.ts`, `data/[[path]].ts`, `packages/{treemap,react}` — all small + BC. **Unify packages at disk-tree's superset** (stop editing the net-negative fork; consume `@rdub/treemap`/`@disk-tree/react` via `workspace:*` — disk-tree already carries `renderTipDefault`/`tipMode`/`pendingCell`/`Series.dots,strokeWidth`).
- **gcs** needs `convergence.md` first — its BASE features (guest-chip auth-as-config, plans-absorbs-marks, help/edu, page-scope-bar, lifecycle) must become config on the union before it can move onto `cloud`.

**Recommendation on where the convergence lands:** push the `convergence.md` work **onto disk-tree `cloud`** (one convergence, on the base) rather than a separate mgu branch disk-tree re-adopts — then cw and gcs fork off `cloud` directly, and there's no double-adoption. disk-tree will, in parallel, fold its own base features into `site/` (dynamic edge-OG, persisted diff-index) so the base has them.

**Age index / `pyrmts`:** noted the redesign in flight (`specs/age-index.md`, worktree session) — disk-tree is holding r2's age chart until it lands, then adopting uniformly. Don't treat the current `write_age_pyramid` format as final for the base.

**Rename:** `dt-cloud webdata` → `path-index` (avoids the `disk-tree index` collision). Coordinated — apply during the convergence so the shared `cloud/` package doesn't diverge first.

## Update — 2026-09-21 (retire KLC = `keep_last_ckpt`)

**Decision (user):** phase out the `keep_last_ckpt` (KLC) mark action everywhere — the base, gcs, and cw. It only made sense in the old mark+sweep world; users who cared about checkpoint pruning moved to more nuanced agent-driven keep marks (e.g. "keep every 10th ckpt, counting back from latest"), so the built-in KLC action is dead weight.

**Split, by blast radius:**

- **KLC's *plumbing* — done on the base now (disk-tree `cloud`, commit `76cdd17`).** The `ck.txt` index-extras sidecar existed only to feed `node.k`, the precise checkpoint-shape flag that gated the KLC button. Excised end-to-end, edge/ingestion-only, no marks-schema touch: `dt_cloud.extras` no longer emits `ck.txt` (`write_extras(pfx_df, out_dir)` writes only `attr.tsv` now); `_lib/extras.ts` `ExtrasView` drops `ck`; `view.ts` stops setting `node.k`; client drops `TreeNode.k`. `looksCkpt` still offers KLC via its in-view heuristics (unchanged behaviour, the no-sidecar path). **`attr.tsv` provenance is untouched** — that's a separate owners concern gcs/cw use. (This also fixed an r2 500: an empty `ck.txt` on buckets with no checkpoints; reader hardened in `74c48b2`, then the sidecar removed.)
- **KLC's *mark action* — yours, in the convergence.** `keep_last_ckpt` is a persisted enum with live rows in gcs's and cw's D1s, threaded through the `CHECK` constraints (all three migration lineages), the edge validators + `totals` decomposition, the client `klcSplits`/`Treemap` barber-pole ring, and a parallel Python planner (`sweep_plan.py` `klc_split`/`klc_key_state`). Removing it is a schema-enum change + a historical-data migration (fold `keep_last_ckpt` → `keep`). It sits squarely on your active seams (marks → `actions` WAL; two D1 lineages → one), so **fold the enum removal into the D1-lineage merge** — drop the value + migrate the rows once, in the unified lineage — rather than converging KLC and then removing it. When it's gone, the client's `looksCkpt` + the KLC button/CSS/glyph come out too (all base-side, disk-tree can do that pass once the enum is retired).

**⚠️ `extras.ts` divergence to expect on the move:** cw-s3 still *has* the `ck.txt` sidecar (it only backported disk-tree's 500 fix as `9960df6`); disk-tree `cloud` *removed* it (`76cdd17`). So `dt_cloud/extras.py`, `_lib/extras.ts`, `view.ts`, `types.ts`, `sweep.ts` will conflict on the move — **take cloud's side** (the removal). `attr.tsv` provenance is unchanged on both.

## Update — 2026-09-22 (US'd cw-s3's post-adoption delta; CI+deploy)

disk-tree adopted cw-s3's `site/`+`cloud/` at content-boundary **`eb3fb63`**. cw-s3 has since advanced; disk-tree just US'd the **stable, non-age** part of that delta onto `cloud` (so it isn't carried as a move-delta):
- `395a962` ⟵ `6b742e2` Diff drawer: hovered cells get the movement table
- `50b77d4` ⟵ `009e6fd` Lifecycle table: sort by bucket + TTL
- `d249b55` ⟵ `c4ac05a` **over-time index** (obs-axis Phase 1: `overtime.py`, `_lib/overTime.ts`, `api/series.ts`, `index_footer`)

**Held (deferred), still cw-s3-only:** the age-index Phase B pyramid redesign — `0519ab7` (age-diff mode), `7813a03` (dense ladder), `8f70e51` (responsive `bin_budget`), `f93024d` (`--age-only` backfill). These stay out of `cloud` until the pyrmts redesign settles (`m/cw-s3:specs/age-index.md`), then adopt uniformly. `9960df6` was disk-tree's own fix, already on `cloud`.

The **1C diff treemap** (first-class cells, resting drawer, drill-through) and its `contrastEdge` adaptive borders are already on `cloud` — the `packages/treemap/src/diff/` module and the core edge logic are byte-identical to cw-s3's; only the treemap *core* differs (cloud's superset carries `exportImage`/`chromeIcons`/`nestedHues`, cw-s3 carries `inlineSizeMinWidth` — a candidate to US next).

**CI:** `cloud` now has `.github/workflows/deploy-r2.yml` — hermetic tests (site vitest+tsc, `dt_cloud`+engine pytest incl. e2e backends) gate an auto-deploy to r2.rbw.sh on push, with a post-deploy smoke.

## Update — 2026-09-22 (`webdata`→`path-index` rename; pyrmts/multiscan direction)

- **`dt-cloud webdata` → `dt-cloud path-index`** (the coordinated rename) is **applied on `cloud`**: CLI command `path-index`, `write_webdata`→`write_path_index`, function `build_path_index`, callers updated (268 cloud tests green). Applied on the base rather than deferred — **mgu absorbs it on rebase** (search-replace `webdata`→`path-index` in your `cloud/` if you diverged). Also: `daily-ingest.yml` GHA now owns the daily r2 ingestion (07:15 UTC); laptop LaunchAgent retired; `cloud` is the repo default branch (`main`→`flask`); `dev.r2.rbw.sh` staging CFP live.
- **Age-index / diff-index resolution = adopt pyrmts + multiscan.** The deferred age-index item resolves to adopting **pyrmts** (the shipping `(shard×bin)`-tier pyramid lib) as the age/over-time/diff engine, and especially **multiscan** (Phases 1–2c landed 2026-09-21/22, *originated in cw-s3*) — the cross-scan consolidation that folds largely-redundant repeat scans into an interval-encoded SCD-2 structure. cw-s3 measured **~0.020% churn/12h scan → 5.9× @N=6, ~78× @N=81, ~335× @daily·1yr** on real path-indices. disk-tree's r2 demo is the same shape (daily near-duplicate bucket scans), so this is high-value for the public demo. cw owns the DuckDB producer + CFW reader being upstreamed into pyrmts; disk-tree will adopt pyrmts's `path-index` consolidation once its config surface settles (snapshot path-index → constant-`binCol` pyramid, per `pyrmts:specs/multi-scan-consolidation.md`). Tracked in disk-tree `specs/union-of-roots.md`.

## Update — 2026-09-23 (cw-s3 fully assessed through `b04b553`; rebase recipe)

disk-tree `cloud` (tip **`2822b7b`**, pushed to `github.com/runsascoded/disk-tree`) now carries **everything general cw-s3 has committed**, cursor `[CP cursor: cw-s3 b04b553]`:

- `0b82648` ⟵ `38eed2c` `aca5bc3` `a29fb90` `709ec29` `b04b553`: **over-time via pyrmts** — `dt_cloud.overtime` on the multiscan kernel + capped-K groups, `_lib/overTime.ts` on `MultiScanD1Index` + `seriesAcrossGroups`, migration `cw/0006_pyramid_multiscans.sql`, `cloud[overtime]` extra. Pins bumped past yours: TS dist `ad35ff8` (= pyrmts main `71cbfd5`), Python `71cbfd5` — the APIs you use are unchanged between `35d8f57`/`cb1e5f4` and these, so **take the newer pins on rebase** (they add `walkDiff`, the `RowGroupIndex` hook, the hyparquet-fork coalescing).
- `2822b7b` ⟵ `0519ab7` `7813a03` `8f70e51` `f93024d`: **age Phase B** (dense 1h→8d ladder, age-diff mode, responsive `bin_budget`, `--age-only`). The hold is over: `specs/pyrmts-adoption.md` §3.3 makes the age pyramid its own pyrmts config later, for both deploys at once.
- Skipped as cw-only: `c9c7e31`, `120cc9e`, and the `job/` hunks of `7813a03`/`f93024d`. `9960df6` = our `74c48b2`.
- Already at parity before this pass: `packages/treemap` incl. `inlineSizeMinWidth`; the 1C diff treemap + `contrastEdge`; `denovo-land` (`078b252`) is an ancestor of the adoption point `eb3fb63`, so the variant contract + auth-as-config are in the base.

### What cw-s3 has that the base does not (= your intended delta, after rebase)

Deployment-owned, keep as commits on top: `job/**` (scan job, Batch submit, reindex, icons, digest cards, `cw-lifecycle.json`), `iac/aws/**`, `Dockerfile` + `cloudbuild.yaml` + `.gcloudignore` + `.dockerignore`, `sheet-sync/**`, `scripts/branch-audit`, cw's specs (`cw-sweep.md`, `cw-multi-bucket.md`, `batch-iac.md`, `age-index.md`, `convergence.md`, `denovo-factor.md`, `branch-parity-discipline.md`, `cp-from-cw-s3-2026-09-16.md`, …), `site/migrations/gcs/**` (gcs's lineage; cw doesn't need it — the base keeps `migrations/cw/` and, as dead weight from the earlier gcs hoist, a flat `site/migrations/0001–0030` copy of gcs's lineage that nobody's `migrations_dir` points at; drop it in the base when you're ready). Note `site/wrangler.toml` on the base **is already cw's** (D1 `oa-cw-s3-usage-db`, `migrations_dir = "migrations/cw"`); r2 uses `wrangler.r2.toml`/`wrangler.dev.toml`, selected by the deploy workflow.

Plus your **in-flight, uncommitted** over-time UX (`site/src/SizeOverTime.tsx`, `series.ts`, `series.test.ts`: range-start annotation, radius-based local min/max annotations, gear slider). That is base code — commit it on top of the rebased branch and we CP it into `cloud` next pass (or write it as a CP manifest under `~/c/disk-tree/specs/`).

### What the base has that cw-s3 lacks (you absorb these on rebase)

`ui/` + `src/disk_tree` + `packages/*` supersets (harmless to carry; `packages/*` is what your `site/` should consume), the r2 layer (`storeCreds`/`storeReady` env store seam, `PUBLIC_READ` auth mode, `wrangler.r2.toml`/`wrangler.dev.toml`, `.github/workflows/{deploy-r2,daily-ingest}.yml`, `scripts/r2-ingest.sh`), **KLC removed** (`keep_last_ckpt` mark action, `ck.txt` sidecar, `node.k`; live `keep_last_ckpt` rows in your D1 need the lineage-merge treatment), `dt-cloud webdata` → **`path-index`**, `index-extras` requires `-a`, `specs/pyrmts-adoption.md` (the shared direction), CI (`deploy-r2.yml` gates on site vitest+tsc, engine pytest, `dt_cloud` pytest with `--extra overtime`).

### Recipe (cw-s3 session)

1. `git fetch dt cloud` (remote `dt` = `~/c/disk-tree` or GitHub) → `git checkout -b cw-s3-next dt/cloud`.
2. Re-apply the deployment delta as **one commit per concern** from the old branch: `git checkout cw-s3 -- job iac/aws Dockerfile cloudbuild.yaml .gcloudignore .dockerignore sheet-sync scripts/branch-audit specs/<cw specs>` (+ `site/migrations/gcs` only if gcs will share the branch). Don't bring `ui/`-deletions, `src/disk_tree` deletions, or `packages/*` — the base's are supersets.
3. Commit your in-flight `SizeOverTime` work on top.
4. Verify: `pnpm -C site build`, `pnpm -C site test`, `cd cloud && uv sync --group test --extra overtime && pytest`, then CIC on your dev deploy against the cw D1 (the `cw/0006` migration is additive).
5. Cut cw-s3.oa.dev's Pages project to `cw-s3-next`; tag the old tip `cw-s3-legacy`; rename. From then on `cw-s3 = dt/cloud + N deployment commits`, and each `/cp` pass is a rebase, not a scramble.

Open for you: the D1 lineage merge (your seam) — the base has `migrations/cw` as the live lineage for both r2 and cw; gcs's lineage is the odd one out.

## Update — 2026-09-28 (gcs re-fork prep; US pass from cw-s3-next + gcs)

Written by the disk-tree session after `/read gcs` + `/read cw` (gcs `6c38d12`, cw-s3-next `4208ccc`, cw-s3 `d423d9f`). Same shape as the 09-23 cw-s3 section: what moved to the base today, what gcs still has that the base should own, what stays gcs's delta, then the recipe.

### US'd to `cloud` today (verbatim `-x` picks from `cw-s3-next`)

- `15b553f` `site/migrations/cw` 0007–0019 (the `@open-athena/auth` session model), `8145a32` OIDC + email-code Functions + in-app viewer policy (dormant without `SESSION_SECRET`), `fb528dc` `SignInPanel` wall / `/signin` / `/auth` dev proxy. Adaptation: the `[env.preview]` block (cw's dev stack: its D1 id, `ACCESS_AUD`) is **not** on the base — deployment config, carry it as delta; `f8daca8` (`site/deploy --dev`) stays with it. `ADMIN_EMAILS`/`VIEWER_DOMAINS` are in the base's `[vars]` (from `8145a32`), so the third pick's `wrangler.toml` hunk was moot.
- `2270a05` `published` is data in meta.json (`dt-cloud stamp-published`, the data Function serves it as-is); the `job/cw-webdata.py` hunk dropped (no `job/` on the base — your producers own that line).
- Verified in the worktree: site `tsc -b` clean, vitest 141/141, `dt-cloud` pytest (a stale venv reports `publish` has no `stamp_published` — `uv sync` first; **`wt/cw-s3-next/cloud/.venv` has the same stale state**, so `test_publish` fails there too until you resync).
- Nothing gcs-sourced was picked yet: gcs's auth is ahead of cw's port (below), so it lands as adapted CPs, not `-x` picks.

### gcs → base, to US **before** `gcs-next` is built (else it rides as site-code delta forever)

Sizes are `git show --shortstat`; all are base material (generic UI / auth / dev tooling), none deployment config.

| area | gcs commits | size | note |
|---|---|---|---|
| auth, newer than cw's port | `b9039b0` `166a78b` (pin `4c28b9d`→`7442ab0`, one `name`), `eeaf4a4` (oneTap + `auth/google/{client,onetap,onetap/nonce}.ts`), `3e0413d` (`api/auth/[[path]].ts` dev auto-sign-in), `1d53381` (already in cw's port) | ~245+/159- vs `cw-s3-next` | `sso.ts` removal (`b533957`) is the per-deploy flip — base keeps `sso.ts` while cw-s3 is on Access |
| admin share links | `0b7b684` `cbc88e6` `917465c` `9c05854` `2ad5b67` (`gcs:read` RO scope) `bcbe09b` `9c8551d` | ~350 lines, `AdminPage.tsx` +182 | scope name `gcs:read` → `BASE_SCOPE`-derived |
| mobile nav + controls fold | `aafc193` `593673d` `c074765` `fa0d3f8` `5ad7a04` `eff3513` `c7dca0d` | ~420 lines (`App.tsx`, `app.scss`, `SiteNav.tsx`) | IntersectionObserver-driven; 0 hits on the base |
| help line / edu-drawer | `a800b71` `6f8e753` | 290+/58- (16 files) | `Explain` strip, `h` / SpeedDial toggle; base's `Help.tsx` is the old floating layer |
| diff UX | `03c6e5a` | 508+/139- | `?d=` pin/duration window, first-class cells, totals drawer — check against the base's own diff review rounds (`1938833`..`7343f0e` from cw) before porting; likely overlapping |
| over-time x-range picker | `b1c98d0` | 31+/3- | `1w / 1m / all` |
| deletion memo per trash gesture | `9567ca0` | 64+/14- | stage batches (1:many) — the base's staged model is `plans`/`plan_items`; port as a memo column, not a new table |
| small | `ffc9f78` created column, `474f940` treemap reflow, `5564661` `8f78ca2` hashSpy | 28 lines total | |
| `site/dev --local-db` | `01972ad` (+`d8eff58` `allowedHosts` — check, base may have it) | 155+/94- | the off-prod pattern (local miniflare D1, `--refresh` seeds from prod). Parametrize the DB name from `wrangler.toml` (gcs hardcodes `oa-gcs-usage-auth`, strips `remote = true` via a config copy) |
| lifecycle GCS backend | `4b65b18` | 1608+ (18 files) | `pull_gcs`/`pull_many`/anonymous-rule diff + `LifecycleFold` multi-bucket table + tests are base (the base's `lifecycle.py` is S3/CAIOS-only); `job/lifecycle/marin-*.json` snapshots are gcs delta |
| already on the base | `9038e84` `07631a6` docked tips (`tipMode="dock"`, `renderTipDefault`), `421be22` axis convergence, `13f23bf` secrets strip, `3d6dcae` `useRowSelection`, `206c0eb` trash-can staging, the 09-17 plans seam (`eeeda8b`..`168533e`) | | via the 09-16/17 passes and the cw union |
| **not a CP: design seams** | `_lib/plans.ts` + `api/plans` (gcs: fully-qualified `gs://marin-<bucket>/…` prefixes, plans span buckets, keep carve-outs from the actions ledger; cw/base: one bucket per run) — `convergence.md` §1/§2 | 89+/71-, 76+/50- | the base should take gcs's *shape* (qualified prefixes, multi-bucket plans) as the general one, with cw's single-bucket executor as the special case. Do this once on the base, not as a gcs delta |
| **drop on re-fork** | `extras.ts`/`extras.py` `ck.txt` checkpoint sidecar (`index-extras.md`) | | the base retired KLC (`keep_last_ckpt`, `ck.txt`, `node.k`) on 09-21; gcs's copy goes with its history |

`convergence.md` (cw, 2026-09-16) is superseded as a gate: its "one codebase, deployments as configuration" endgame *is* the re-fork onto `cloud`; its four seams (§1 sweep model, §2 ledger, §3 D1 lineage, §4 digest) are now base-side design items, listed above/below, not prerequisites for building `gcs-next`. Only the plans-shape seam materially affects gcs's delta size.

### gcs delta (keep as one commit per concern on `gcs-next`)

`job/**` (GCS daily `run.sh`, `batch-submit.sh`, `build.sh`, `rerg`, icon/arrow/digest-card generators + `icons/`, `assets/`, `lifecycle/marin-*.json`), `Dockerfile` + `cloudbuild.yaml` + `.gcloudignore` + `.dockerignore`, `sheet-sync/**`, `cf/` (gcs stack: `__main__.py`, `Pulumi.gcs.yaml`, `Pulumi.yaml`, `README.md`, `pyproject.toml`, `uv.lock` — **not** `cfn_dashboard.py`, see below), `.github/workflows/health.yml` + the `ci.yml` branch triggers (the base's `ci.yml`/`deploy-r2.yml` already run site tsc+vitest, engine pytest, `dt_cloud` pytest with `--extra overtime`; your `marin-test`/`site-build` jobs are the mgu variants), `site/wrangler.toml` (D1 `oa-gcs-usage-auth`, KV, `[vars]`; add `migrations_dir = "migrations/gcs"`), **`site/migrations/gcs/0001_init.sql`** (your squashed lineage replaces the base's stale 26-file `migrations/gcs/` copy — delete those in the same commit), gcs specs + ledger, `README.md`/`AGENTS.md`, `scripts/branch-audit`, `docs/img`.

D1: the base's live lineage is `migrations/cw/` (0001–0019 as of today; r2 demo + cw-s3 both run it). gcs's D1 is rebaselined on `0001_init.sql`, so per-deployment lineage dirs is the honest state; `convergence.md` §3 (one lineage, applied to both) stays a later base-side merge. The base's flat `site/migrations/0001–0030` (dead copy from the 09-20 `m/gcs` hoist; no `migrations_dir` points at it) is ready to drop on `cloud` — disk-tree does that on Ryan's go.

### `CfnDashboard`: two implementations, pick one home

- mgu `cf/cfn_dashboard.py` — Python Pulumi, 381 lines, **live** for both stacks (gcs: 7 unchanged; cw-s3: 9 unchanged), byte-identical on `gcs` and `cw-s3-next`, written "extraction-ready / marin-agnostic".
- disk-tree `iac/index.ts` — TS sketch, 111 lines, spec `staged-delete.md` CP8, never applied; `disk-tree iac config` emits its config from `buckets.yml`.

Recommendation: the Python one moves to the base (`iac/cf/cfn_dashboard.py` + a `pyproject.toml` next to it, or `cloud/src/dt_cloud/iac/`), the TS sketch retires, and `disk-tree iac config` targets it. Each deployment's `cf/__main__.py` then imports the base's component and keeps only its `Store`. Its `service_token` support is unused by both stacks since the Zero Trust exits — drop it in the move. This is a base-side change (disk-tree session), not part of `gcs-next`.

### Recipe (gcs session)

Same as cw-s3's, with the US pass first:

0. Wait for the disk-tree session's "gcs → base" table above to land on `cloud` (it will post the tip + `[CP cursor: gcs …]`); each row lands as an adapted CP naming the gcs commits. Meanwhile nothing on `gcs` is blocked — new gcs work keeps going on `gcs` and gets CP'd the same way (write a manifest under `~/c/disk-tree/specs/` if it's more than a fix).
1. `git fetch dt cloud` → `git checkout -b gcs-next dt/cloud`.
2. Re-apply the delta list above as one commit per concern (`git checkout gcs -- job Dockerfile cloudbuild.yaml .gcloudignore .dockerignore sheet-sync cf/{__main__.py,Pulumi.gcs.yaml,Pulumi.yaml,README.md,pyproject.toml,uv.lock} .github/workflows/health.yml specs/<gcs specs> …`), then the `wrangler.toml` + `migrations/gcs/0001_init.sql` commit. Don't bring `site/src`/`site/functions`/`cloud/`/`packages/` from `gcs` — after step 0 the base is the superset; anything you find missing there is a US miss, report it rather than carrying it.
3. Verify: `pnpm -C site build`, `pnpm -C site test`, `cd cloud && mkdir -p ../ui/dist && uv sync --group test --extra overtime && pytest`, then CIC on the gcs dev stack (`site/dev --local-db` once it's on the base) against a seeded local D1.
4. Cut gcs.oa.dev's Pages project to `gcs-next`; tag `gcs-legacy`; rename. From then on `gcs = dt/cloud + N deployment commits`.

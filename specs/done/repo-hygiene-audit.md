# Repo hygiene audit — 2026-08-28

Point-in-time audit of tests, CI, docs, pipeline health, and layout, taken while Percy nags his students (feature freeze window). Items are ranked; each checkbox is either fixed-in-place (commit noted) or a proposed follow-up spec.

## 0. Live incidents found during the audit

- [ ] **Daily snapshot FAILED 2026-08-28** (07:00 UTC cron run): `webdata` was kernel-OOM-killed at RSS 98.5 GB. Root cause: the Cloud Scheduler body still carries `DUCKDB_MEM=100GB` and lacks `DUCKDB_MEM_ACCESS` — `job/batch-submit.sh` was already fixed to 90GB ("100GB inside 117GiB got kernel-OOM-killed twice") but the cron body never got the fix. Classic body drift (same failure class as the 8/10 `LISTING_MODE` regression). Fix: patch the decoded body env in place (never regenerate), and re-run today via `job/batch-submit.sh` (8/28 listings completed; `webdata` re-runs and publishes).
- [ ] **CoreWeave 6-hourly cron crash-looping** since the first `gcs`-branch image rebuild after `053cc33` (which pruned `job/cw-*` "to the `cw-s3` branch" — but they were never added there; they exist only in git history). Every run since ~8/26 fails with `job/cw-run.sh: No such file or directory`. Mitigation: pin the CW scheduler's image to the last pre-prune digest. Real fix: restore `job/cw-*` onto `cw-s3` (`git checkout 053cc33^ -- job/cw-*`), build a `:cw` image tag from that branch, point the cron at it.
- Neither failure alerted anyone (see §1): the Slack digest is a success-path-only final step.

## 1. Pipeline health (proposed spec: `pipeline-alerting-and-health.md`)

- **No failure alerting.** `gcs-usage alert` runs last and only on success; a hard failure posts nothing — silence in #gcs-usage is the only signal, and `maxRetryCount: 0` means no retry. Cheapest fix: an `ERR`/`EXIT` trap in `job/run.sh` posting a failure message via the already-wired `SLACK_BOT_TOKEN`/`SLACK_CHANNEL`.
- **Publish is non-atomic and unverified.** Step 6 is a bare `cp *.json` into the live store; `data/[[path]].ts` lists any date-shaped dir, so a half-written snapshot appears in the UI scan picker. Fix: stage to `$SNAP_PATH.tmp/`, verify `meta.json` parses with the expected `asof`, then rename; post-publish re-read as the success criterion.
- **No `/health` page — but every input already exists.** A `site/functions/api/health.ts` can compose: `scans.json` newest date + `meta.json.published` (snapshot age), `meta.json.access.{from,to}` (ingest recency), `HEAD /api/path-index` (index presence), a D1 `SELECT count(*)` (marks/claims + reachability), and `series.json` `fate`-key presence. Plus a tiny `/health` route rendering it. A cron (or the daily job's trap) can curl it and alert on staleness — that closes the "job silently didn't run" hole.
- **`GCS_USAGE_TOKEN` is never provided in prod** — `batch-submit.sh` doesn't declare or pass it, so `series.json` lacks `fate` on every scheduled run and the To-do burn-down silently renders empty. Blocked on the agent-token decision (must NOT reuse/rotate Ryan's personal token; mint a dedicated service token via `/api/token` once applied to prod D1).
- **Image dependency resolution is unpinned**: the Dockerfile installs `pip install ./marin`, ignoring `marin/uv.lock` — every rebuild re-rolls deps. Switch to `uv pip install --locked` or export a constraints file.
- `gcs-usage rules` failure is swallowed (`|| true`, no WARN) — `rules.json` can go stale silently.

## 2. Tests + CI (proposed spec: `ci-and-test-coverage.md`)

Four test roots, no single runner; the highest-stakes code has zero tests:

- **CI never runs `marin/tests/`** (73 tests — the actual product). Blocker discovered while verifying: `marin` imports `disk_tree` at runtime (`viz.py:211`, tree_build sync) but doesn't declare it — 4 `marin/tests/test_viz.py` tests fail on a fresh `uv sync`. Declare the dep (path/workspace source) first, then add the CI job.
- **CI's mypy step is a masked no-op**: exits 2 in 0.3s ("not a valid Python package name") under `continue-on-error: true` — has typechecked nothing since ≥8/24.
- **`site/` appears in no CI job** — not built, not typechecked, not tested. Worse, `site/tsconfig.json` includes only `src`, so `site/functions/` (~1.9k lines: auth gating, actions ledger, subtree/path-index serving) is never typechecked anywhere, even locally. Add a `tsconfig` for functions + a CI job (`pnpm -C site build` + `tsc -p functions`).
- **`site/src/sweep.ts` + `marks.ts` (~580 lines of pure fate/mark-resolution logic) are untested** — `klcSplits`, `allUserFates`, `subtreeFateTotals`, `applyTodoFilter`, `newer()`. Pure functions over plain data: prime vitest targets, and the sweep executor will lean on exactly these. Highest-value new tests in the repo.
- `gcs-usage series` / `report` / `access *` and `job/run.sh` branching (NOP_IF_PUBLISHED / REPROC / ACCESS_ONLY) are untested; `access.py` (451 L) has no tests.
- Release workflows are stale (pnpm 9 vs 10, `ui/pnpm-lock.yaml` path that no longer exists, hardcoded wheel name).
- Assertion hygiene: 15 banned substring asserts, all in root `tests/` (disk-tree legacy); `marin/tests/` is clean.
- No lint runs anywhere (no ruff config; `pnpm lint` in no workflow).

## 3. Docs (proposed spec: `docs-refresh.md`)

- [x] **README published Percy's two personal emails** + described the pre-cutover per-email CF policy — replaced with the D1-allowlist model (fixed in this commit).
- [x] **`gcs-usage report` was defined twice in `cli.py`** — the mark-status CSV (line ~1450) silently shadowed the documented listing×attribution report (line ~184). Renamed the older one `attr-report`; README updated (fixed in this commit).
- **`CLAUDE.md` is ~85% wrong for this repo** — it's verbatim upstream disk-tree's (Flask :5001, `disk-tree` CLI, `ui/`); it never mentions `gcs-usage`, `site/`, `job/`, marks/claims, Cloudflare, or the public-repo constraint, while the accurate doc (AGENTS.md) isn't referenced. Rewrite as a short layout map + pointer to AGENTS.md/README, with disk-tree engine notes scoped to `src/disk_tree` + `ui/`.
- AGENTS.md endpoint table covers ~8 of ~15 routes (missing `/api/marks`, `/api/claims`, `/api/db/*`, `/auth/sso`, `/users`, `/user/:id`, `/v1/files/*`).
- No written explainer for marks/claims/fates semantics (recency-beats-specificity, KLC decomposition), the attribution join rule, the `gs://oa-gcs-usage-dvx` layout, or `site/dev`/`refresh-db`/`deploy` local workflow. `packages/react/README.md` is the stock Vite template; `ui/README.md` documents the non-deployed app.
- Spec housekeeping: ~15 tracked specs in `specs/` are fully landed but never moved to `done/` (`actions-ledger`, `mark-sweep-ui`, `adaptive-listing`, `streaming-aggregation`, `diff-and-search`, `dir-agg-cache`, `path-index-lazy-drill`, `selection-actions`, `scan-browser`, `personal-sync`, `size-over-time`, `aggregation-extensions`, `gcs-backend-and-snapshot-diff`, + delete the self-declared-superseded `external-listings-and-gcs.md`). `done/` currently holds only engine specs — every shipped product spec is still in the root.

## 4. Layout + hygiene

- [x] `.gcloudignore`: added the local secrets file, `site/.dev.vars`, and `dscrd` (was uploading the secrets file to the Cloud Build staging bucket on every image build) (fixed in this commit).
- **No root `.gitignore`** — outside contributors (public repo) get zero protection; locally, `.tmp/` (1.1 GB DuckDB spill) is unignored even by the global excludes file. Decide: add a minimal root `.gitignore` (mirroring `.gcloudignore`) vs. keep the global-excludes convention.
- Untracked-but-load-bearing: `job/icons/_headers` (CORS headers for the icons Pages project — exists nowhere else), `site/screenshots/` (OG flow), `job/icons/arrows/` (deployed Slack emoji set). Track them.
- Dead/stray: `site/Dockerfile` + `site/nginx.conf` + `site/.gcloudignore` (abandoned nginx-on-Cloud-Run path, superseded by CF Pages — delete), the stray billing CSV at repo root (move to `tmp/`), root `__init__.py` + `extra-mp4s.py` (disk-tree vestiges), empty `wt/`.
- Fresh-clone hazards: `@open-athena/auth` is a private-repo git dep (install fails without org creds — also in the Docker build path); `site/.pds.json` may be resolving `scrns`/`@rdub/file-tree` from local checkouts (local green ≠ pinned-SHA green); `packageManager` drift (root pnpm@10.19.0 vs site 10.18.1); CI node 20 vs Docker node 22.

## 5. Ranked next specs

1. **`sweep-executor.md`** — still the must-do: dry-run manifest + deletion tool over live sweep marks (klcSplits provides the KLC expansion). Nothing can actually delete yet.
2. **`pipeline-alerting-and-health.md`** (§1) — failure trap + atomic publish + `/api/health` + staleness alarm. Today's silent double-failure is the motivating incident.
3. **`ci-and-test-coverage.md`** (§2) — marin dep fix + CI job, functions typecheck, sweep.ts/marks.ts unit tests. The sweep executor should not ship on untested fate resolution.
4. **`docs-refresh.md`** (§3) — CLAUDE.md rewrite, AGENTS.md endpoint completion, marks/fates explainer, spec housekeeping sweep.
5. `lens-aware-time-series.md` part 2 (`/api/age`) — already specced, restores histograms under lenses.
6. CW re-platform: restore `job/cw-*` on `cw-s3` + `:cw` image (incident follow-up, §0).

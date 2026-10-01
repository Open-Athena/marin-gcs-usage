# cw-s3 — agent notes

This branch is the cw-s3.oa.dev deployment: the shared `cloud` base plus the CoreWeave store. These notes cover what is cw-s3's own; the shared engine, `dt-cloud` and viewer guide is `cloud`'s `CLAUDE.md` (`git show refs/heads/cloud:CLAUDE.md`).

The repo is **public**: no emails or other PII in tracked files or commit messages (name + GitHub handle at most). Sizes stay behind the site's auth.

## Branches

- `cw-s3` merges the local `cloud` branch (`git merge refs/heads/cloud`; bare `cloud` is ambiguous with the `cloud/` dir). Never rebase onto it.
- Base-worthy changes made here are tagged `[base]` and cherry-picked up to `cloud` by the root session; this branch keeps only what the cw-s3 deployment runs.
- Migrations: one lineage, `site/migrations/cw/`. Never rename an applied migration; a migration `cloud` drops (or another branch adds) only needs its file to match what cw prod D1 has applied (`d1_migrations` records file names).

## Layout (cw-s3's own)

- `job/cw-*`: the scan job (`cw-run.sh`, submitted by `cw-batch-submit.sh`; `PIN=1 DRY=1` prints the cron body) and its one-shots (`cw-reindex*`, `cw-overtime*`, `cw-meta-*`, `cw-publish-submit.sh`, `cw-recompress-submit.sh`). `job/build.sh` builds the `:cw` image (`JOB=cw-run.sh`). `job/batch-submit.sh` is gcs's submitter, kept until the ops scheduler program takes a checkout per cron.
- `job/icons/arrows/` + `gen-{arrow-avatars,delta-arrows}.py`: the digest's trend-arrow avatars (hosted on `gcs-usage-icons.pages.dev`); `job/icons-cw/` is where `cw-digest` renders OP plots before deploying them to that project's `cw` branch. `job/slack/`: the CoreWeave Usage Bot manifest.
- `cloud/src/dt_cloud/cw_digest*.py`: the monthly Slack thread (`dt-cloud cw-digest -m YYYY-MM -c <channel>`, `-n` dry run).
- `site/wrangler.toml`: the cw store's config (`STORE = "cw"`, `AUTH_MODE = "app"`, `STORE_BUCKETS`, `SNAPSHOTS_SUBDIR = "cw"`, the meta store, the plan-sweep executor). `[env.preview]` is the dev stack.
- `cf/`: Pulumi for the `oa-cw-s3-usage` Pages project, domain, D1, KV and R2 (stack `cw-s3`).

## Dev workflow

- `site/dev`: Vite on **:3263** + `wrangler pages dev` on **:3264** (`devPort` in `site/package.json`; `./dev --refresh` seeds a local D1 from a prod export).
- `site/deploy` (run from `site/`): manual deploy to cw-s3.oa.dev, then pushes the branch + `cw-s3-prod` to `o` (`-P` skips the push). `--dev` deploys the preview branch (dev.cw-s3.oa.dev; prod D1). `--status` compares deployed vs HEAD.
- D1: `wrangler d1 migrations apply oa-cw-s3-usage-db --remote` (separate from deploy; needs `CLOUDFLARE_ACCOUNT_ID`).
- Tests: `PYTHONPATH=$PWD/src:$PWD/cloud/src pytest tests cloud/tests`; `cd site && pnpm build && pnpm vitest run`.
- Serving health: `dt-cloud healthcheck -u https://cw-s3.oa.dev -s cw/` with a `cw`-scoped token (Secret Manager `cw-s3-job-grant`).

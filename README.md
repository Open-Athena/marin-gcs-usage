# cw-s3

Storage usage of Marin's CoreWeave (CAIOS) S3 buckets, served at [cw-s3.oa.dev]: treemap, sizes over time, diffs between scans, bucket lifecycle rules, and a plan-first sweep console.

This is the `cw-s3` deployment branch of [disky]. It merges the shared `cloud` base (the `disk-tree` engine, the `dt-cloud` CLI and the viewer site) and adds the CoreWeave store on top: the scan job, the cw store's site config, sweep plans, and the `#cw-s3-usage` Slack digest.

## How it runs

- **Scan job** ([`job/cw-run.sh`], GCP Batch, every 12 h on the `:cw` image): `disk-tree bulk-list` each bucket in `CW_BUCKETS` over the CAIOS S3 endpoint, `disk-tree import -e stream`, then `dt-cloud` `index-write` / `index-sync` / `index-gc` (the path-store tiers the site reads), `publish-r2`, `lifecycle pull`, `warm-cache` and `cw-digest`.
- **Site** ([`site/`], Cloudflare Pages project `oa-cw-s3-usage`): the viewer reads each scan's index tiers through its Pages Functions; marks, sweep plans and runs live in D1 (`oa-cw-s3-usage-db`, migrations in `site/migrations/cw/`). Sign-in is the app's own (Google OIDC or emailed codes).
- **Digest**: one Slack thread per month in `#cw-s3-usage`, with a daily reply and an OP image (size vs quota, plus a "what changed" diff treemap).

## Development

See [`AGENTS.md`] (also `CLAUDE.md`).

[cw-s3.oa.dev]: https://cw-s3.oa.dev
[disky]: https://github.com/runsascoded/disky
[`job/cw-run.sh`]: job/cw-run.sh
[`site/`]: site
[`AGENTS.md`]: AGENTS.md

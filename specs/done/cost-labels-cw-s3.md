# Cost labels: the cw-s3 rollout

The design, the billing export and the query are in `cloud`'s `specs/cost-labels.md`. This file covers only the cw-s3 branch's part.

1. Merge `cloud` (it brings `gcp_jobs.labeled_provider` / `cost_labels`, `RunJobCron(labels=)`, `submitter_spec`'s `DISKY_LABELS` passthrough, dt-cloud's `label_batch_spec`, and the site's `DISKY_LABELS` var).
2. `git apply specs/cost-labels-cw-s3.patch`. It touches `infra/gcp/__main__.py`, the `job/` submitters and `site/wrangler.toml`. It was checked with `git apply --check` against `cw-s3` on 2026-10-09.
3. Add `export DISKY_LABELS=app=disky,deployment=cw-s3` to this worktree's `.envrc`. `pulumi preview` refuses to run without it.
4. `cd infra/gcp && pulumi preview -s cw-s3 --diff`. Expect one provider create, updates to label-capable resources and the cron bodies only, and no replaces. Ryan's go is needed for `up`.
5. Rebuild the job image (`job/build.sh`), so the scan VM's `dt-cloud` labels its children, then redeploy the site (its sweep executors).

Commit this file and the patch's changes together, then move this file to `specs/done/`.

## Status (2026-10-10): done

`pulumi up -s cw-s3` applied (1 created, 9 updated, nothing replaced; verified labels on a secret and the scan cron body), `DISKY_LABELS` in the worktree `.envrc`, the `:cw` image rebuilt with it, and the site deployed with the var.

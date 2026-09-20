# Batch job scheduling + secret wiring as IaC

Capture the hand-edited-in-GCP scheduling and secret plumbing behind the two GCP Batch jobs (the GCS fleet scan and the CoreWeave scan) as Pulumi, so it stops being drift-prone. This spec is investigation + design only; nothing here has been applied. No `pulumi up`, `pulumi import`, or mutating `gcloud` was run to produce it — the inventory below came from read-only `describe`/`list`/`get-iam-policy`.

## Why

Today the image, the `run.sh`/`cw-run.sh` pipelines, and the `job/*-batch-submit.sh` submitters (which contain the Batch job spec as a heredoc) are git-tracked. But the things that actually schedule and authorize the jobs live only in GCP, hand-edited: the two Cloud Scheduler cron jobs (schedule + the full Batch job spec they POST as their request body) and the Secret Manager secrets + their IAM bindings to the job service account. There is no record in git of the crons' schedules or bodies, and no guard against the cron body drifting from the tracked submitter. We already have one confirmed drift (see [Gap](#gap)).

## Inventory (live GCP, read-only)

Project `oa-internal-450019`, region `us-central1`. Job/scheduler service account `gcs-usage-job@oa-internal-450019.iam.gserviceaccount.com` (display name "Daily GCS-usage snapshot job").

### Cloud Scheduler jobs (HTTP → Batch API)

Both POST a full Batch job spec to `https://batch.googleapis.com/v1/projects/oa-internal-450019/locations/us-central1/jobs` (the jobs collection, no `job_id` — Batch auto-names each run), authed with an `oauthToken` for the job SA (`scope=cloud-platform`), `attemptDeadline=180s`, default retry config (min 5s / max 3600s backoff, 5 doublings).

| Job | Schedule (TZ) | Image tag | Body = |
|---|---|---|---|
| `gcs-usage-snapshot-daily` | `0 7 * * *` (Etc/UTC) | `…/gcs-usage-snapshot:latest` | GCS fleet scan spec (7 GCS bucket mounts, `n2-highmem-32`, 250000 MiB, 1500 GB local-SSD `stage`, `maxRunDuration=21600s`) |
| `cw-usage-snapshot` | `0 */12 * * *` (Etc/UTC) | `…/gcs-usage-snapshot:cw` | CoreWeave scan spec (`entrypoint=bash`, `commands=[job/cw-run.sh]`, one GCS mount, `n2-standard-8`, 30000 MiB, 200 GB pd-ssd `stage`, `maxRunDuration=14400s`) |

A third scheduler, `gcs-sheet-sync-hourly` (`5 * * * *`), targets a Cloud **Run** job (`…/run.googleapis.com/…/jobs/gcs-sheet-sync:run`), not Batch — out of scope here, but the same drift argument applies and it could join the same stack later.

The daily body's env `variables` block: `DUCKDB_MEM=100GB`, `DUCKDB_MEM_ACCESS=48GB`, `DUCKDB_THREADS=16`, `DATA_BUCKET=oa-gcs-usage-dvx`, `STAGE_DIR=/stage`, `LISTING_MODE=diy`, `SLACK_CHANNEL`, `SLACK_ALERT_CHANNEL`, `CLOUDFLARE_ACCOUNT_ID`. Its `secretVariables`: `SLACK_BOT_TOKEN` → `gcs-alert-slack-bot-token`, `SLACK_WEBHOOK` → `gcs-alert-slack-webhook`, `CLOUDFLARE_API_TOKEN` → `cf-pages-token`, `GCS_USAGE_TOKEN` → `gcs-sheet-sync-token`, `DISCORD_GCS_USAGE_WEBHOOK` → `gcs-usage-discord-webhook`, `DISCORD_BOT_TOKEN` → `marin-discord-bot-token` (all `…/versions/latest`).

The CW body's `secretVariables`: `AWS_ACCESS_KEY_ID` → `cw-s3-access-key-id`, `AWS_SECRET_ACCESS_KEY` → `cw-s3-secret-access-key`, `SLACK_BOT_TOKEN` → `cw-s3-slack-bot-token`, `CLOUDFLARE_API_TOKEN` → `cf-pages-token`.

### Secret Manager secrets

Nine secrets back the two jobs (automatic replication; values hand-created via `gcloud secrets create`, never in git). Each has exactly one IAM binding: `roles/secretmanager.secretAccessor` → the job SA.

`cf-pages-token`, `cw-s3-access-key-id`, `cw-s3-secret-access-key`, `cw-s3-slack-bot-token`, `gcs-alert-slack-bot-token`, `gcs-alert-slack-webhook`, `gcs-sheet-sync-token`, `gcs-usage-discord-webhook`, `marin-discord-bot-token`.

(`go-ao-dev-backups` and `OA-GDrive-Upload-Client-Info` exist in the project but belong to other systems — golink and a GDrive uploader — and are out of scope.)

### Service account + IAM

- `gcs-usage-job@…` — project roles: `roles/artifactregistry.reader`, `roles/batch.agentReporter`, `roles/batch.jobsEditor`, `roles/logging.logWriter`.
- actAs on the job SA: `roles/iam.serviceAccountUser` held by the job SA itself (self-actAs — required so the scheduler, running as this SA, can submit a Batch job that also runs as this SA) and by `gcs-usage-dispatch@…`; `roles/iam.serviceAccountTokenCreator` held by the human operator. The `gcs-usage-dispatch@` SA is a separate dispatcher identity — note it exists; managing it is optional.

### Artifact Registry

`cloud-run-source-deploy` (DOCKER, STANDARD, `us-central1`) — holds `gcs-usage-snapshot:{latest,cw}` plus unrelated Cloud Run source-deploy images (~19 GB). Shared, pre-existing infra; **reference it, don't adopt it** into this stack.

## Gap

What git already has: the image contents, `run.sh`/`cw-run.sh`, and the `job/*-batch-submit.sh` submitters (Batch spec heredoc + env/secret bindings), which can be dumped with `DRY=1 ./job/<x>-batch-submit.sh`.

What is only in GCP: the two cron **schedules**, the crons' **request bodies**, the nine **secrets** + their **accessor IAM**, the **job SA** + its **project roles**.

Confirmed drift (git submitter vs. live cron body), from `diff <(DRY=1 …) <live body>`:

- **CW**: the tracked `job/cw-batch-submit.sh` `DRY=1` output is byte-identical to the live `cw-usage-snapshot` body (after key-sorting). No drift.
- **Daily**: the live `gcs-usage-snapshot-daily` body carries `LISTING_MODE=diy` in its env `variables`, which the tracked `job/batch-submit.sh` does **not** emit. This is real drift — the cron would set `LISTING_MODE=diy`, a from-scratch resubmit via the script would not.
- **Hazard, not drift**: `DRY=1 ./job/batch-submit.sh` run locally also emitted `USER=ryan`, because the submitter passes a passthrough allow-list of ambient env vars straight into the spec. So the script's output is environment-sensitive — the exact body depends on the caller's shell. This is a second reason the body should be pinned deterministically rather than regenerated ad hoc.

## Pulumi design

Match `ops/` conventions (see `ops/gcp/golink/`): Python + `pulumi-gcp`, one directory per Pulumi project under `ops/gcp/`, GCS backend `gs://oa-pulumi`, GCP-KMS secrets provider, APIs enabled idempotently via `gcp.projects.Service`, additive `gcp.projects.IAMMember` / `gcp.secretmanager.SecretIamMember` for bindings, config-driven with `gcp.Config("gcp").require("project")`.

New project: `ops/gcp/gcs-usage/` (stack `prod`). Resources:

1. **`gcp.serviceaccount.Account`** `gcs-usage-job` — the job/scheduler identity.
2. **`gcp.projects.IAMMember` ×4** — the SA's four project roles.
3. **`gcp.secretmanager.Secret` ×9** — the secret **containers** only (`secret_id` + automatic replication). Pulumi does **not** manage `SecretVersion` payloads; values stay hand-created out-of-band, so no secret material ever lands in code or state as a managed input. (Import adopts only the container.)
4. **`gcp.secretmanager.SecretIamMember` ×9** — `secretAccessor` → job SA, one per secret (loop).
5. **`gcp.cloudscheduler.Job` ×2** — schedule + `http_target` (uri, `POST`, `Content-Type: application/json`, base64 `body`, `oauth_token{service_account_email, scope}`). This is the core drift-prone thing being captured.
6. Optional: **`gcp.serviceaccount.IAMMember`** for the `gcs-usage-dispatch` actAs binding; a **`gcp.artifactregistry.get_repository`** data source for reference only (do not adopt the repo).

### Keeping the cron body from re-drifting (the whole point)

The Batch spec must have one source of truth. The submitters (`job/*-batch-submit.sh`, canonical for the spec shape) already emit it via `DRY=1`. Options, best first:

- **(B, recommended) Pulumi generates the body from the submitter.** The `__main__.py` shells out to `DRY=1 bash <repo>/job/batch-submit.sh` (and `cw-batch-submit.sh`), parses the JSON, and feeds it as the scheduler body. `<repo>` comes from a stack config `gcs_usage_repo` (default: the sibling checkout, e.g. `../../../marin-gcs-usage` relative to the ops project, or an absolute path; in CI, a checkout step). One source of truth, no re-drift by construction. Caveats to close first: (a) the submitter's ambient-env passthrough must be neutered for deterministic output — run it under a scrubbed env (`env -i` plus only the intended vars), or add a `--pin`/`CRON=1` mode to the submitter that ignores ambient passthroughs; (b) `LISTING_MODE=diy` must be reconciled (add it to the submitter's defaults, or drop it from the cron) so the generated body matches what's imported; (c) cross-repo path coupling between `ops` and `marin-gcs-usage`.

- **(A, fallback) Literal body in Pulumi + a parity check.** Keep the imported body inline in `__main__.py`, and add a CI/pre-commit check that diffs `DRY=1 ./job/*-batch-submit.sh` (scrubbed env) against the committed Pulumi body and fails on drift. Simpler wiring, but two representations to keep aligned.

Either way the fix for the ambient-env hazard (deterministic submitter output) is a prerequisite, and is worth doing regardless.

### Import-first adoption

Everything already exists, so **import, never recreate** — `pulumi import` adopts a live resource into state and prints matching code; a subsequent `pulumi preview` must show **no** changes. Sequence:

0. Reconcile the daily-body drift first (decide `LISTING_MODE`), and make the submitter's `DRY=1` output deterministic, so imported bodies and generated bodies agree.
1. `pulumi stack init prod --secrets-provider=gcpkms://projects/oa-internal-450019/locations/global/keyRings/ops/cryptoKeys/pulumi/` and set `gcp:project=oa-internal-450019`, `gcp:region=us-central1`.
2. Import the SA, then its project IAM members, the secrets, the secret IAM members, then the two schedulers (details in the commands list below).
3. `pulumi refresh` then `pulumi preview` — iterate on the code until preview is empty (the SA description, secret replication, and especially the scheduler base64 body must match exactly).
4. Only once preview is clean, hand management over (future edits go through Pulumi; stop hand-editing in the console).

## Risks & sequencing

- **A destructive replace on a scheduler would drop a cron.** Import + verify empty preview before trusting `up`. If a field can't be matched (e.g. body byte-difference), fix the code, not the resource. Never let Pulumi replace `gcs-usage-snapshot-daily` / `cw-usage-snapshot`.
- **Secret values must never be managed.** Model only `Secret` containers + IAM; never `SecretVersion` with data. This keeps secret material out of the public repo and out of Pulumi state.
- **Base64 body exactness.** `google_cloud_scheduler_job.http_target.body` is base64; the imported value must round-trip exactly or every `up` shows a diff. This is the main reason to reconcile drift and pin the submitter output before importing.
- **Project IAMMember is additive.** Use `IAMMember` (as golink does), not `IAMPolicy`/`IAMBinding`, so importing the SA's roles can't clobber the rest of the project policy.
- **Don't adopt the Artifact Registry repo** — it's shared and holds unrelated images; reference via data source only.
- **Cross-repo coupling** (design B): the ops stack reads `marin-gcs-usage`'s submitters. Pin via stack config + a CI checkout; document that a submitter change is a two-repo concern.
- **Public repo hygiene:** resource names, paths, and the SA email are fine (already in tracked scripts); no secret values, emails, or other PII in this spec or the ops code.

## Import command sequence (plan — DO NOT RUN yet)

From `ops/gcp/gcs-usage/` after the stack is selected. Pulumi resource-name (2nd arg) is the logical name in code; last arg is the GCP import id.

```bash
# Service account
pulumi import gcp:serviceaccount/account:Account gcs-usage-job \
  projects/oa-internal-450019/serviceAccounts/gcs-usage-job@oa-internal-450019.iam.gserviceaccount.com

# SA project roles (additive IAMMember; id = "<project> <role> <member>")
pulumi import gcp:projects/iAMMember:IAMMember job-sa-artifactregistry-reader \
  "oa-internal-450019 roles/artifactregistry.reader serviceAccount:gcs-usage-job@oa-internal-450019.iam.gserviceaccount.com"
pulumi import gcp:projects/iAMMember:IAMMember job-sa-batch-agent-reporter \
  "oa-internal-450019 roles/batch.agentReporter serviceAccount:gcs-usage-job@oa-internal-450019.iam.gserviceaccount.com"
pulumi import gcp:projects/iAMMember:IAMMember job-sa-batch-jobs-editor \
  "oa-internal-450019 roles/batch.jobsEditor serviceAccount:gcs-usage-job@oa-internal-450019.iam.gserviceaccount.com"
pulumi import gcp:projects/iAMMember:IAMMember job-sa-logging-log-writer \
  "oa-internal-450019 roles/logging.logWriter serviceAccount:gcs-usage-job@oa-internal-450019.iam.gserviceaccount.com"

# Secrets (container only; id = projects/<p>/secrets/<name>) — repeat for each:
#   cf-pages-token cw-s3-access-key-id cw-s3-secret-access-key cw-s3-slack-bot-token
#   gcs-alert-slack-bot-token gcs-alert-slack-webhook gcs-sheet-sync-token
#   gcs-usage-discord-webhook marin-discord-bot-token
pulumi import gcp:secretmanager/secret:Secret cf-pages-token \
  projects/oa-internal-450019/secrets/cf-pages-token
# … ×9

# Secret accessor bindings (id = "projects/<p>/secrets/<name> <role> <member>") — ×9:
pulumi import gcp:secretmanager/secretIamMember:SecretIamMember cf-pages-token-accessor \
  "projects/oa-internal-450019/secrets/cf-pages-token roles/secretmanager.secretAccessor serviceAccount:gcs-usage-job@oa-internal-450019.iam.gserviceaccount.com"
# … ×9

# Cloud Scheduler crons (id = projects/<p>/locations/<region>/jobs/<name>)
pulumi import gcp:cloudscheduler/job:Job gcs-usage-snapshot-daily \
  projects/oa-internal-450019/locations/us-central1/jobs/gcs-usage-snapshot-daily
pulumi import gcp:cloudscheduler/job:Job cw-usage-snapshot \
  projects/oa-internal-450019/locations/us-central1/jobs/cw-usage-snapshot
```

After each batch of imports: `pulumi refresh` → `pulumi preview`, and edit the program until preview reports no changes. Then, and only then, is the stack authoritative.

## Status

Draft. Read-only investigation complete; draft Pulumi program at `ops/gcp/gcs-usage/` (not yet a live stack — no `pulumi stack init` run, no import, no `up`). Move to `specs/done/` once imported and preview-clean.

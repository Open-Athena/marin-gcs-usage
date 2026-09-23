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

Confirmed drift (git submitter vs. live cron body), from `diff <(PIN=1 DRY=1 …) <live body>` — **now reconciled** (see [design](#keeping-the-cron-body-from-re-drifting-the-whole-point--implemented)):

- **CW**: `PIN=1 DRY=1 ./job/cw-batch-submit.sh` is byte-identical to the live `cw-usage-snapshot` body (after key-sorting). No drift.
- **Daily**: the live `gcs-usage-snapshot-daily` body carries `LISTING_MODE=diy` in its env `variables`, which the tracked `job/batch-submit.sh` does **not** emit. `LISTING_MODE` is dead code (removed in `850e8cd`); the cron value is a no-op vestige. Resolution: drop it from the cron, applied by the first `pulumi up` after import (the one intended semantic change on adoption).
- **Ambient-env hazard (fixed):** `DRY=1 ./job/batch-submit.sh` run locally used to leak `USER=ryan` into the spec, because the submitter forwards a passthrough allow-list of ambient env vars — so its output was caller-shell-dependent. Fixed by the new `PIN=1` mode (both submitters), which the Pulumi program uses so the generated body is byte-stable.

## Pulumi design

Match `ops/` conventions (see `ops/gcp/golink/`): Python + `pulumi-gcp`, one directory per Pulumi project under `ops/gcp/`, GCS backend `gs://oa-pulumi`, GCP-KMS secrets provider, APIs enabled idempotently via `gcp.projects.Service`, additive `gcp.projects.IAMMember` / `gcp.secretmanager.SecretIamMember` for bindings, config-driven with `gcp.Config("gcp").require("project")`.

New project: `ops/gcp/gcs-usage/` (stack `prod`). Resources:

1. **`gcp.serviceaccount.Account`** `gcs-usage-job` — the job/scheduler identity.
2. **`gcp.projects.IAMMember` ×4** — the SA's four project roles.
3. **`gcp.secretmanager.Secret` ×9** — the secret **containers** only (`secret_id` + automatic replication). Pulumi does **not** manage `SecretVersion` payloads; values stay hand-created out-of-band, so no secret material ever lands in code or state as a managed input. (Import adopts only the container.)
4. **`gcp.secretmanager.SecretIamMember` ×9** — `secretAccessor` → job SA, one per secret (loop).
5. **`gcp.cloudscheduler.Job` ×2** — schedule + `http_target` (uri, `POST`, `Content-Type: application/json`, base64 `body`, `oauth_token{service_account_email, scope}`). This is the core drift-prone thing being captured.
6. Optional: **`gcp.serviceaccount.IAMMember`** for the `gcs-usage-dispatch` actAs binding; a **`gcp.artifactregistry.get_repository`** data source for reference only (do not adopt the repo).

### Keeping the cron body from re-drifting (the whole point) — IMPLEMENTED

The Batch spec has one source of truth: the submitters (`job/*-batch-submit.sh`). The Pulumi program (`ops/gcp/gcs-usage/__main__.py`, `batch_body()`) shells out to `PIN=1 DRY=1 bash <repo>/job/<submitter>` under a fully scrubbed subprocess env, parses the JSON, and feeds it as the scheduler body. `<repo>` comes from stack config `gcs_usage_repo` (default: the sibling checkout `../../../marin-gcs-usage` relative to the ops project; in CI, a checkout step). No re-drift by construction.

Both prerequisites are now closed (2026-09-20, this branch):

- **Deterministic submitter output — done.** Both submitters honor a new `PIN=1` mode: `PIN` unsets the bash-level override knobs (`IMAGE`/`MACHINE`/`MEMORY_MIB`/`LOCAL_SSD_GB`/`MAX_RUN_SECONDS`/`DATA_BUCKET`, plus `CW_BUCKET` for the CW one) before the `${VAR:-default}` lines, and the `vars()` python skips the ambient passthrough allow-list and reads every default via a `g()` shim that ignores `os.environ`. Verified: `PIN=1 DRY=1 ./job/<submitter>` produces byte-identical output under a deliberately polluted shell (`USER=hacker DATA_BUCKET=evil MACHINE=n2-tiny SWEEP=1 WEEKLY=1 …`). The non-PIN path is unchanged — manual one-off overrides (`SWEEP=1`, `REPROC=1`, `MACHINE=…`, `CW_BUCKETS=…`) still forward exactly as before. This also fixes the demonstrated `USER=ryan` leak.

- **`LISTING_MODE` reconciled — drop from the cron.** `LISTING_MODE` is **dead code**: commit `850e8cd` ("Make DIY the only listing mode; delete SII path") made DIY unconditional, deleted the var from `run.sh`, and dropped its passthrough from `batch-submit.sh`. Nothing in the repo reads it any more. The live daily cron still carries `LISTING_MODE=diy` as a stale vestige — a no-op. So the reconciliation is to **remove it from the cron**, which the generated body already omits; the first `pulumi up` after import applies that removal. (The same commit message flags this exact class of bug — the var "silently dropped out of the Cloud Scheduler spec when it was regenerated" — which is why this IaC exists.)

Fallback, if the subprocess-at-plan-time coupling is ever unwanted: keep a literal body inline + a CI check diffing `PIN=1 DRY=1 ./job/*-batch-submit.sh` against it. Not needed now.

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

## Field-by-field match (Pulumi ↔ live)

Statically checked against the captured live JSON (`tmp/batch-iac/`); the pulumi CLI was **not** run (it would authenticate to prod). Each resource's declared inputs mirror live:

- **`serviceaccount.Account` `gcs-usage-job`** — `account_id=gcs-usage-job`, `display_name="Daily GCS-usage snapshot job"`. Live SA has no `description` → none set. ✓
- **`projects.IAMMember` ×4** — roles `artifactregistry.reader`, `batch.agentReporter`, `batch.jobsEditor`, `logging.logWriter`; member `serviceAccount:gcs-usage-job@…`. Additive (non-authoritative). ✓
- **`secretmanager.Secret` ×9** — `secret_id` = each name; `replication.auto={}` matches live `automatic: {}`. No versions/values modeled. ✓
- **`secretmanager.SecretIamMember` ×9** — `role=roles/secretmanager.secretAccessor`, member the job SA; one per secret, matching the single live binding each has. ✓
- **`cloudscheduler.Job` ×2** —
  - `schedule`/`time_zone`: `0 7 * * *` / `Etc/UTC` (daily), `0 */12 * * *` / `Etc/UTC` (cw). ✓
  - `attempt_deadline="180s"`. ✓
  - `http_target.uri` = the Batch jobs-collection URL; `http_method=POST`; `headers={"Content-Type":"application/json"}` (Cloud Scheduler injects `User-Agent`/`Content-Length` itself; the provider ignores those, so they're intentionally omitted). ✓
  - `oauth_token.service_account_email=gcs-usage-job@…`, `scope=https://www.googleapis.com/auth/cloud-platform`. ✓
  - `retry_config` set explicitly to the live server defaults (`retry_count=0`, `max_retry_duration=0s`, `min_backoff_duration=5s`, `max_backoff_duration=3600s`, `max_doublings=5`) so import→preview doesn't diff on computed values. ✓
  - `body` = base64 of `PIN=1 DRY=1 ./job/<submitter>` (canonical JSON). Content matches live **except**: (a) the daily body omits the dead `LISTING_MODE=diy` (intended — see Gap); (b) whitespace/key-ordering differ from the hand-created live bytes, so the first `up` re-normalizes the body formatting on both crons. Both are expected and semantically safe (Batch parses JSON regardless of formatting).
- **`artifactregistry.get_repository`** — reference only (data source), never adopted. ✓

The only non-empty items the first post-import `preview` should show: the two cron `body` fields (formatting re-normalization on both + `LISTING_MODE` removal on daily). If preview shows **anything else** (a role, a secret, the SA, oauth, schedule, retry), stop and fix the code before `up`.

## Runbook (for the user — mutating steps are user-gated)

Prereqs: `gcloud auth login --update-adc`, `pulumi login gs://oa-pulumi`, a local `marin-gcs-usage` checkout on `cw-s3` (or set `gcs-usage:gcs_usage_repo`), `python3` on PATH (the submitters shell out to it). Run from `ops/gcp/gcs-usage/`.

```bash
# 0. Deps + stack (KMS secrets provider, per ops/README.md)
uv sync   # or: pip install 'pulumi>=3,<4' 'pulumi-gcp>=9,<10'
pulumi stack init prod \
  --secrets-provider=gcpkms://projects/oa-internal-450019/locations/global/keyRings/ops/cryptoKeys/pulumi/
pulumi config set gcp:project oa-internal-450019
pulumi config set gcp:region  us-central1
# if the marin-gcs-usage checkout isn't the sibling default:
# pulumi config set gcs-usage:gcs_usage_repo /abs/path/to/marin-gcs-usage

# 1. Import every existing resource (adopt, don't recreate). Full id list in
#    "Import command sequence" above — SA, 4 project IAM, 9 secrets, 9 secret
#    IAM, 2 crons. Example first + last:
pulumi import gcp:serviceaccount/account:Account gcs-usage-job \
  projects/oa-internal-450019/serviceAccounts/gcs-usage-job@oa-internal-450019.iam.gserviceaccount.com
# … (all imports) …
pulumi import gcp:cloudscheduler/job:Job cw-usage-snapshot \
  projects/oa-internal-450019/locations/us-central1/jobs/cw-usage-snapshot

# 2. Reconcile state with live, then inspect the plan
pulumi refresh --yes
pulumi preview --diff | tee /tmp/gcs-usage-preview.txt
```

**What an acceptable preview looks like:** `~ 2 to update` (the two `cloudscheduler.Job`s), both diffs limited to `http_target.body`; the daily diff drops `LISTING_MODE` and re-formats, the cw diff only re-formats. Everything else: `unchanged`. **Any create/replace/delete, or any change to a role / secret / SA / oauth / schedule / retry, is a red flag — do not `up`; fix the code (or the import) first.**

```bash
# 3. Apply — normalizes the two cron bodies (and drops dead LISTING_MODE). No
#    other resource changes. This is the only mutating step.
pulumi up --yes

# 4. Confirm steady state
pulumi preview   # expect: no changes
```

**Rollback:** the crons keep running throughout (import + refresh don't touch them; `up` only rewrites the request body, which the next fire uses). If `up` misbehaves, restore either cron's previous body from the captured JSON — decode and re-set with the same SA/oauth:

```bash
# body-only restore of a cron from the pre-adoption capture (no Pulumi):
gcloud scheduler jobs update http gcs-usage-snapshot-daily \
  --project oa-internal-450019 --location us-central1 \
  --message-body-from-file <(python3 -c "import json,base64;print(base64.b64decode(json.load(open('tmp/batch-iac/sched-gcs-usage-snapshot-daily.json'))['httpTarget']['body']).decode())")
```

`pulumi destroy` is **not** a rollback here (it would delete the crons/secrets); never run it against this stack. To hand a resource back to manual control instead, `pulumi state delete <urn>` (drops it from state, leaves it live).

## Status

Import-READY. Read-only investigation complete; drift reconciled (submitter `PIN=1` mode + dead-`LISTING_MODE` finding); `PIN=1 DRY=1` output verified deterministic and matching live (cw byte-identical; daily = live minus dead `LISTING_MODE`). Draft Pulumi program at `ops/gcp/gcs-usage/` passes `py_compile`, fields mapped to live. Still **not** a live stack — no `pulumi stack init`/`import`/`up` run (those are user-gated, per the runbook). Move to `specs/done/` once imported and preview-clean.

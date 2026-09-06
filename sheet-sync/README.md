# `sheet-sync` — hourly mark-status → Google Sheet

Keeps Percy's shared sheet mirroring the site's `/users` "who still needs to
mark & sweep" rollup, refreshed hourly, with no human re-export. Standalone from
the heavy daily-snapshot job (`job/`, root `Dockerfile`): this image is just the
`gcs-usage` CLI, so hourly runs are cheap and cold-start fast.

## Chain (`sync.sh`)

1. `gcs-usage report` → the per-user CSV: the site's `/api/marks/totals` for
   the latest scan (the ledger folded against the index, claims applied), with
   the read-only `gcs` grant token (`GCS_USAGE_TOKEN`, from Secret Manager).
2. `gcs-usage sheet-push -w <tab> -D <footer>` → full-replaces one **named** tab
   in place (values-only clear preserves header styling + freeze), stamping an
   "AUTO — regenerated hourly" disclaimer two rows below the table.

Runs **as the SA** `gcs-usage-job@…` (the daily-snapshot SA): ambient ADC covers
both the GCS read and the Sheets write (the SA is Editor on the sheet) — no key
material, no impersonation. Percy stays Editor; only the named tab is rewritten,
so derived tabs people add are untouched.

## Deploy

```bash
sheet-sync/build.sh                                  # Cloud Build → :latest
SHEET_ID=1k_11LA…RFc sheet-sync/deploy.sh            # Run job + hourly Scheduler + IAM
gcloud run jobs execute gcs-sheet-sync --region us-central1 --wait   # one-off test
```

`deploy.sh` is idempotent and grants the SA the two roles the wiring needs
(`secretmanager.secretAccessor` on the token, `run.invoker` on the job so the
Scheduler can trigger it). A code/CLI change reaches prod by re-running
`build.sh` (the job runs `:latest`); a wiring change, by re-running `deploy.sh`.

## The grant token

Read-only `@open-athena/auth` `gcs` grant (`email=NULL` → can GET `/api/actions`,
cannot mark), minted into the site's D1 and stashed in Secret Manager
(`gcs-sheet-sync-token`). Revoke by setting `revoked_at` on its `grants` row —
independent of anyone's personal token. See `specs/gsheet-mark-status-sync.md`.

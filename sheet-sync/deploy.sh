#!/usr/bin/env bash
# Create/update the hourly mark-status → GSheet sync: a Cloud Run *job* (runs
# sync.sh once) + an hourly Cloud Scheduler trigger, both as the snapshot SA.
# Idempotent: `run jobs deploy` upserts; the scheduler is create-or-update.
# Grants the SA the two IAM roles the wiring needs (secret access + self-invoke).
# Run sheet-sync/build.sh first (or pass a fresh IMAGE).
set -euo pipefail

PROJECT=${PROJECT:-oa-internal-450019}
REGION=${REGION:-us-central1}
SA=${SA:-gcs-usage-job@oa-internal-450019.iam.gserviceaccount.com}
IMAGE=${IMAGE:-us-central1-docker.pkg.dev/$PROJECT/cloud-run-source-deploy/gcs-sheet-sync:latest}
SECRET=${SECRET:-gcs-sheet-sync-token}
SHEET_ID=${SHEET_ID:?SHEET_ID must be set (the Google Sheet key)}
JOB=gcs-sheet-sync
SCHED=$JOB-hourly
SCHEDULE=${SCHEDULE:-5 * * * *}   # hourly, :05 to dodge top-of-hour contention

echo "== IAM: SA can read the token secret ==" >&2
gcloud secrets add-iam-policy-binding "$SECRET" --project "$PROJECT" \
  --member "serviceAccount:$SA" --role roles/secretmanager.secretAccessor >/dev/null

echo "== Cloud Run job (upsert) ==" >&2
gcloud run jobs deploy "$JOB" --project "$PROJECT" --region "$REGION" \
  --image "$IMAGE" --service-account "$SA" \
  --set-env-vars "SHEET_ID=$SHEET_ID,DATA_BUCKET=oa-gcs-usage-dvx" \
  --set-secrets "GCS_USAGE_TOKEN=$SECRET:latest" \
  --max-retries 1 --task-timeout 300

echo "== IAM: SA can run the job (Scheduler invokes as SA) ==" >&2
gcloud run jobs add-iam-policy-binding "$JOB" --project "$PROJECT" --region "$REGION" \
  --member "serviceAccount:$SA" --role roles/run.invoker >/dev/null

URI="https://$REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT/jobs/$JOB:run"
echo "== Cloud Scheduler (create or update) ==" >&2
if gcloud scheduler jobs describe "$SCHED" --project "$PROJECT" --location "$REGION" >/dev/null 2>&1; then
  verb=update
else
  verb=create
fi
gcloud scheduler jobs "$verb" http "$SCHED" --project "$PROJECT" --location "$REGION" \
  --schedule "$SCHEDULE" --time-zone UTC \
  --uri "$URI" --http-method POST \
  --oauth-service-account-email "$SA"

echo "done. Trigger a one-off run with:" >&2
echo "  gcloud run jobs execute $JOB --project $PROJECT --region $REGION --wait" >&2

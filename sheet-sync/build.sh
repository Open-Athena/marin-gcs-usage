#!/usr/bin/env bash
# Build + push the standalone sheet-sync image via Cloud Build (no local Docker).
# The hourly Cloud Run job runs IMAGE:latest, so a rebuild is how sync.sh / CLI
# changes reach it. Env: PROJECT, IMAGE (override the tag).
set -euo pipefail
cd "$(dirname "$0")/.."
PROJECT=${PROJECT:-oa-internal-450019}
IMAGE=${IMAGE:-us-central1-docker.pkg.dev/$PROJECT/cloud-run-source-deploy/gcs-sheet-sync:latest}
echo "building $IMAGE (Cloud Build; context = repo root, minus .gcloudignore)" >&2
exec gcloud builds submit --project "$PROJECT" \
  --config sheet-sync/cloudbuild.yaml --substitutions "_IMAGE=$IMAGE" .

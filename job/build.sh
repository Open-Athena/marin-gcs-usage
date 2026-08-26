#!/usr/bin/env bash
# Build + push the daily-snapshot Batch image via Cloud Build (offloaded — no
# local Docker). Bakes `job/run.sh`, the `gcs-usage` package (marin/src), and the
# disk-tree engine per the root Dockerfile; `.gcloudignore` trims the context.
#
# The scheduled job (batch-submit.sh) runs `IMAGE:latest`, so a rebuild is how
# run.sh / pipeline changes reach prod. Immutable per-day snapshots mean this is
# safe to run any time; the next daily run (or a manual batch-submit) picks it up.
#
# Env: PROJECT, IMAGE (override the tag, e.g. a throwaway tag to test a build).
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT=${PROJECT:-oa-internal-450019}
IMAGE=${IMAGE:-us-central1-docker.pkg.dev/$PROJECT/cloud-run-source-deploy/gcs-usage-snapshot:latest}

echo "building $IMAGE (Cloud Build; context = repo root, minus .gcloudignore)" >&2
exec gcloud builds submit --project "$PROJECT" --tag "$IMAGE" .

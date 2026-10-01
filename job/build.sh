#!/usr/bin/env bash
# Build + push the CoreWeave scan job's Batch image via Cloud Build (offloaded —
# no local Docker). Bakes `job/cw-run.sh`, the `dt-cloud` package (cloud/src),
# and the disk-tree engine per the root Dockerfile; `.gcloudignore` trims the
# context.
#
# The scheduled scan (cw-batch-submit.sh) runs `IMAGE:cw`, so a rebuild is how
# cw-run.sh / pipeline changes reach prod. Immutable per-scan snapshots mean
# this is safe to run any time; the next scan picks it up.
#
# Env: PROJECT, IMAGE (override the tag, e.g. a throwaway tag to test a build).
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT=${PROJECT:-oa-internal-450019}
# Which job script the image runs (`job/<JOB>`); the tag defaults to `cw`, never
# `latest` (the gcs deployment's image, built from its own branch).
JOB=${JOB:-cw-run.sh}
TAG=${TAG:-cw}
IMAGE=${IMAGE:-us-central1-docker.pkg.dev/$PROJECT/cloud-run-source-deploy/gcs-usage-snapshot:$TAG}

echo "building $IMAGE (Cloud Build; context = repo root, minus .gcloudignore)" >&2
exec gcloud builds submit --project "$PROJECT" --config cloudbuild.yaml --substitutions "_IMAGE=$IMAGE,_JOB=$JOB" .

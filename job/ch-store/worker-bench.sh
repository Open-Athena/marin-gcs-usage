#!/bin/bash
# From a laptop: `dt-cloud ch-bench` against the deployed Worker (gcs.oa.dev, uncached via the seeded
# `minArea` jitter), with the read-only sheet-sync grant from Secret Manager (never printed).
#   job/ch-store/worker-bench.sh OUT.jsonl [ch-bench args…]
set -euo pipefail
W=$(cd "$(dirname "$0")/../.." && pwd)
out=$1; shift
GCS_USAGE_TOKEN=$(gcloud secrets versions access latest --secret gcs-sheet-sync-token --project oa-internal-450019 | tr -d '[:space:]')
export GCS_USAGE_TOKEN
export PYTHONPATH=$W/cloud/src:$W/src
exec /Users/ryan/c/disky/.venv/bin/python -m dt_cloud.cli ch-bench -u https://gcs.oa.dev -T GCS_USAGE_TOKEN -o "$out" "$@"

#!/bin/bash
# From a laptop: one gcs.oa.dev response body to a file, with the read-only sheet-sync grant (never printed).
#   job/ch-store/fetch-worker.sh '/api/subtree?…' OUT.json
set -euo pipefail
tok=$(gcloud secrets versions access latest --secret gcs-sheet-sync-token --project oa-internal-450019 | tr -d '[:space:]')
curl -sS -A dt-cloud-probe/1.0 -H "Authorization: Bearer $tok" -o "$2" "https://gcs.oa.dev$1"

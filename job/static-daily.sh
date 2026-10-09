#!/usr/bin/env bash
# Append one scan to the static name index — a thin wrapper over the Python
# orchestrator (specs/static-append.md), which needs no gcloud:
#
#   job/static-daily.sh [-n] SCAN_ID
#
# `-c` appends every earlier published scan still pending first, in scan-id
# order. Exit 3: SCAN_ID is not published yet. Env: R2_ENDPOINT (or
# CLOUDFLARE_ACCOUNT_ID), and STATIC_NAMES_SRC until the job image carries the
# orchestrator's code.
set -euo pipefail
cd "$(dirname "$0")/.."
exec env STATIC_NAMES_PROFILE=gcs dt-cloud static-names runs add -c "$@"

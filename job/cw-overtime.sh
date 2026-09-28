#!/usr/bin/env bash
# Seal the next over-time index groups for the CoreWeave scans (specs/
# obs-axis-indexing.md Phase 1): `dt-cloud over-time-groups` builds every full
# K-scan group not yet in the `pyramid_multiscans` manifest from the scans'
# path-indices on the GCS mount, publishes each under its last scan's layer-2
# dir, syncs the footer + manifest row; then each new group's scan dir goes to
# R2 (`publish-r2`), where the site serves from. Stage 4d of cw-run.sh, which
# `source`s this with its env already set; standalone via cw-overtime-submit.sh
# (a one-off Batch job, e.g. the first backfill). Idempotent: a re-run appends
# only the groups still missing.
#
#   job/cw-overtime.sh          # env mirrors cw-run.sh (D1 ids, LAYER2_PREFIX, DATA_BUCKET, R2_*)
set -euo pipefail
# Strip surrounding whitespace off secrets before anything reads them (a version
# created with `--data-file=-` carries a trailing newline; see cw-run.sh).
for n in CLOUDFLARE_API_TOKEN R2_ENDPOINT R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY; do
  if [ -n "${!n+set}" ]; then v=${!n}; v=${v#"${v%%[![:space:]]*}"}; v=${v%"${v##*[![:space:]]}"}; export "$n=$v"; fi
done
export D1_DB_ID=${D1_DB_ID:-7f1e1326-b879-4ecd-8621-846621c24f36}   # oa-cw-s3-usage-db
export D1_DB_NAME=${D1_DB_NAME:-oa-cw-s3-usage-db}
export LAYER2_PREFIX=${LAYER2_PREFIX:-'cw-l2/{scan}/'}
export SNAPSHOTS_SUBDIR=${SNAPSHOTS_SUBDIR:-cw}
DATA=${DATA_BUCKET:-oa-gcs-usage-dvx}
GEN=${GEN:-$(date -u +%Y%m%dT%H%M%SZ)}
WORK=${WORK:-${WORK_DIR:-/stage}/over-time}
if [ -z "${CLOUDFLARE_API_TOKEN:+set}" ] || [ -z "${CLOUDFLARE_ACCOUNT_ID:-}" ]; then
  echo "no CLOUDFLARE_API_TOKEN/ACCOUNT_ID — over-time groups need D1; skipping" >&2
  exit 0
fi
NEW=$(dt-cloud over-time-groups -g "$GEN" -o "$WORK" -p "/gcs/$DATA" -m "${DUCKDB_MEM:-16GB}" -t "${IMPORT_JOBS:-8}")
for gid in $(printf '%s' "$NEW" | python3 -c 'import json, sys; print(" ".join(json.load(sys.stdin)["groups"]))'); do
  if [ -n "${R2_ENDPOINT:+set}" ] && [ -n "${R2_BUCKET:-}" ]; then  # `:+set`: xtrace must not print the creds
    dt-cloud publish-r2 "$gid" || echo "WARN: publish-r2 failed for over-time group $gid (the site reads R2: the group stays invisible until a re-publish)" >&2
  else
    echo "no R2_ENDPOINT/R2_BUCKET — over-time group $gid published to GCS only" >&2
  fi
done
echo "OVER-TIME-DONE: $NEW"

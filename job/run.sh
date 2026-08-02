#!/usr/bin/env bash
# Daily snapshot job (Cloud Run). Chain:
#   1. direct-list marin-us-central2 (SII unavailable there — API 502s)
#   2. webdata over: SII inventories (5 buckets) + central2 listing + weekly-scan fallback
#   3. publish snapshot JSONs to the data bucket (canonical store)
#   4. rebuild site data dir from the bucket (recent snapshots + scans.json) and deploy
#
# Buckets are mounted via GCS FUSE at /gcs/<bucket> (DuckDB needs local paths).
# Env: SNAPSHOT_DATE (default today UTC), DATA_BUCKET, DUCKDB_MEM,
# LISTING_PATH (default central2-listing/<date>; relative to the data bucket),
# LISTING_EXISTS (error|reuse|clear; default reuse = idempotent re-runs),
# SNAP_PATH (default snapshots/<date>), PUBLISH=0 to skip the site rebuild+deploy,
# CLOUDFLARE_API_TOKEN + CLOUDFLARE_ACCOUNT_ID (optional — skip deploy if absent).
# Parallel/experimental runs: override LISTING_PATH + SNAP_PATH (and PUBLISH=0)
# so they can't collide with the daily run'"'"'s outputs.
set -euxo pipefail

DATE=${SNAPSHOT_DATE:-$(date -u +%F)}
DATA=${DATA_BUCKET:-oa-gcs-usage-dvx}
LISTING_PATH=${LISTING_PATH:-central2-listing/$DATE}
SNAP_PATH=${SNAP_PATH:-snapshots/$DATE}
SII_BUCKETS=(marin-us-east1 marin-us-east5 marin-us-central1 marin-eu-west4 marin-us-west4)
FALLBACK_GLOB="/gcs/marin-us-central2/tmp/storage-scan/deduped/objects-*.parquet"
KEEP_DEPLOYED=30  # most-recent snapshots included in the site deploy

cd /app

# Scheduled retry attempts set NOP_IF_PUBLISHED=1: exit quietly when the
# snapshot already exists (an earlier attempt won). Manual runs leave it
# unset so intentional re-runs always proceed.
if [ "${NOP_IF_PUBLISHED:-0}" = "1" ] && [ -f "/gcs/$DATA/$SNAP_PATH/meta.json" ]; then
  echo "SNAPSHOT-JOB-NOP $DATE (already published)"
  exit 0
fi

# DIY mode (LISTING_MODE=diy): list ALL buckets ourselves via a fan-out Batch
# job (task per bucket) instead of depending on SII reports — their generation
# times scatter 02:26-13:00 UTC, day-1 reports can lag ~30h, and us-central2
# has none at all. Tasks reuse completed listings, so re-runs only fill gaps.
if [ "${LISTING_MODE:-sii}" = "diy" ]; then
  # We own the listing end-to-end: no SII in the primary path. If the fan-out
  # fails (any region OOMs or errors) or any bucket lacks a completed listing,
  # abort — better no snapshot than a silently incomplete one (a dropped
  # bucket like central2, which has no SII report at all, is a ~1000 TB hole).
  FLEET=(marin-us-central2 marin-eu-west4 marin-us-central1 marin-us-east5 marin-us-east1 marin-us-west4)
  # Listing node size/parallelism are runtime config: set LISTING_MACHINE /
  # LISTING_PROCS / LISTING_WORKERS on the snapshot job's env (batch-submit.sh
  # or the scheduler body) to resize without rebuilding — unset falls back to
  # the CLI defaults. computeResource is derived from the machine, so any size fits.
  LZ=()
  [ -n "${LISTING_MACHINE:-}" ] && LZ+=(-m "$LISTING_MACHINE")
  [ -n "${LISTING_PROCS:-}" ] && LZ+=(-P "$LISTING_PROCS")
  [ -n "${LISTING_WORKERS:-}" ] && LZ+=(-w "$LISTING_WORKERS")
  gcs-usage job submit-listing -d "$DATE" -W "${LZ[@]}"
  G=()
  for b in "${FLEET[@]}"; do
    if [ -f "/gcs/$DATA/listing/$DATE/$b/_SUCCESS.json" ]; then G+=("/gcs/$DATA/listing/$DATE/$b/*.parquet")
    else echo "ERROR: no completed $DATE listing for $b — aborting snapshot" >&2; exit 1; fi
  done
  AG=("/gcs/$DATA/attr/attribution-2026-07-20.parquet" "/gcs/$DATA/attr/attribution-wandb.parquet")
else

# 1. central2 direct listing (canonical schema shards straight into the data bucket).
# The exists-policy handles prior output: completed listings are reused,
# partials from crashed runs are cleared. Worker chunks are balanced using
# the newest previously-completed listing'"'"'s per-prefix object counts.
W=()
for d in $(ls "/gcs/$DATA/central2-listing/" 2>/dev/null | sort -r); do
  [ "$d" = "$(basename "$LISTING_PATH")" ] && continue
  if [ -f "/gcs/$DATA/central2-listing/$d/_SUCCESS.json" ]; then
    W=(-W "/gcs/$DATA/central2-listing/$d/*.parquet"); break
  fi
done
gcs-usage list-bucket marin-us-central2 -o "gs://$DATA/$LISTING_PATH" -P 6 -w 8 -x "${LISTING_EXISTS:-reuse}" "${W[@]}"

# 2. assemble input globs: SII per bucket (skip loudly if a day's report is
# missing), then the fresh central2 listing, then the weekly-scan fallback
# (earlier wins per bucket)
# SII reports generate at scattered times (02:30-13:00 UTC by bucket); when a
# bucket's report for $DATE hasn't landed yet, yesterday's SII beats the weekly
# scan fallback (which truncates huge flat prefixes, undercounting objects ~3x)
PREV=$(date -u -d "$DATE - 1 day" +%F)
G=()
for b in "${SII_BUCKETS[@]}"; do
  glob="/gcs/$b/inventory-reports/*_${DATE}T*_*.parquet"
  pglob="/gcs/$b/inventory-reports/*_${PREV}T*_*.parquet"
  if compgen -G "$glob" > /dev/null; then G+=("$glob")
  elif compgen -G "$pglob" > /dev/null; then G+=("$pglob"); echo "WARN: no SII report for $b on $DATE — using $PREV's" >&2
  else echo "WARN: no SII report for $b on $DATE or $PREV (falling back to weekly scan)" >&2; fi
done
G+=("/gcs/$DATA/$LISTING_PATH/*.parquet")
if compgen -G "$FALLBACK_GLOB" > /dev/null; then G+=("$FALLBACK_GLOB"); else echo "WARN: weekly-scan fallback glob is empty" >&2; fi
AG=("/gcs/$DATA/attr/attribution-2026-07-20.parquet" "/gcs/$DATA/attr/attribution-wandb.parquet")
fi  # LISTING_MODE

# With STAGE_DIR set (a local-SSD mount on Batch), copy all inputs there once
# and point webdata at the local copies — gcsfuse reads are ~20-50 MB/s and
# webdata makes several passes, so local NVMe scans win by an order of magnitude.
# DuckDB spill goes to the same disk.
if [ -n "${STAGE_DIR:-}" ]; then
  gcs-usage stage -o "$STAGE_DIR" "${G[@]}" "${AG[@]}"
  export DUCKDB_TMP=${DUCKDB_TMP:-$STAGE_DIR/.duckdb-tmp}
  mkdir -p "$DUCKDB_TMP"
fi
loc() { if [ -n "${STAGE_DIR:-}" ]; then echo "$STAGE_DIR/${1#/gcs/}"; else echo "$1"; fi; }
L=(); for g in "${G[@]}"; do L+=(-l "$(loc "$g")"); done
A=(); for a in "${AG[@]}"; do A+=(-a "$(loc "$a")"); done

gcs-usage webdata -d "$DATE" "${L[@]}" "${A[@]}" -o "/tmp/snap/$DATE"
gcs-usage rules -o /tmp/rules.json || true  # findings shouldn't block the snapshot

# 3. publish to the canonical store
mkdir -p "/gcs/$DATA/$SNAP_PATH"
cp "/tmp/snap/$DATE"/*.json "/gcs/$DATA/$SNAP_PATH/"

# 4. site data dir = recent snapshots from the bucket + fresh scans.json/rules.json
DD=dist/data
mkdir -p "$DD"
dates=$(ls "/gcs/$DATA/snapshots/" | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' | sort -r)
echo "$dates" | head -n "$KEEP_DEPLOYED" | while read -r d; do
  mkdir -p "$DD/$d" && cp "/gcs/$DATA/snapshots/$d"/*.json "$DD/$d/"
done
python3 - "$DD" <<'EOF'
import json, re, sys
from pathlib import Path
dd = Path(sys.argv[1])
dates = sorted((p.name for p in dd.iterdir() if p.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name)), reverse=True)
(dd / "scans.json").write_text(json.dumps(dates) + "\n")
print("scans.json:", dates)
EOF
cp /tmp/rules.json "$DD/rules.json" 2>/dev/null || true

if [ "${PUBLISH:-1}" != "1" ]; then
  echo "PUBLISH=0 — snapshot written to gs://$DATA/$SNAP_PATH; site untouched" >&2
elif [ -n "${CLOUDFLARE_API_TOKEN:-}" ]; then
  wrangler pages deploy dist --project-name oa-gcs-usage --branch main
else
  echo "WARN: CLOUDFLARE_API_TOKEN not set — snapshot published to gs://$DATA but site NOT deployed" >&2
fi
echo "SNAPSHOT-JOB-DONE $DATE"

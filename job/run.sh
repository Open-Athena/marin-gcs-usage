#!/usr/bin/env bash
# Daily snapshot job (GCP Batch). Chain:
#   1. DIY fan-out: list all 6 marin-* buckets ourselves (one Batch task per
#      bucket) → canonical listing parquet under listing/<date>/<bucket>/
#   2. webdata: aggregate the listings (+ attribution) into tree/age/meta JSONs
#   3. publish snapshot JSONs to the data bucket (canonical store)
#
# The live site reads snapshots straight from the bucket (see
# site/functions/data/[[path]].ts), so the job no longer builds or deploys a
# site data dir — publishing to the bucket is the whole "publish" step.
#
# Buckets are mounted via GCS FUSE at /gcs/<bucket> (DuckDB needs local paths).
# Env: SNAPSHOT_DATE (default today UTC), DATA_BUCKET, DUCKDB_MEM,
# SNAP_PATH (default snapshots/<date>).
# Parallel/experimental runs: override SNAP_PATH so they write to their own
# bucket path (the live site only reads snapshots/<date>/).
set -euxo pipefail

DATE=${SNAPSHOT_DATE:-$(date -u +%F)}
DATA=${DATA_BUCKET:-oa-gcs-usage-dvx}
SNAP_PATH=${SNAP_PATH:-snapshots/$DATE}

cd /app

# Scheduled retry attempts set NOP_IF_PUBLISHED=1: exit quietly when the
# snapshot already exists (an earlier attempt won). Manual runs leave it
# unset so intentional re-runs always proceed.
if [ "${NOP_IF_PUBLISHED:-0}" = "1" ] && [ -f "/gcs/$DATA/$SNAP_PATH/meta.json" ]; then
  echo "SNAPSHOT-JOB-NOP $DATE (already published)"
  exit 0
fi

# 1. List every bucket ourselves via a fan-out Batch job (one task per bucket).
# We own the listing end-to-end — no dependency on SII inventory reports, which
# generated at scattered times (02:26-13:00 UTC), lagged ~30h on day-1, and
# didn't exist at all for us-central2. Tasks reuse completed listings, so
# re-runs only fill gaps. If the fan-out fails or any bucket lacks a completed
# listing, abort — better no snapshot than a silently incomplete one (a dropped
# bucket like central2 is a ~1000 TB hole).
FLEET=(marin-us-central2 marin-eu-west4 marin-us-central1 marin-us-east5 marin-us-east1 marin-us-west4)
# Listing node size/parallelism are runtime config: set LISTING_MACHINE /
# LISTING_PROCS / LISTING_WORKERS on the snapshot job's env (batch-submit.sh or
# the scheduler body) to resize without rebuilding — unset falls back to the CLI
# defaults. computeResource is derived from the machine, so any size fits.
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

# 3. publish to the canonical store — the live site reads these directly
# (site/functions/data/[[path]].ts), so no site rebuild/deploy is needed.
mkdir -p "/gcs/$DATA/$SNAP_PATH"
cp "/tmp/snap/$DATE"/*.json "/gcs/$DATA/$SNAP_PATH/"
# attribution rules: the single latest copy the /data/rules.json function serves
cp /tmp/rules.json "/gcs/$DATA/snapshots/rules.json" 2>/dev/null || true

# Post the daily usage digest to Slack (only when a transport is configured, so
# experimental / local runs stay quiet — only the scheduled job sets it). The
# alert cmd prefers chat.postMessage (SLACK_BOT_TOKEN + SLACK_CHANNEL → per-message
# avatar), falling back to SLACK_WEBHOOK (static icon). A failed digest never
# fails the snapshot.
if { [ -n "${SLACK_BOT_TOKEN:-}" ] && [ -n "${SLACK_CHANNEL:-}" ]; } || [ -n "${SLACK_WEBHOOK:-}" ]; then
  gcs-usage alert -d "$DATE" -r "gs://$DATA/snapshots" \
    ${GCS_ALERT_CEILING_TB:+-c "$GCS_ALERT_CEILING_TB"} \
    ${GCS_ALERT_SPIKE_PCT:+-s "$GCS_ALERT_SPIKE_PCT"} \
    || echo "WARN: usage-digest (alert) step failed" >&2
else
  echo "no Slack transport (SLACK_BOT_TOKEN+SLACK_CHANNEL or SLACK_WEBHOOK) — skipping usage digest" >&2
fi

echo "SNAPSHOT-JOB-DONE $DATE"

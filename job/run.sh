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

# Access-log ingest (incremental, watermarked — specs/access-logs-and-cost.md
# § Productionize). Runs on every scheduled attempt, including ones where the
# snapshot below NOPs, so read-recency ("atime") stays ~6h fresh. A failure
# never blocks the snapshot (it self-heals next attempt via the watermark).
# ACCESS_ONLY=1 = ingest-only job (e.g. the backlog backfill); SKIP_ACCESS=1
# opts out entirely (e.g. REPROC runs that only re-aggregate).
# Default the ingest's DuckDB cap here (not just batch-submit.sh) so runs
# launched from a stale scheduler template don't fall back to the laptop-sized
# 8GB CLI default — that OOM'd the 2026-08-23 daily run's parquet write.
# 48GB is safe: ingest runs *before* the webdata step (sequential, not
# concurrent) on a 128G node, and a row-heavy chunk blew the earlier 24GB cap.
export DUCKDB_MEM_ACCESS=${DUCKDB_MEM_ACCESS:-48GB}
if [ "${SKIP_ACCESS:-0}" != "1" ]; then
  if ! gcs-usage access ingest ${ACCESS_ARGS:-}; then
    if [ "${ACCESS_ONLY:-0}" = "1" ]; then exit 1; fi
    echo "WARN: access ingest failed (snapshot continues; watermark self-heals next run)" >&2
  fi
fi
if [ "${ACCESS_ONLY:-0}" = "1" ]; then
  echo "ACCESS-JOB-DONE"
  exit 0
fi

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
# bucket like central2 is a huge hole).
FLEET=(marin-us-central2 marin-eu-west4 marin-us-central1 marin-us-east5 marin-us-east1 marin-us-west4)
# Listing node size/parallelism are runtime config: set LISTING_MACHINE /
# LISTING_PROCS / LISTING_WORKERS on the snapshot job's env (batch-submit.sh or
# the scheduler body) to resize without rebuilding — unset falls back to the CLI
# defaults. computeResource is derived from the machine, so any size fits.
# REPROC=1 re-aggregates an existing date from its already-archived listing/
# (e.g. to re-apply an identities.yaml change): skip the fan-out entirely and
# just verify the listings are present. The digest is also suppressed below.
if [ "${REPROC:-0}" != "1" ]; then
  LZ=()
  [ -n "${LISTING_MACHINE:-}" ] && LZ+=(-m "$LISTING_MACHINE")
  [ -n "${LISTING_PROCS:-}" ] && LZ+=(-P "$LISTING_PROCS")
  [ -n "${LISTING_WORKERS:-}" ] && LZ+=(-w "$LISTING_WORKERS")
  gcs-usage job submit-listing -d "$DATE" -W "${LZ[@]}"
fi
G=()
for b in "${FLEET[@]}"; do
  if [ -f "/gcs/$DATA/listing/$DATE/$b/_SUCCESS.json" ]; then G+=("/gcs/$DATA/listing/$DATE/$b/*.parquet")
  else echo "ERROR: no completed $DATE listing for $b — aborting snapshot" >&2; exit 1; fi
done
AG=("/gcs/$DATA/attr/attribution-2026-07-20.parquet" "/gcs/$DATA/attr/attribution-wandb.parquet")
# Access-log layer-2a shards (read-recency join) — optional until the first
# ingest lands them; webdata runs without -x when none exist yet.
XG="/gcs/$DATA/access/agg/*/*.parquet"
HAVE_ACCESS=0
compgen -G "$XG" > /dev/null && HAVE_ACCESS=1

# With STAGE_DIR set (a local-SSD mount on Batch), copy all inputs there once
# and point webdata at the local copies — gcsfuse reads are ~20-50 MB/s and
# webdata makes several passes, so local NVMe scans win by an order of magnitude.
# DuckDB spill goes to the same disk.
if [ -n "${STAGE_DIR:-}" ]; then
  SG=("${G[@]}" "${AG[@]}")
  [ "$HAVE_ACCESS" = "1" ] && SG+=("$XG")
  gcs-usage stage -o "$STAGE_DIR" "${SG[@]}"
  export DUCKDB_TMP=${DUCKDB_TMP:-$STAGE_DIR/.duckdb-tmp}
  mkdir -p "$DUCKDB_TMP"
fi
loc() { if [ -n "${STAGE_DIR:-}" ]; then echo "$STAGE_DIR/${1#/gcs/}"; else echo "$1"; fi; }
L=(); for g in "${G[@]}"; do L+=(-l "$(loc "$g")"); done
A=(); for a in "${AG[@]}"; do A+=(-a "$(loc "$a")"); done
X=(); [ "$HAVE_ACCESS" = "1" ] && X+=(-x "$(loc "$XG")")

gcs-usage webdata -d "$DATE" "${L[@]}" "${A[@]}" "${X[@]}" -o "/tmp/snap/$DATE"
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
if [ "${REPROC:-0}" = "1" ]; then
  echo "REPROC — skipping usage digest" >&2
elif { [ -n "${SLACK_BOT_TOKEN:-}" ] && [ -n "${SLACK_CHANNEL:-}" ]; } || [ -n "${SLACK_WEBHOOK:-}" ]; then
  gcs-usage alert -d "$DATE" -r "gs://$DATA/snapshots" \
    ${GCS_ALERT_CEILING_TB:+-c "$GCS_ALERT_CEILING_TB"} \
    ${GCS_ALERT_SPIKE_PCT:+-s "$GCS_ALERT_SPIKE_PCT"} \
    || echo "WARN: usage-digest (alert) step failed" >&2
else
  echo "no Slack transport (SLACK_BOT_TOKEN+SLACK_CHANNEL or SLACK_WEBHOOK) — skipping usage digest" >&2
fi

echo "SNAPSHOT-JOB-DONE $DATE"

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

# Failure alerting: any command dying under `set -e` posts to Slack before the
# job exits — otherwise silence in #gcs-usage is the only failure signal (the
# success digest is the very last step, so a hard failure posts nothing; both
# 2026-08-28 incidents went unnoticed this way). Uses the same transport gating
# as the digest; `set +x` first because the curl carries the bot token, which
# xtrace would otherwise echo into Cloud Logging.
fail_alert() {
  local rc=$1 line=$2 cmd=$3
  { set +x; } 2>/dev/null
  local msg="❌ \`gcs-usage\` snapshot job failed ($DATE): \`${cmd}\` exited $rc at run.sh:$line"
  [ -n "${BATCH_JOB_UID:-}" ] && msg+=$'\n'"<https://console.cloud.google.com/logs/query;query=labels.job_uid%3D%22$BATCH_JOB_UID%22?project=oa-internal-450019|task logs>"
  if [ -n "${SLACK_BOT_TOKEN:-}" ] && [ -n "${SLACK_CHANNEL:-}" ]; then
    curl -sS -X POST https://slack.com/api/chat.postMessage \
      -H "Authorization: Bearer $SLACK_BOT_TOKEN" -H 'Content-type: application/json; charset=utf-8' \
      -d "$(python3 -c 'import json,sys; print(json.dumps({"channel": sys.argv[1], "text": sys.argv[2]}))' "$SLACK_CHANNEL" "$msg")" >/dev/null || true
  elif [ -n "${SLACK_WEBHOOK:-}" ]; then
    curl -sS -X POST "$SLACK_WEBHOOK" -H 'Content-type: application/json' \
      -d "$(python3 -c 'import json,sys; print(json.dumps({"text": sys.argv[1]}))' "$msg")" >/dev/null || true
  fi
}
trap 'fail_alert $? $LINENO "$BASH_COMMAND"' ERR

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
# 48GB is safe: ingest still completes before the webdata step (the barrier
# below), and it overlaps only the listing fan-out — which runs on a *separate*
# node, leaving the orchestrator's 128G free for ingest. A row-heavy chunk blew
# the earlier 24GB cap.
export DUCKDB_MEM_ACCESS=${DUCKDB_MEM_ACCESS:-48GB}
# ACCESS_ONLY (backlog backfill): serial ingest, then exit — no snapshot.
if [ "${ACCESS_ONLY:-0}" = "1" ]; then
  gcs-usage access ingest ${ACCESS_ARGS:-} || exit 1
  echo "ACCESS-JOB-DONE"
  exit 0
fi

# Scheduled retry attempts set NOP_IF_PUBLISHED=1: exit quietly when the
# snapshot already exists (an earlier attempt won). Manual runs leave it
# unset so intentional re-runs always proceed. A NOP retry still ingests access
# logs (keeps read-recency ~6h fresh) — serially, since no listing follows it.
if [ "${NOP_IF_PUBLISHED:-0}" = "1" ] && [ -f "/gcs/$DATA/$SNAP_PATH/meta.json" ]; then
  [ "${SKIP_ACCESS:-0}" = "1" ] || gcs-usage access ingest ${ACCESS_ARGS:-} \
    || echo "WARN: access ingest failed (watermark self-heals next run)" >&2
  echo "SNAPSHOT-JOB-NOP $DATE (already published)"
  exit 0
fi

# Real snapshot run: kick access ingest off in the BACKGROUND so it overlaps the
# listing fan-out below — both are ~40 min and independent (only `stage` reads
# the ingest's access/agg shards, gated on the `wait` barrier before staging).
# Was serial (ingest THEN listing), putting ~37 min squarely on the critical
# path; now it hides under the listing's wall time. Non-fatal: a failure is
# caught at the barrier and the watermark self-heals next run.
ACCESS_PID=""
if [ "${SKIP_ACCESS:-0}" != "1" ] && [ "${REPROC:-0}" != "1" ]; then
  gcs-usage access ingest ${ACCESS_ARGS:-} & ACCESS_PID=$!
fi
SECONDS=0   # phase-timing baseline (see PHASE markers below)

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
echo "PHASE listing-fanout: ${SECONDS}s (wall)" >&2

# Barrier: the concurrent access ingest must finish before we stage (stage reads
# its access/agg shards) and before HAVE_ACCESS is computed just below. Non-fatal.
if [ -n "$ACCESS_PID" ]; then
  wait "$ACCESS_PID" || echo "WARN: access ingest failed (snapshot continues; watermark self-heals next run)" >&2
fi
echo "PHASE access-ingest: ${SECONDS}s (wall, overlapped the listing)" >&2
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

# Layer-2 dir-cache: attribution-independent per-dir rollups, written on the
# first aggregation of a date and reused by any re-attribution run (REPROC,
# ledger refreshes) — those then skip the 595M-row object scans entirely.
# Colocated with the listing (immutable per date, same lifecycle).
gcs-usage webdata -d "$DATE" "${L[@]}" "${A[@]}" "${X[@]}" -o "/tmp/snap/$DATE" \
  -c "/gcs/$DATA/listing/$DATE/dir-cache" \
  -P "/gcs/$DATA/listing/$DATE/path-index.parquet"
gcs-usage rules -o /tmp/rules.json || true  # findings shouldn't block the snapshot
echo "PHASE webdata+stage: ${SECONDS}s (wall)" >&2

# 3. publish to the canonical store — the live site reads these directly
# (site/functions/data/[[path]].ts), so no site rebuild/deploy is needed.
mkdir -p "/gcs/$DATA/$SNAP_PATH"
cp "/tmp/snap/$DATE"/*.json "/gcs/$DATA/$SNAP_PATH/"
# attribution rules: the single latest copy the /data/rules.json function serves
cp /tmp/rules.json "/gcs/$DATA/snapshots/rules.json" 2>/dev/null || true
echo "PHASE publish: ${SECONDS}s (wall)" >&2

# Footer-in-D1: sync the path-index parquet footer into the site's D1 so the
# reader skips the cold-isolate footer parse (specs/path-agnostic-serving.md
# §2.1). Needs CLOUDFLARE_API_TOKEN (D1 write) + CLOUDFLARE_ACCOUNT_ID — set as
# Batch secretVariables; without them the site falls back to parsing the footer,
# so this never blocks the snapshot. Disable xtrace for the WHOLE block first:
# even the `[ -n "$CLOUDFLARE_API_TOKEN" ]` test echoes the token under `set -x`.
{ set +x; } 2>/dev/null
if [ -n "${CLOUDFLARE_API_TOKEN:-}" ] && [ -n "${CLOUDFLARE_ACCOUNT_ID:-}" ]; then
  gcs-usage index-sync -d "/gcs/$DATA/listing/$DATE" "$DATE" \
    || echo "WARN: index-sync failed (site falls back to footer parse)" >&2
else
  echo "WARN: no CLOUDFLARE_API_TOKEN/ACCOUNT_ID — skipping index-sync (site parses the footer)" >&2
fi
set -x

# Cross-scan size index for the site's per-subpath "size over time" chart
# (specs/size-over-time.md case 1). Re-folds every archived tree into a single
# snapshots/series.json — scans are immutable, so this is effectively
# append-only (only the new date's column changes). Reads/writes over the same
# FUSE mount as the snapshot cp above. A failure never blocks the snapshot: the
# chart just falls back to the fleet total, and the next run self-heals (it
# always regenerates the whole index).
# Ledger export for the fate-over-time replay (specs/lens-aware-time-series.md):
# needs an agent token (GCS_USAGE_TOKEN, e.g. via Secret Manager); without one
# the series omits `fate` and the To-do burn-down chart hides.
SER_A=()
if [ -n "${GCS_USAGE_TOKEN:-}" ]; then
  # subshell +x: the Authorization header must not hit the xtrace log
  if ( { set +x; } 2>/dev/null; curl -fsS -H "Authorization: Bearer $GCS_USAGE_TOKEN" \
      "${GCS_USAGE_URL:-https://gcs.oa.dev}/api/actions" -o /tmp/actions.json ); then
    SER_A=(-a /tmp/actions.json)
  else
    echo "WARN: actions export failed — series omits fate" >&2
  fi
fi
gcs-usage series "${SER_A[@]}" -r "/gcs/$DATA/snapshots" -o "/gcs/$DATA/snapshots/series.json" \
  || echo "WARN: series-index step failed (size chart falls back to fleet total)" >&2

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

echo "PHASE total: ${SECONDS}s (wall)" >&2
echo "SNAPSHOT-JOB-DONE $DATE"

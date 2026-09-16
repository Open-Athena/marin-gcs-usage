#!/usr/bin/env bash
# CoreWeave S3 scan job (GCP Batch). Chain:
#   1. bulk-list  s3://<bucket> via the CAIOS S3-compatible endpoint
#   2. import     listing shards -> canonical layer-2 parquet
#   3. webdata    layer-2 -> the site's tree/age/meta JSONs (the diff is
#                 served live from the index tiers — step 4b)
#   4. publish    JSONs to gs://$DATA/snapshots/cw/<id>/
#
# Why GCP Batch and not AWS Batch: the output lands in GCS (the site's Pages
# Function lists `snapshots/cw/` straight out of the bucket), the image and
# service account already exist here, and the listing is CoreWeave *egress*
# either way -- running it in AWS would just add a second cloud to operate.
#
# Sizing note: this job is far lighter than the GCS one. A full 92M-object
# scan measured 26 min / 1.1 GB RSS for the listing and 3:39 / 1.8 GB for the
# import, so it needs neither highmem nor local-SSD spill -- an n2-standard-8
# with a plain boot disk is enough. The bytes that matter are transient listing
# shards (~10 GB), not DuckDB spill.
#
# Env: CW_BUCKET, CW_ENDPOINT, DATA_BUCKET, SNAP_ID (default: UTC YYYY-MM-DDTHHMM
# at listing start), LISTING_PROCS/WORKERS, AWS_ACCESS_KEY_ID/SECRET_ACCESS_KEY
# (injected from Secret Manager by cw-batch-submit.sh).
set -euxo pipefail

BUCKET=${CW_BUCKET:-marin-us-east-02a}
ENDPOINT=${CW_ENDPOINT:-https://cwobject.com}
DATA=${DATA_BUCKET:-oa-gcs-usage-dvx}
PROCS=${LISTING_PROCS:-8}
WORKERS=${LISTING_WORKERS:-8}
# Snapshot ids are sub-daily: the bucket can move >40 TiB between morning and
# evening during a cleanup push, so a date-only id would silently overwrite.
SNAP_ID=${SNAP_ID:-$(date -u +%Y-%m-%dT%H%M)}
DATE=${SNAP_ID%%T*}

# Failure alerting: any command dying under `set -e` posts to Slack before the
# job exits (silence was the only failure signal — the 8/26–8/28 crash-loop on
# a missing script went unnoticed for two days). Activates once a transport is
# configured on the scheduler body (SLACK_BOT_TOKEN+SLACK_CHANNEL secret/env,
# or SLACK_WEBHOOK); `set +x` first so the token never hits the xtrace log.
# Alerts go to SLACK_ALERT_CHANNEL (#gcs-usage-alerts, as the GCS job) when
# set, so they don't land in the #cw-s3-usage digest thread's channel.
fail_alert() {
  local rc=$1 line=$2 cmd=$3
  { set +x; } 2>/dev/null
  local msg="❌ CoreWeave scan job failed ($SNAP_ID): \`${cmd}\` exited $rc at cw-run.sh:$line"
  [ -n "${BATCH_JOB_UID:-}" ] && msg+=$'\n'"<https://console.cloud.google.com/logs/query;query=labels.job_uid%3D%22$BATCH_JOB_UID%22?project=oa-internal-450019|task logs>"
  local chan="${SLACK_ALERT_CHANNEL:-${SLACK_CHANNEL:-}}"
  if [ -n "${SLACK_BOT_TOKEN:-}" ] && [ -n "$chan" ]; then
    curl -sS -X POST https://slack.com/api/chat.postMessage \
      -H "Authorization: Bearer $SLACK_BOT_TOKEN" -H 'Content-type: application/json; charset=utf-8' \
      -d "$(python3 -c 'import json,sys; print(json.dumps({"channel": sys.argv[1], "text": sys.argv[2]}))' "$chan" "$msg")" >/dev/null || true
  elif [ -n "${SLACK_WEBHOOK:-}" ]; then
    curl -sS -X POST "$SLACK_WEBHOOK" -H 'Content-type: application/json' \
      -d "$(python3 -c 'import json,sys; print(json.dumps({"text": sys.argv[1]}))' "$msg")" >/dev/null || true
  fi
}
trap 'fail_alert $? $LINENO "$BASH_COMMAND"' ERR

WORK=${WORK_DIR:-/stage/cw-$SNAP_ID}
mkdir -p "$WORK"
cd /app

# The lister opens a socket per concurrent range; the default 1024 soft limit
# is well under PROCS*WORKERS*keep-alive and surfaces as opaque connection
# resets partway through a multi-hour listing.
ulimit -n "$(ulimit -Hn)" 2>/dev/null || ulimit -n 65536

# 1. Listing. `-x clear` starts from a clean output dir so a retried task never
# merges shards from a half-finished previous attempt.
disk-tree bulk-list -a "s3://$BUCKET" -E "$ENDPOINT" \
  -o "$WORK/listing" -P "$PROCS" -w "$WORKERS" -x clear

# 2. Layer-2. `-j` is CPU-bound over the k-way merge; the shards' row groups are
# bounded at write time (see find/bulk.py) so each merge source decodes one 64K
# group rather than the whole shard -- that bound is what keeps this in ~2 GB.
env DISK_TREE_ROOT="$WORK/l2" \
  disk-tree import -e stream -j "${IMPORT_JOBS:-8}" -m -p storage_class_id \
    -s s3 -t "${DATE}T00:00:00+00:00" -l "$WORK/listing/shard-*.parquet" -b "$BUCKET"

L2=$(ls "$WORK"/l2/scans/*.parquet | head -1)

# 3. Site JSONs.
python job/cw-webdata.py "$L2" "$WORK/web" -b "$BUCKET" -l "Marin CoreWeave" -a "$DATE"

# 4. Publish. Written last and all at once: the site's scan list is derived by
# listing this prefix, so a partially-uploaded snapshot would show up in the
# dropdown as a broken entry.
DEST="/gcs/$DATA/snapshots/cw/$SNAP_ID"
mkdir -p "$DEST"
cp "$WORK"/web/*.json "$DEST/"
# The bucket's lifecycle rules in force at this scan (Marin's tmp/ttl TTLs, the
# abort-MPU rule, the noncurrent-version GC): snapshotted next to the scan so
# the site can show them and later infer their effects. `job/cw-lifecycle.json`
# is the intended state (`dt-cloud lifecycle diff|push`); a pull never fails
# the scan.
dt-cloud lifecycle pull -b "$BUCKET" -o "$DEST/lifecycle.json" || echo "WARN: lifecycle pull failed" >&2

# Keep the canonical layer-2 parquet too -- it's the input to every ad-hoc
# question ("what grew?", "what's idle?") that the JSONs can't answer.
mkdir -p "/gcs/$DATA/cw-l2/$SNAP_ID"
cp "$L2" "/gcs/$DATA/cw-l2/$SNAP_ID/$BUCKET.parquet"

# 4b. Index tiers (specs/view-serving.md, ported from gcs): the site's
# /api/subtree, /api/diff and /api/series read row-group-pruned ranges of
# `path-index[-coarse<E>].parquet` — the layer-2's dir rows in the site's
# column contract, 8k-row groups, plus coarse tiers by absolute byte floor.
# Each run writes its tiers under a fresh generation dir and never overwrites
# a file D1 points at; `index-sync` lands the footers in D1 and flips the
# pointer last, so a reader sees the old complete set or the new one.
GEN=${GEN:-$(date -u +%Y%m%dT%H%M%SZ)}
INDEX_KEY="cw-l2/$SNAP_ID/index/$GEN"
dt-cloud index-write -b "$BUCKET" -m "${DUCKDB_MEM:-16GB}" -t "${IMPORT_JOBS:-8}" -o "$WORK/index" "$L2"
mkdir -p "/gcs/$DATA/$INDEX_KEY"
cp "$WORK"/index/path-index*.parquet "/gcs/$DATA/$INDEX_KEY/"
if [ -n "${CLOUDFLARE_API_TOKEN:+set}" ] && [ -n "${CLOUDFLARE_ACCOUNT_ID:-}" ]; then  # `:+set`: xtrace must not print the token
  dt-cloud index-sync -d "/gcs/$DATA/$INDEX_KEY" -g "$GEN" -k "$INDEX_KEY" "$SNAP_ID" \
    || echo "WARN: index-sync failed (the site keeps serving the previous generation)" >&2
  dt-cloud index-gc "$SNAP_ID" || echo "WARN: index-gc failed" >&2
else
  echo "WARN: no CLOUDFLARE_API_TOKEN/ACCOUNT_ID — tiers published but not synced (scan unlisted for the index reader)" >&2
fi

# 5. Converge the monthly Shape-C digest thread in Slack (specs/cw-slack-
# digest.md): the OP + one reply per scan, into #cw-s3-usage. Only when
# SLACK_BOT_TOKEN + SLACK_CHANNEL are set — Shape C needs the Web API's
# per-message sender/avatar overrides. `dt-cloud cw-digest` also renders the
# plot into job/icons-cw/ and `wrangler pages deploy`s it to the icons Pages
# project's `cw` branch, so it needs CLOUDFLARE_* + wrangler (both in this
# image). A failed digest never fails the scan.
if [ -n "${SLACK_BOT_TOKEN:+set}" ] && [ -n "${SLACK_CHANNEL:-}" ]; then  # `:+set`: xtrace must not print the token
  dt-cloud cw-digest -r "gs://$DATA/snapshots/cw" \
    || echo "WARN: usage-digest step failed" >&2
else
  echo "no Slack bot transport (SLACK_BOT_TOKEN+SLACK_CHANNEL) — skipping usage digest" >&2
fi

echo "CW-SCAN-JOB-DONE $SNAP_ID -> gs://$DATA/snapshots/cw/$SNAP_ID"

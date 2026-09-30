#!/usr/bin/env bash
# The "meta" self-scan (GCP Batch, daily): the storage the deployments write,
# as the cw-s3 deployment's secondary store `meta` (specs/multi-store.md
# phase 3 — served at cw-s3.oa.dev/meta once `STORES_JSON` / `STORES_EXTRA`
# name it and `migrations/cw/0006` is applied). The chain is cw-run.sh's, over
# different roots and into the store's own prefixes:
#   1. bulk-list  each root: the GCS data bucket (`gcs://`, ADC as the job SA)
#                 and cw's R2 serving bucket (`r2://` via the R2 S3 endpoint)
#   2. import     listing shards -> canonical layer-2 parquet, one per root
#   3. webdata    layer-2 -> the site's tree/age/meta JSONs
#   4. publish    JSONs to snapshots/meta/<id>/, layer-2 to meta-l2/<id>/,
#                 index tiers to meta-l2/<id>/index/<gen>/ + D1 (`--store meta`),
#                 then the served subset to R2 under the same prefixes
#
# Self-reference is fine and intended: this scan lists the bucket it writes to,
# so each scan sees the previous meta scan's own output (one scan late, and a
# rounding error next to gcs's 2.4 TiB `listing/` + `access/`).
#
# Not here (vs cw-run.sh): the lifecycle snapshot (no rules on these buckets to
# show), the Slack digest thread (no audience for a daily "our storage" post —
# the treemap is the report), the over-time groups (per-scan reads suffice at
# one scan a day until the store has a year of history), and the cache warm-up
# (`dt-cloud warm-cache` has no `--store` yet — phase 3 open item).
#
# Env: META_ROOTS (space-separated `<scheme>://<bucket>`; the first is the
# store's primary root), DATA_BUCKET, SNAP_ID (default: UTC YYYY-MM-DDTHHMM at
# listing start — the same id shape as the cw scan, which the site's scan
# parsing expects), LISTING_PROCS/WORKERS, IMPORT_JOBS; the R2 creds as both
# R2_* (publish-r2) and AWS_* (the `r2://` lister) — cw-meta-submit.sh injects
# them from Secret Manager.
set -euxo pipefail

# Strip surrounding whitespace off every secret env var before anything reads
# it (a Secret Manager version made with `echo … | --data-file=-` carries a
# trailing newline — see cw-run.sh). xtrace off for the block.
{ set +x; } 2>/dev/null
for n in AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY R2_ENDPOINT R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY \
         CLOUDFLARE_API_TOKEN GCS_USAGE_TOKEN SLACK_BOT_TOKEN; do
  if [ -n "${!n+set}" ]; then
    v=${!n}; v=${v#"${v%%[![:space:]]*}"}; v=${v%"${v##*[![:space:]]}"}
    export "$n=$v"
  fi
done
unset n v
set -x

STORE=meta
# Deployment config for the shared CLI (cloud/src/dt_cloud/{index_footer,publish}.py):
# the same D1 + site as the primary; this store's own snapshot subdir + layer-2 prefix.
export D1_DB_ID=${D1_DB_ID:-7f1e1326-b879-4ecd-8621-846621c24f36}   # oa-cw-s3-usage-db (site/wrangler.toml)
export D1_DB_NAME=${D1_DB_NAME:-oa-cw-s3-usage-db}
export INDEX_VARIANTS=${INDEX_VARIANTS:-path}
export SITE_URL=${SITE_URL:-https://cw-s3.oa.dev}
export SNAPSHOTS_SUBDIR=${SNAPSHOTS_SUBDIR:-$STORE}                  # snapshots/meta/ (`STORES_JSON` says the same)
export LAYER2_PREFIX=${LAYER2_PREFIX:-"$STORE-l2/{scan}/"}          # this store's layer-2 dir (`dt-cloud publish-r2 -l`)

ROOTS=${META_ROOTS:-"gcs://oa-gcs-usage-dvx r2://oa-cw-s3-usage-index"}
DATA=${DATA_BUCKET:-oa-gcs-usage-dvx}
PROCS=${LISTING_PROCS:-4}
WORKERS=${LISTING_WORKERS:-4}
SNAP_ID=${SNAP_ID:-$(date -u +%Y-%m-%dT%H%M)}
DATE=${SNAP_ID%%T*}

# Failure alerting, as cw-run.sh: any command dying under `set -e` posts to
# Slack (SLACK_ALERT_CHANNEL, #gcs-usage-alerts) before the job exits.
fail_alert() {
  local rc=$1 line=$2 cmd=$3
  { set +x; } 2>/dev/null
  local msg="❌ meta self-scan job failed ($SNAP_ID): \`${cmd}\` exited $rc at cw-meta-run.sh:$line"
  [ -n "${BATCH_JOB_UID:-}" ] && msg+=$'\n'"<https://console.cloud.google.com/logs/query;query=labels.job_uid%3D%22$BATCH_JOB_UID%22?project=oa-internal-450019|task logs>"
  local chan="${SLACK_ALERT_CHANNEL:-}"
  if [ -n "${SLACK_BOT_TOKEN:-}" ] && [ -n "$chan" ]; then
    curl -sS -X POST https://slack.com/api/chat.postMessage \
      -H "Authorization: Bearer $SLACK_BOT_TOKEN" -H 'Content-type: application/json; charset=utf-8' \
      -d "$(python3 -c 'import json,sys; print(json.dumps({"channel": sys.argv[1], "text": sys.argv[2]}))' "$chan" "$msg")" >/dev/null || true
  fi
}
trap 'fail_alert $? $LINENO "$BASH_COMMAND"' ERR

WORK=${WORK_DIR:-/stage/meta-$SNAP_ID}
mkdir -p "$WORK"
cd /app
ulimit -n "$(ulimit -Hn)" 2>/dev/null || ulimit -n 65536

# 1 + 2, per root: listing, then layer-2 (one listing dir and one layer-2 root
# per bucket; the bucket becomes the path's first segment at index/webdata
# time, so the two roots sit side by side under the store root). `-x clear`
# restarts a retried task from a clean output dir. The R2 root needs the S3
# endpoint (`R2_ENDPOINT`, the account's `…r2.cloudflarestorage.com`) and the
# AWS_* creds; the GCS root lists via ADC.
SRC=()      # <bucket>=<layer-2>, in META_ROOTS order
BUCKETS=()  # bare bucket names, same order
for root in $ROOTS; do
  scheme=${root%%://*}; b=${root#*://}
  BUCKETS+=("$b")
  args=(-a "$root" -o "$WORK/listing/$b" -P "$PROCS" -w "$WORKERS" -x clear)
  [ "$scheme" = r2 ] && args+=(-E "$R2_ENDPOINT")
  disk-tree bulk-list "${args[@]}"
  env DISK_TREE_ROOT="$WORK/l2/$b" \
    disk-tree import -e stream -j "${IMPORT_JOBS:-4}" -m -p storage_class_id \
      -s "$scheme" -t "${DATE}T00:00:00+00:00" -l "$WORK/listing/$b/shard-*.parquet" -b "$b"
  SRC+=("$b=$(ls "$WORK/l2/$b"/scans/*.parquet | head -1)")
done

# 3. Site JSONs: the store root wraps one node per root bucket.
python job/cw-webdata.py "$WORK/web" "${SRC[@]}" -l "our storage" -a "$DATE"

# 4. Publish. JSONs last-and-at-once (the site's scan list is derived by
# listing this prefix); the canonical layer-2 parquets beside them.
DEST="/gcs/$DATA/snapshots/$STORE/$SNAP_ID"
mkdir -p "$DEST"
cp "$WORK"/web/*.json "$DEST/"
mkdir -p "/gcs/$DATA/$STORE-l2/$SNAP_ID"
for s in "${SRC[@]}"; do cp "${s#*=}" "/gcs/$DATA/$STORE-l2/$SNAP_ID/${s%%=*}.parquet"; done

# 4b. Index tiers, footers to D1 under this store (`--store meta`: rows are
# `store = 'meta'`, variant `meta:path`, never visible to the primary's
# queries; they need `migrations/cw/0006` applied, and fail loudly otherwise).
GEN=${GEN:-$(date -u +%Y%m%dT%H%M%SZ)}
INDEX_KEY="$STORE-l2/$SNAP_ID/index/$GEN"
dt-cloud index-write -b "${BUCKETS[0]}" -m "${DUCKDB_MEM:-8GB}" -t "${IMPORT_JOBS:-4}" -o "$WORK/index" "${SRC[@]}"
mkdir -p "/gcs/$DATA/$INDEX_KEY"
cp "$WORK"/index/*.parquet "/gcs/$DATA/$INDEX_KEY/"
if [ -n "${CLOUDFLARE_API_TOKEN:+set}" ] && [ -n "${CLOUDFLARE_ACCOUNT_ID:-}" ]; then  # `:+set`: xtrace must not print the token
  dt-cloud index-sync -s "$STORE" -d "/gcs/$DATA/$INDEX_KEY" -g "$GEN" -k "$INDEX_KEY" "$SNAP_ID" \
    || echo "WARN: index-sync failed (the site keeps serving the previous generation)" >&2
  dt-cloud index-gc -s "$STORE" "$SNAP_ID" || echo "WARN: index-gc failed" >&2
else
  echo "WARN: no CLOUDFLARE_API_TOKEN/ACCOUNT_ID — tiers published but not synced (scan unlisted for the index reader)" >&2
fi

# 4c. The served subset to R2 (the same bucket the primary serves from, under
# this store's prefixes — `SNAPSHOTS_SUBDIR` / `LAYER2_PREFIX` above).
if [ -n "${R2_ENDPOINT:+set}" ] && [ -n "${R2_BUCKET:-}" ]; then  # `:+set`: xtrace must not print the creds
  dt-cloud publish-r2 -L "$SNAP_ID" \
    || echo "WARN: publish-r2 failed for $SNAP_ID (the site keeps serving GCS)" >&2
else
  echo "no R2_ENDPOINT/R2_BUCKET — skipping publish-r2" >&2
fi

echo "META-SCAN-JOB-DONE $SNAP_ID -> gs://$DATA/snapshots/$STORE/$SNAP_ID"

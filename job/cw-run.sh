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
# Env: SCAN_BUCKETS (space-separated, the first is the primary — the quota bucket,
# the sweep/lifecycle default; specs/done/cw-multi-bucket.md), SWEEP_BUCKET (the primary;
# defaults to SCAN_BUCKETS' first), SWEEP_S3_ENDPOINT (each also read under its old
# name, CW_BUCKETS / CW_BUCKET / CW_ENDPOINT), DATA_BUCKET, SNAP_ID (default: UTC
# YYYY-MM-DDTHHMM at listing start), LISTING_PROCS/WORKERS,
# AWS_ACCESS_KEY_ID/SECRET_ACCESS_KEY (injected from Secret Manager by
# cw-batch-submit.sh).
set -euxo pipefail

# Secrets arrive from Secret Manager verbatim, and a version created with
# `echo … | --data-file=-` carries a trailing newline (it broke the Discord
# digest for two days, 2026-09-16/17). Strip surrounding whitespace off every
# secret env var before anything (boto, curl, wrangler, the CLI) reads it.
# xtrace off for the block: `${!n}` would print the values.
{ set +x; } 2>/dev/null
for n in AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY CLOUDFLARE_API_TOKEN DISCORD_BOT_TOKEN \
         DISCORD_GCS_USAGE_WEBHOOK SITE_TOKEN GCS_USAGE_TOKEN SLACK_BOT_TOKEN SLACK_WEBHOOK \
         CF_ACCESS_CLIENT_ID CF_ACCESS_CLIENT_SECRET; do
  if [ -n "${!n+set}" ]; then
    v=${!n}; v=${v#"${v%%[![:space:]]*}"}; v=${v%"${v##*[![:space:]]}"}
    export "$n=$v"
  fi
done
unset n v
# The site token's old name (`GCS_USAGE_TOKEN`) is what the scheduler body still
# passes until its next `pulumi up`; carry it under the new one.
if [ -z "${SITE_TOKEN+set}" ] && [ -n "${GCS_USAGE_TOKEN+set}" ]; then export SITE_TOKEN=$GCS_USAGE_TOKEN; fi
unset GCS_USAGE_TOKEN
set -x

# Deployment config for the shared CLI (cloud/src/dt_cloud/{index_footer,warm}.py).
export D1_DB_ID=${D1_DB_ID:-7f1e1326-b879-4ecd-8621-846621c24f36}   # oa-cw-s3-usage-db (site/wrangler.toml)
export D1_DB_NAME=${D1_DB_NAME:-oa-cw-s3-usage-db}
export INDEX_VARIANTS=${INDEX_VARIANTS:-path}                           # no user-sorted tiers here
export SITE_URL=${SITE_URL:-https://cw-s3.oa.dev}
export SNAPSHOTS_SUBDIR=${SNAPSHOTS_SUBDIR:-cw}                          # snapshots/cw/ (site/wrangler.toml says the same)
export LAYER2_PREFIX=${LAYER2_PREFIX:-'cw-l2/{scan}/'}   # this store's layer-2 dir (`dt-cloud publish-r2 -l`; the base's is listing/{scan}/index/)
export WARM_PATHS=${WARM_PATHS:-",marin-us-east-02a,marin-us-east-02a/marin,marin-us-east-02a/tmp,marin-us-east-02a/iris,hero-checkpoints,hero-checkpoints/tmp,hero-checkpoints/marin,marin-us-east-06a,marin-us-east-06a/marin,rhoarnet-us-east-08a,marin-us-west-04a"}

# Every bucket in the scan; the primary first. The scheduler body sets only
# the primary, so the list's default here IS the deployment's.
BUCKETS=${SCAN_BUCKETS:-${CW_BUCKETS:-"marin-us-east-02a hero-checkpoints marin-us-east-06a rhoarnet-us-east-08a marin-us-west-04a"}}
BUCKET=${SWEEP_BUCKET:-${CW_BUCKET:-${BUCKETS%% *}}}
ENDPOINT=${SWEEP_S3_ENDPOINT:-${CW_ENDPOINT:-https://cwobject.com}}
export SCAN_BUCKETS=$BUCKETS SWEEP_BUCKET=$BUCKET SWEEP_S3_ENDPOINT=$ENDPOINT
unset CW_BUCKETS CW_BUCKET CW_ENDPOINT
DATA=${DATA_BUCKET:-oa-gcs-usage-dvx}
export DATA_BUCKET=$DATA
PROCS=${LISTING_PROCS:-8}
WORKERS=${LISTING_WORKERS:-8}
# Snapshot ids are sub-daily: the bucket can move >40 TiB between morning and
# evening during a cleanup push, so a date-only id would silently overwrite.
SNAP_ID=${SNAP_ID:-$(date -u +%Y-%m-%dT%H%M)}
DATE=${SNAP_ID%%T*}
GEN=${GEN:-$(date -u +%Y%m%dT%H%M%SZ)}  # this run's index generation (step 4b), stamped once at start

# Scan-run record (specs/scan-runs-ui.md): this run's phases, outputs and exit
# in D1 (`scan_runs`), shown at /scans. `dt-cloud scan-run record` never fails
# the job and writes over the D1 HTTP API (the index-sync credentials), so
# xtrace is off around it. `job/scan-runs.json` says what each phase wrote.
export SCAN_RUNS_PROFILE=${SCAN_RUNS_PROFILE:-job/scan-runs.json}
SR_ERR=""
SECONDS=0
scan_run() {
  { set +x; } 2>/dev/null
  if [ "${REPROC:-0}" != "1" ] && [ -n "${BATCH_JOB_UID:-}" ]; then
    (cd /app && dt-cloud scan-run record -g "$GEN" "$@" "$SNAP_ID")
  fi
  set -x
}
phase() { echo "PHASE $1: ${SECONDS}s (wall${2:+, $2})" >&2; scan_run -P "$1" ${2:+-q "$2"}; }
scan_run_exit() { local rc=$?; scan_run -x "$rc" ${SR_ERR:+-e "$SR_ERR"}; exit "$rc"; }

# Failure alerting: any command dying under `set -e` posts to Slack before the
# job exits (silence was the only failure signal — the 8/26–8/28 crash-loop on
# a missing script went unnoticed for two days). Activates once a transport is
# configured on the scheduler body (SLACK_BOT_TOKEN+SLACK_CHANNEL secret/env,
# or SLACK_WEBHOOK); `set +x` first so the token never hits the xtrace log.
# Alerts go to SLACK_ALERT_CHANNEL (#gcs-usage-alerts, as the GCS job) when
# set, so they don't land in the #cw-s3-usage digest thread's channel.
slack_alert() {
  local msg=$1 chan="${SLACK_ALERT_CHANNEL:-${SLACK_CHANNEL:-}}"
  if [ -n "${SLACK_BOT_TOKEN:-}" ] && [ -n "$chan" ]; then
    curl -sS -X POST https://slack.com/api/chat.postMessage \
      -H "Authorization: Bearer $SLACK_BOT_TOKEN" -H 'Content-type: application/json; charset=utf-8' \
      -d "$(python3 -c 'import json,sys; print(json.dumps({"channel": sys.argv[1], "text": sys.argv[2]}))' "$chan" "$msg")" >/dev/null || true
  elif [ -n "${SLACK_WEBHOOK:-}" ]; then
    curl -sS -X POST "$SLACK_WEBHOOK" -H 'Content-type: application/json' \
      -d "$(python3 -c 'import json,sys; print(json.dumps({"text": sys.argv[1]}))' "$msg")" >/dev/null || true
  fi
}
fail_alert() {
  local rc=$1 line=$2 cmd=$3
  { set +x; } 2>/dev/null
  SR_ERR="exit $rc: $cmd (cw-run.sh:$line)"  # the EXIT trap records it (scan_run_exit)
  local msg="❌ CoreWeave scan job failed ($SNAP_ID): \`${cmd}\` exited $rc at cw-run.sh:$line"
  [ -n "${BATCH_JOB_UID:-}" ] && msg+=$'\n'"<https://console.cloud.google.com/logs/query;query=labels.job_uid%3D%22$BATCH_JOB_UID%22?project=oa-internal-450019|task logs>"
  slack_alert "$msg"
}
trap 'fail_alert $? $LINENO "$BASH_COMMAND"' ERR
trap scan_run_exit EXIT
scan_run -S -B

# Every bucket the CAIOS key can list is either scanned or empty. A non-empty
# one outside SCAN_BUCKETS (a new zone's bucket) is billed storage the site
# would silently miss, so it alerts each run until it's added. Never fatal (an
# empty bucket can't be scanned: bulk-list writes no shards for it). CAIOS
# rejects path-style object calls (`PathStyleRequestNotAllowed`), so the client
# is virtual-hosted, as `sweep.py` / `bulk_s3.py` build theirs. A failed check
# alerts too, naming the step and the error, never a bucket list.
UNSCANNED=$(python3 - "$ENDPOINT" $BUCKETS <<'PY' || echo "check failed: python exited $?"
import sys, boto3
from botocore.config import Config
endpoint, *scanned = sys.argv[1:]
s3 = boto3.client("s3", endpoint_url=endpoint, config=Config(s3={"addressing_style": "virtual"}))
try:
    names = sorted(b["Name"] for b in s3.list_buckets()["Buckets"])
except Exception as e:
    sys.exit(print(f"check failed: ListBuckets: {type(e).__name__}: {e}"))
out = []
for n in names:
    if n in scanned:
        continue
    try:
        if s3.list_objects_v2(Bucket=n, MaxKeys=1).get("KeyCount"):
            out.append(n)
    except Exception as e:
        out.append(f"{n} (check failed: ListObjectsV2: {type(e).__name__}: {e})")
print(" ".join(out))
PY
)
phase bucket-check
if [ -n "$UNSCANNED" ]; then
  echo "WARN: unscanned-bucket check: $UNSCANNED" >&2
  { set +x; } 2>/dev/null
  slack_alert "⚠️ CoreWeave scan ($SNAP_ID): non-empty CAIOS bucket(s) not in SCAN_BUCKETS: \`$UNSCANNED\`"
  set -x
fi

WORK=${WORK_DIR:-/stage/cw-$SNAP_ID}
mkdir -p "$WORK"
cd /app

# The lister opens a socket per concurrent range; the default 1024 soft limit
# is well under PROCS*WORKERS*keep-alive and surfaces as opaque connection
# resets partway through a multi-hour listing.
ulimit -n "$(ulimit -Hn)" 2>/dev/null || ulimit -n 65536

# 1 + 2, per bucket: listing, then layer-2. One listing dir and one layer-2
# root per bucket (paths stay bucket-relative; the bucket becomes the path's
# first segment at index/webdata time). `-x clear` starts from a clean output
# dir so a retried task never merges shards from a half-finished previous
# attempt. Import: `-j` is CPU-bound over the k-way merge; the shards' row
# groups are bounded at write time (see find/bulk.py) so each merge source
# decodes one 64K group rather than the whole shard -- that bound is what
# keeps this in ~2 GB.
SRC=()  # <bucket>=<layer-2>, in SCAN_BUCKETS order
for b in $BUCKETS; do
  disk-tree bulk-list -a "s3://$b" -E "$ENDPOINT" \
    -o "$WORK/listing/$b" -P "$PROCS" -w "$WORKERS" -x clear
  phase "list $b"
  env DISK_TREE_ROOT="$WORK/l2/$b" \
    disk-tree import -e stream -j "${IMPORT_JOBS:-8}" -m -p storage_class_id \
      -s s3 -t "${DATE}T00:00:00+00:00" -l "$WORK/listing/$b/shard-*.parquet" -b "$b"
  SRC+=("$b=$(ls "$WORK/l2/$b"/scans/*.parquet | head -1)")
  phase "import $b"
done

# 3. Site JSONs: the store root wraps one node per bucket.
python job/cw-webdata.py "$WORK/web" "${SRC[@]}" -l "Marin CoreWeave" -a "$DATE"
phase webdata

# 4. Publish. Written last and all at once: the site's scan list is derived by
# listing this prefix, so a partially-uploaded snapshot would show up in the
# dropdown as a broken entry.
DEST="/gcs/$DATA/snapshots/cw/$SNAP_ID"
mkdir -p "$DEST"
cp "$WORK"/web/*.json "$DEST/"
# Each bucket's lifecycle rules in force at this scan (Marin's tmp/ttl TTLs, the
# abort-MPU rule, the primary's noncurrent-version GC): snapshotted next to the
# scan as `{<bucket>: Rules[]}` so the site can show them and later infer their
# effects. `job/cw-lifecycle.json` is the primary's intended state (`dt-cloud
# lifecycle diff|push`); a pull never fails the scan.
LB=(); for b in $BUCKETS; do LB+=(-b "$b"); done
dt-cloud lifecycle pull "${LB[@]}" -o "$DEST/lifecycle.json" || echo "WARN: lifecycle pull failed" >&2
phase publish

# Keep the canonical layer-2 parquets too -- they're the input to every ad-hoc
# question ("what grew?", "what's idle?") that the JSONs can't answer.
mkdir -p "/gcs/$DATA/cw-l2/$SNAP_ID"
for s in "${SRC[@]}"; do cp "${s#*=}" "/gcs/$DATA/cw-l2/$SNAP_ID/${s%%=*}.parquet"; done
phase layer-2

# 4b. Index tiers (specs/view-serving.md, ported from gcs): the site's
# /api/subtree, /api/diff and /api/series read row-group-pruned ranges of
# `path-index[-coarse<E>].parquet` — the layer-2's dir rows in the site's
# column contract, 8k-row groups, plus coarse tiers by absolute byte floor.
# Each run writes its tiers under a fresh generation dir and never overwrites
# a file D1 points at; `index-sync` lands the footers in D1 and flips the
# pointer last, so a reader sees the old complete set or the new one.
INDEX_KEY="cw-l2/$SNAP_ID/index/$GEN"
dt-cloud index-write -m "${DUCKDB_MEM:-16GB}" -t "${IMPORT_JOBS:-8}" -o "$WORK/index" "${SRC[@]}"
mkdir -p "/gcs/$DATA/$INDEX_KEY"
cp "$WORK"/index/*.parquet "/gcs/$DATA/$INDEX_KEY/"  # path-index + coarse tiers + age-index
phase index-write
if [ -n "${CLOUDFLARE_API_TOKEN:+set}" ] && [ -n "${CLOUDFLARE_ACCOUNT_ID:-}" ]; then  # `:+set`: xtrace must not print the token
  dt-cloud index-sync -d "/gcs/$DATA/$INDEX_KEY" -g "$GEN" -k "$INDEX_KEY" "$SNAP_ID" \
    || echo "WARN: index-sync failed (the site keeps serving the previous generation)" >&2
  dt-cloud index-gc "$SNAP_ID" || echo "WARN: index-gc failed" >&2
else
  echo "WARN: no CLOUDFLARE_API_TOKEN/ACCOUNT_ID — tiers published but not synced (scan unlisted for the index reader)" >&2
fi
phase index-sync

# 4c. Publish this scan's served subset to R2 (specs/done/r2-serving-migration.md):
# the snapshot JSONs + the layer-2 dir (parquets, index tiers, manifests) the
# site reads through its `STORE_*` seam, colocated with the Worker instead of
# fetched cross-provider from GCS. Idempotent (size + md5), so a re-run or a
# backfill over old scans only moves what's missing. Must precede the warm-up
# below: once the site serves from R2, the warm has to find the scan there.
# Skipped without the R2 env (the `cw-s3-r2-*` Secret Manager secrets +
# `R2_BUCKET`, from `cw-batch-submit.sh`); the site keeps serving GCS then.
if [ -n "${R2_ENDPOINT:+set}" ] && [ -n "${R2_BUCKET:-}" ]; then  # `:+set`: xtrace must not print the creds
  # `-L`: the canonical per-bucket listings stay in GCS only (specs/listing-slim.md
  # phase 2; nothing served reads them — the site reads the tiers).
  dt-cloud publish-r2 -L "$SNAP_ID" \
    || echo "WARN: publish-r2 failed for $SNAP_ID (the site keeps serving GCS)" >&2
else
  echo "no R2_ENDPOINT/R2_BUCKET — skipping publish-r2" >&2
fi
phase publish-r2

# 4d. Over-time index groups (specs/obs-axis-indexing.md Phase 1): every full
# K-scan run of indexed scans not yet in the manifest becomes one sealed
# multi-scan group; `/api/series` then reads ⌈N/K⌉ pruned groups instead of
# one point per scan. The stage lives in job/cw-overtime.sh (also the one-off
# backfill via cw-overtime-submit.sh); it takes this run's env. Never fatal:
# the series keeps its per-scan reads if a group fails.
GEN="$GEN" WORK="$WORK/over-time" bash job/cw-overtime.sh || echo "WARN: over-time groups failed (the series keeps its per-scan reads)" >&2
phase over-time

# 4e. Delete superseded index generations' files (specs/storage-consolidation.md
# phase 1): `index-gc` above drops only their D1 rows, so every reindex used to
# leave its previous `cw-l2/<scan>/index/<gen>/` behind in GCS and R2. Over every
# scan (`-R`: the row sweep ran above, for this scan): a dir goes only when no
# `index_schema` row names it, its scan still has a pointed `path` generation,
# and its newest object is older than the 2-day grace (an in-flight reindex is
# never raced). The site reads generations only through their pointers, so this
# is invisible to it. Needs D1; R2 only with its env. Never fatal.
if [ -n "${CLOUDFLARE_API_TOKEN:+set}" ] && [ -n "${CLOUDFLARE_ACCOUNT_ID:-}" ]; then  # `:+set`: xtrace must not print the token
  GC_TARGETS=(-F "gs://$DATA")
  if [ -n "${R2_ENDPOINT:+set}" ] && [ -n "${R2_BUCKET:-}" ]; then GC_TARGETS+=(-F r2); fi
  dt-cloud index-gc -R -m 2d "${GC_TARGETS[@]}" \
    || echo "WARN: index-gc -F failed (superseded generations stay until the next run)" >&2
fi
phase index-gc

# 4f. The static name index (specs/static-append.md): append this scan (and any earlier one still pending, in
# scan-id order) to cw's generation as a run — Batch stages, then R2, then `manifests/<scan>.json` — so the map's
# `?f=` filter and /names answer it statically. On when STATIC_NAMES=1 (the cron sets it, with STATIC_NAMES_IMAGE =
# this job's image): the stages run as this job's account (`cw-s3-job`, cw's profile; it holds the scratch bucket).
# See specs/cw-static-names.md. Never fatal: a scan the index doesn't hold reads "not indexed" until a later run's
# `-c` catches it up. Exit 3 = not the next scan yet.
if [ "${STATIC_NAMES:-0}" = "1" ] && [ "${REPROC:-0}" != "1" ]; then
  STATIC_NAMES_PROFILE=cw dt-cloud static-names runs add -c "$SNAP_ID" \
    || echo "WARN: static-names runs add failed for $SNAP_ID (exit $?; the filter keeps its path-store fallback)" >&2
fi
phase static-names

# 4b. Warm the site's subtree + diff caches for this scan (the colo cache, plus
# the global KV tier once `CACHE_KV` is bound in site/wrangler.toml) so the
# first viewer gets hits instead of a multi-second compute: the home page's
# default requests — one subtree, the diff span chips and the previous-scan
# pair — at the common canvas widths (`dt-cloud warm-cache`, which reads the
# SITE_URL / SNAPSHOTS_SUBDIR exported above). Auth is the job's machine grant
# (`SITE_TOKEN` ← Secret Manager `cw-s3-job-grant`, scope `cw`, minted
# from /admin — specs/done/oidc-cutover-cw.md P5); the CF Access pair is the
# pre-cutover form, kept for a rollback. Skipped when neither is set. Never fatal.
if { [ -n "${SITE_TOKEN:+set}" ] || { [ -n "${CF_ACCESS_CLIENT_ID:+set}" ] && [ -n "${CF_ACCESS_CLIENT_SECRET:+set}" ]; }; } \
   && [ "${REPROC:-0}" != "1" ]; then  # `:+set`: xtrace must not print the tokens
  dt-cloud warm-cache -d "$SNAP_ID" -r "gs://$DATA/snapshots/cw" \
    || echo "WARN: cache warm-up failed for $SNAP_ID" >&2
else
  echo "no warm-cache auth (SITE_TOKEN, or CF_ACCESS_CLIENT_ID+SECRET) — skipping cache warm-up" >&2
fi
phase warm-cache

# 4c'. Replay the site's page loads uncached (`dt-cloud probe -c`, as the gcs
# job does): every response a 2xx/4xx, and one cold-latency record per scan
# under probes/cw/ (the time series of what a viewer waits past the warmed
# cache). Same token and gating as the warm-up; never fatal.
if [ -n "${SITE_TOKEN:+set}" ] && [ "${REPROC:-0}" != "1" ]; then
  dt-cloud probe -c -u "$SITE_URL" -o "gs://$DATA/probes/cw/" \
    || echo "WARN: serving probe failed for $SNAP_ID (a 5xx or a dropped request; data is fine)" >&2
fi
phase probe

# 5. Converge the monthly Shape-C digest thread in Slack (specs/cw-slack-
# digest.md): the OP + one reply per UTC day (+ the day's provisional reply
# until its 12Z scan lands), into #cw-s3-usage. Only when SLACK_BOT_TOKEN +
# SLACK_CHANNEL are set — Shape C needs the Web API's per-message
# sender/avatar overrides. Settings: job/digest.yml (specs/digest-
# unification.md). `dt-cloud digest` also renders the plot into job/icons-cw/
# and `wrangler pages deploy`s it to the icons Pages project's `cw` branch, so
# it needs CLOUDFLARE_* + wrangler (both in this image). A failed digest never
# fails the scan.
if [ -n "${SLACK_BOT_TOKEN:+set}" ] && [ -n "${SLACK_CHANNEL:-}" ]; then  # `:+set`: xtrace must not print the token
  dt-cloud digest -T cw -C job/digest.yml -r "gs://$DATA/snapshots/cw" \
    || echo "WARN: usage-digest step failed" >&2
else
  echo "no Slack bot transport (SLACK_BOT_TOKEN+SLACK_CHANNEL) — skipping usage digest" >&2
fi
phase digest

echo "CW-SCAN-JOB-DONE $SNAP_ID -> gs://$DATA/snapshots/cw/$SNAP_ID"

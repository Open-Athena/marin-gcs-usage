#!/usr/bin/env bash
# CoreWeave S3 scan job (GCP Batch). Chain:
#   1. bulk-list  s3://<bucket> via the CAIOS S3-compatible endpoint
#   2. import     listing shards -> canonical layer-2 parquet
#   3. webdata    layer-2 -> the site's tree/age/meta JSONs
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

# 3.5. Diff vs the previous snapshot, precomputed here because the site is
# static. Consecutive pairs only (no pair explosion); the previous l2 is
# copied local first — the diff walk does a few hundred filtered reads, which
# beat on a FUSE mount but are cheap against local disk.
PREV=$(ls "/gcs/$DATA/cw-l2/" 2>/dev/null | awk -v s="$SNAP_ID" '$0 < s' | sort | tail -1)
if [ -n "$PREV" ] && [ -f "/gcs/$DATA/cw-l2/$PREV/$BUCKET.parquet" ]; then
  cp "/gcs/$DATA/cw-l2/$PREV/$BUCKET.parquet" "$WORK/prev.parquet"
  python job/cw-diff.py "$WORK/prev.parquet" "$L2" "$WORK/web/diff.json" -p "$PREV" -c "$SNAP_ID"
fi

# 4. Publish. Written last and all at once: the site's scan list is derived by
# listing this prefix, so a partially-uploaded snapshot would show up in the
# dropdown as a broken entry.
DEST="/gcs/$DATA/snapshots/cw/$SNAP_ID"
mkdir -p "$DEST"
cp "$WORK"/web/*.json "$DEST/"

# Keep the canonical layer-2 parquet too -- it's the input to every ad-hoc
# question ("what grew?", "what's idle?") that the JSONs can't answer.
mkdir -p "/gcs/$DATA/cw-l2/$SNAP_ID"
cp "$L2" "/gcs/$DATA/cw-l2/$SNAP_ID/$BUCKET.parquet"

echo "CW-SCAN-JOB-DONE $SNAP_ID -> gs://$DATA/snapshots/cw/$SNAP_ID"

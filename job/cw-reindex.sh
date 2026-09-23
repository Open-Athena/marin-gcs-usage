#!/usr/bin/env bash
# Reindex published CoreWeave scans from their persisted layer-2 parquets — no
# re-listing (no CAIOS egress), just DuckDB over the L2 already in GCS. Produces
# the age pyramid (+ path/coarse) tiers for scans that predate the pyramid
# producer, so AgeChart works on every scan (specs/age-index.md). Each scan gets
# a fresh index generation and its D1 pointer flipped via index-sync, so this is
# safe to re-run and never disturbs a scan mid-serve.
#
#   SCANS="2026-09-20T1201 2026-09-19T1201" job/cw-reindex.sh   # just these
#   job/cw-reindex.sh                                            # every scan with L2 (skips ones already pyramided unless FORCE=1)
#
# Runs on GCP Batch (GCS mounted at /gcs/$DATA_BUCKET, CLOUDFLARE_API_TOKEN from
# Secret Manager) via job/cw-reindex-submit.sh. Env mirrors cw-run.sh.
set -euxo pipefail

# Strip surrounding whitespace off secrets before anything reads them (a version
# created with `--data-file=-` carries a trailing newline; see cw-run.sh).
{ set +x; } 2>/dev/null
for n in CLOUDFLARE_API_TOKEN; do
  if [ -n "${!n+set}" ]; then v=${!n}; v=${v#"${v%%[![:space:]]*}"}; v=${v%"${v##*[![:space:]]}"}; export "$n=$v"; fi
done
set -x

export D1_DB_ID=${D1_DB_ID:-7f1e1326-b879-4ecd-8621-846621c24f36}
export D1_DB_NAME=${D1_DB_NAME:-oa-cw-s3-usage-db}
export INDEX_VARIANTS=${INDEX_VARIANTS:-path}
DATA=${DATA_BUCKET:-oa-gcs-usage-dvx}
ROOT="/gcs/$DATA/cw-l2"
WORK_ROOT=${WORK_DIR:-/stage}/reindex

# The scans to do: an explicit SCANS list, else every dir under cw-l2/ that has
# layer-2 parquets. Without FORCE=1, skip a scan that already has a pyramid tier
# in some generation (already reindexed / produced by the live job).
list_scans() {
  if [ -n "${SCANS:-}" ]; then printf '%s\n' $SCANS; return; fi
  for d in "$ROOT"/*/; do
    s=$(basename "$d")
    compgen -G "$d"'*.parquet' >/dev/null || continue
    if [ "${FORCE:-0}" != 1 ] && compgen -G "$d"'index/*/age-pyramid-1d.parquet' >/dev/null; then
      echo "skip $s (already pyramided)" >&2; continue
    fi
    echo "$s"
  done | sort
}

ok=0; fail=0
for SNAP in $(list_scans); do
  SRC=()
  for p in "$ROOT/$SNAP"/*.parquet; do
    [ -e "$p" ] || continue
    b=$(basename "$p" .parquet)
    SRC+=("$b=$p")
  done
  if [ ${#SRC[@]} -eq 0 ]; then echo "WARN: no L2 parquets for $SNAP, skipping" >&2; continue; fi

  WORK="$WORK_ROOT/$SNAP"; rm -rf "$WORK"; mkdir -p "$WORK"
  GEN=$(date -u +%Y%m%dT%H%M%SZ)
  KEY="cw-l2/$SNAP/index/$GEN"
  # AGE_ONLY=1: recompute + sync only the age pyramid (skip the byte-identical
  # path-index + coarse recompute); those variants keep their existing pointer.
  AO=${AGE_ONLY:+-A}
  if dt-cloud index-write ${AO:-} -m "${DUCKDB_MEM:-16GB}" -t "${IMPORT_JOBS:-8}" -o "$WORK" "${SRC[@]}" \
     && mkdir -p "/gcs/$DATA/$KEY" \
     && cp "$WORK"/*.parquet "/gcs/$DATA/$KEY/" \
     && dt-cloud index-sync ${AO:-} -d "/gcs/$DATA/$KEY" -g "$GEN" -k "$KEY" "$SNAP"; then
    dt-cloud index-gc "$SNAP" || echo "WARN: index-gc failed for $SNAP" >&2
    echo "REINDEXED $SNAP -> $KEY (buckets: ${SRC[*]%%=*})"
    ok=$((ok + 1))
  else
    echo "ERROR: reindex failed for $SNAP" >&2
    fail=$((fail + 1))
  fi
  rm -rf "$WORK"
done
echo "REINDEX-DONE: $ok ok, $fail failed"
[ "$fail" -eq 0 ]

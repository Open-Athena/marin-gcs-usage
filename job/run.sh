#!/usr/bin/env bash
# Daily snapshot job (Cloud Run). Chain:
#   1. direct-list marin-us-central2 (SII unavailable there — API 502s)
#   2. webdata over: SII inventories (5 buckets) + central2 listing + weekly-scan fallback
#   3. publish snapshot JSONs to the data bucket (canonical store)
#   4. rebuild site data dir from the bucket (recent snapshots + scans.json) and deploy
#
# Buckets are mounted via GCS FUSE at /gcs/<bucket> (DuckDB needs local paths).
# Env: SNAPSHOT_DATE (default today UTC), DATA_BUCKET, DUCKDB_MEM,
# CLOUDFLARE_API_TOKEN + CLOUDFLARE_ACCOUNT_ID (optional — skip deploy if absent).
set -euxo pipefail

DATE=${SNAPSHOT_DATE:-$(date -u +%F)}
DATA=${DATA_BUCKET:-oa-gcs-usage-dvx}
SII_BUCKETS=(marin-us-east1 marin-us-east5 marin-us-central1 marin-eu-west4 marin-us-west4)
FALLBACK_GLOB="/gcs/marin-us-central2/tmp/storage-scan/deduped/objects-*.parquet"
KEEP_DEPLOYED=30  # most-recent snapshots included in the site deploy

cd /app

# 1. central2 direct listing (canonical schema shards straight into the data bucket);
# clear any partial output from a previously failed run first
rm -f "/gcs/$DATA/central2-listing/$DATE"/*.parquet 2>/dev/null || true
gcs-usage list-bucket marin-us-central2 -o "gs://$DATA/central2-listing/$DATE" -w 24

# 2. assemble -l args: SII per bucket (skip loudly if a day's report is missing),
# then the fresh central2 listing, then the weekly-scan fallback (earlier wins per bucket)
L=()
for b in "${SII_BUCKETS[@]}"; do
  glob="/gcs/$b/inventory-reports/*_${DATE}T*_*.parquet"
  if compgen -G "$glob" > /dev/null; then L+=(-l "$glob"); else echo "WARN: no SII report for $b on $DATE (falling back to weekly scan)" >&2; fi
done
L+=(-l "/gcs/$DATA/central2-listing/$DATE/*.parquet")
if compgen -G "$FALLBACK_GLOB" > /dev/null; then L+=(-l "$FALLBACK_GLOB"); else echo "WARN: weekly-scan fallback glob is empty" >&2; fi

A=(-a "/gcs/$DATA/attr/attribution-2026-07-20.parquet" -a "/gcs/$DATA/attr/attribution-wandb.parquet")

gcs-usage webdata -d "$DATE" "${L[@]}" "${A[@]}" -o "/tmp/snap/$DATE"
gcs-usage rules -o /tmp/rules.json || true  # findings shouldn't block the snapshot

# 3. publish to the canonical store
cp -r "/tmp/snap/$DATE" "/gcs/$DATA/snapshots/$DATE.tmp" && mv "/gcs/$DATA/snapshots/$DATE.tmp" "/gcs/$DATA/snapshots/$DATE" 2>/dev/null || {
  # FUSE rename of dirs can be unsupported; fall back to direct copy
  mkdir -p "/gcs/$DATA/snapshots/$DATE"
  cp "/tmp/snap/$DATE"/*.json "/gcs/$DATA/snapshots/$DATE/"
}

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

if [ -n "${CLOUDFLARE_API_TOKEN:-}" ]; then
  wrangler pages deploy dist --project-name oa-gcs-usage --branch main
else
  echo "WARN: CLOUDFLARE_API_TOKEN not set — snapshot published to gs://$DATA but site NOT deployed" >&2
fi
echo "SNAPSHOT-JOB-DONE $DATE"

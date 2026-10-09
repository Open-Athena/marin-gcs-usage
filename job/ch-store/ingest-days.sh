#!/bin/bash
# On the ch-store VM (as root): ingest scans in order into the ClickHouse store
# (`dt-cloud ch-ingest -F`), one record per scan appended to /data/ingest.jsonl.
# Each scan's newest index generation's `path` sort is copied into ClickHouse's
# user_files (/data/in/) first; the next scan's copy overlaps this scan's ingest.
#   ingest-days.sh 2026-09-04 … 2026-10-03
set -euo pipefail
B=gs://oa-gcs-usage-dvx/listing
IMAGE=$(cat /data/image)
# The newest generation's; scans before index generations (≤ 2026-09-06) keep it beside the listing.
src() { { gcloud storage ls "$B/$1/index/*/path-index.parquet" 2>/dev/null || gcloud storage ls "$B/$1/path-index.parquet"; } | sort | tail -1; }
fetch() { local s; s=$(src "$1"); [ -n "$s" ] && gcloud storage cp -q "$s" "/data/in/$1.parquet.tmp" && mv "/data/in/$1.parquet.tmp" "/data/in/$1.parquet"; echo "$s" > "/data/in/$1.src"; }
days=("$@")
fetch "${days[0]}"
for i in "${!days[@]}"; do
  d=${days[$i]}
  next=${days[$((i + 1))]:-}
  [ -n "$next" ] && { fetch "$next" & pf=$!; }
  t0=$(date +%s)
  if docker run --rm --network host -v /data:/data -e PYTHONPATH=/data/src -e CH_INGEST_PAIRS="${CH_INGEST_PAIRS:-}" --entrypoint python3 "$IMAGE" -u -m dt_cloud.cli \
    ch-ingest -d "$d" -F -t "${THREADS:-8}" "$d.parquet" >> /data/ingest.jsonl 2>> /data/ingest.log; then
    echo "ingested $d rc=0 in $(( $(date +%s) - t0 ))s (src $(cat "/data/in/$d.src"))" >> /data/ingest.log
  else
    rc=$?
    echo "ingest failed $d rc=$rc in $(( $(date +%s) - t0 ))s (src $(cat "/data/in/$d.src"))" >> /data/ingest.log
    exit "$rc"
  fi
  rm -f "/data/in/$d.parquet"
  [ -n "$next" ] && wait "$pf"
done
echo "ingest done" >> /data/ingest.log

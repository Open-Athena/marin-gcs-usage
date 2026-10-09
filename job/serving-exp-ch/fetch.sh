#!/bin/bash
# On the ClickHouse VM: copy each date's per-object listing shards and dir rollups
# into /data/in/<date>/ (ClickHouse's user_files), then the bench export.
#   fetch.sh 2026-09-27 … 2026-10-03
set -uo pipefail
L=gs://oa-gcs-usage-dvx/listing
for d in "$@"; do
  t0=$(date +%s)
  mkdir -p /data/in/$d/obj
  for b in $(gcloud storage ls $L/$d/ | grep -o 'marin-[a-z0-9-]*/$' | tr -d /); do
    mkdir -p /data/in/$d/obj/$b
    gcloud storage cp -q "$L/$d/$b/shard-*.parquet" /data/in/$d/obj/$b/
  done
  gcloud storage cp -q $L/$d/dir-cache/dir-stats.parquet /data/in/$d/
  echo "fetched $d in $(( $(date +%s) - t0 ))s: $(du -sh /data/in/$d | cut -f1)"
done

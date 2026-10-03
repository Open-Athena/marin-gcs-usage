#!/bin/bash
# On the ClickHouse VM: load each date as it finishes fetching (fetch.sh's log), in scan order.
#   load-all.sh 2026-09-27 … 2026-10-03
set -uo pipefail
s=0
for d in "$@"; do
  until grep -q "fetched $d" /data/fetch.log; do sleep 5; done
  /data/load-scan.sh $s $d
  s=$((s + 1))
done
echo "loaded all"

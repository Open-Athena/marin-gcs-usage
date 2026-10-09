#!/bin/bash
# On the ch-store VM (as root): box-bench.sh's latency runs without the exactness probe or the cold filter pass
# (a smaller machine's price check).   box-bench-short.sh TAG
set -uo pipefail
tag=${1:?TAG}
IMAGE=$(cat /data/image)
B=gs://oa-gcs-usage-dvx/scratch/bench
py() { docker run --rm --privileged --network host -v /data:/data -e PYTHONPATH=/data/src -e QUERY_BOX_TOKEN="$(cat /data/token)" \
  --entrypoint python3 "$IMAGE" -u -m dt_cloud.cli "$@"; }
py ch-bench -f /data/requests.txt -s 1 -n 3 -o "/data/bench-$tag-warm.jsonl" > /dev/null 2> "/data/bench-$tag-warm.log"
py ch-bench -f /data/requests.txt -s 1 -n 1 -C -o "/data/bench-$tag-cold.jsonl" > /dev/null 2> "/data/bench-$tag-cold.log"
py ch-bench -Q $B/queries/gcs.yml -d 2026-10-01 -n 2 -o "/data/bench-$tag-fwarm.jsonl" > /dev/null 2> "/data/bench-$tag-fwarm.log"
echo "bench $tag done" >> /data/bench.log

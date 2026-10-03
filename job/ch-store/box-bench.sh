#!/bin/bash
# On the ch-store VM (as root), with `serve-query -e ch` up on :8080: the bench through its HTTP.
#   box-bench.sh TAG   → /data/bench-TAG-{warm,cold,fwarm,fcold}.jsonl, /data/bench-TAG-probe.log
# Request classes (requests.txt) warm (2 rounds after a first) and cold (caches dropped per request);
# the 105-query filter bench warm and cold; and its exactness against the phase-1 truth (`probe -Q`).
set -uo pipefail
tag=${1:?TAG}
IMAGE=$(cat /data/image)
B=gs://oa-gcs-usage-dvx/scratch/bench
py() { docker run --rm --privileged --network host -v /data:/data -e PYTHONPATH=/data/src -e QUERY_BOX_TOKEN="$(cat /data/token)" \
  -e GCS_USAGE_TOKEN="$(cat /data/token)" --entrypoint python3 "$IMAGE" -u -m dt_cloud.cli "$@"; }
py ch-bench -f /data/requests.txt -s 1 -n 3 -o "/data/bench-$tag-warm.jsonl" > "/data/bench-$tag-warm.summary" 2> "/data/bench-$tag-warm.log"
py ch-bench -f /data/requests.txt -s 1 -n 1 -C -o "/data/bench-$tag-cold.jsonl" > "/data/bench-$tag-cold.summary" 2> "/data/bench-$tag-cold.log"
py ch-bench -Q $B/queries/gcs.yml -d 2026-10-01 -n 2 -o "/data/bench-$tag-fwarm.jsonl" > "/data/bench-$tag-fwarm.summary" 2> "/data/bench-$tag-fwarm.log"
py ch-bench -Q $B/queries/gcs.yml -d 2026-10-01 -n 1 -C -o "/data/bench-$tag-fcold.jsonl" > "/data/bench-$tag-fcold.summary" 2> "/data/bench-$tag-fcold.log"
py probe -u http://localhost:8080 -Q $B/queries/gcs.yml -T $B/truth/2026-10-01/ > "/data/bench-$tag-probe.out" 2> "/data/bench-$tag-probe.log"
echo "bench $tag done" >> /data/bench.log

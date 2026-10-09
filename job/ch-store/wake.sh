#!/bin/bash
# From a laptop: time suspend → resume (MODE=suspend) or stop → start (MODE=stop) of the ch-store VM to its
# first answer through serve-query (a root subtree of the newest scan, then a filter), each step from the API call.
#   job/ch-store/wake.sh suspend|stop [IDLE_SECONDS]
set -euo pipefail
cd "$(dirname "$0")/../.."
G=(--zone us-east1-c --project oa-internal-450019)
mode=$1
now() { python3 -c 'import time; print(time.time())'; }
dt() { python3 -c "print(round($2 - $1, 1))"; }
ask() { job/ch-store.sh sh "curl -s -o /dev/null -w '%{http_code} %{time_total}' -H \"Authorization: Bearer \$(sudo cat /data/token)\" '$1'"; }
t0=$(now)
if [ "$mode" = suspend ]; then gcloud compute instances suspend ch-store "${G[@]}" -q > /dev/null 2>&1; else gcloud compute instances stop ch-store "${G[@]}" -q > /dev/null 2>&1; fi
t1=$(now)
sleep "${2:-60}"
t2=$(now)
if [ "$mode" = suspend ]; then gcloud compute instances resume ch-store "${G[@]}" > /dev/null 2>&1; else gcloud compute instances start ch-store "${G[@]}" > /dev/null 2>&1; fi
t3=$(now)
until job/ch-store.sh sh true 2> /dev/null; do sleep 1; done
t4=$(now)
[ "$mode" = stop ] && job/ch-store.sh serve > /dev/null
until job/ch-store.sh sh 'curl -sf localhost:8080/healthz > /dev/null' 2> /dev/null; do sleep 1; done
t5=$(now)
r1=$(ask "localhost:8080/api/subtree?cv=2&w=1408&h=896&date=2026-10-03&path=&minArea=12.$RANDOM")
t6=$(now)
r2=$(ask "localhost:8080/api/subtree?w=1408&h=896&date=2026-10-01&path=&q=safetensors&full=1&minArea=12.$RANDOM")
echo "$mode: down call $(dt "$t0" "$t1")s; up call $(dt "$t2" "$t3")s; ssh +$(dt "$t3" "$t4")s; healthz +$(dt "$t3" "$t5")s; first answer +$(dt "$t3" "$t6")s (root: $r1; then a filter: $r2)"
date -u "+%FT%TZ wake test $mode" >> tmp/ch-store/costlog.txt

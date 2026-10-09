#!/bin/bash
# From a laptop: stop the ch-store VM, change its machine type, start it, and wait until serve-query answers
# (it is restarted). Prints the seconds each step took.   job/ch-store/resize.sh MACHINE_TYPE
set -euo pipefail
cd "$(dirname "$0")/../.."
G=(--zone us-east1-c --project oa-internal-450019)
t0=$(date +%s)
gcloud compute instances stop ch-store "${G[@]}" -q > /dev/null 2>&1
t1=$(date +%s)
gcloud compute instances set-machine-type ch-store "${G[@]}" --machine-type "$1" > /dev/null 2>&1
gcloud compute instances start ch-store "${G[@]}" > /dev/null 2>&1
t2=$(date +%s)
until job/ch-store.sh sh true 2> /dev/null; do sleep 3; done
job/ch-store.sh serve > /dev/null
until job/ch-store.sh sh 'curl -sf localhost:8080/healthz > /dev/null' 2> /dev/null; do sleep 2; done
echo "stop $((t1 - t0))s, start $((t2 - t1))s, serving $(( $(date +%s) - t2 ))s later ($1)"
date -u "+%FT%TZ resized to $1" >> tmp/ch-store/costlog.txt

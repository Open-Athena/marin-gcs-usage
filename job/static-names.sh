#!/usr/bin/env bash
# The static name-search build (specs/architecture/static-name-search.md, "Pipeline") on GCP Batch in
# us-east1, beside the data bucket. Every stage is `dt-cloud static-names …` from this checkout's
# committed `cloud/src/dt_cloud` (staged content-addressed by its git tree), in the pinned job image,
# with the bucket mounted at /gcs/$B and a local SSD at /stage.
#
#   job/static-names.sh stage-src                 # upload HEAD's dt_cloud tree → gs://$B/static-names/src/<tree>/ (prints <tree>)
#   job/static-names.sh run KIND TASKS ARGS…      # one Batch job of TASKS tasks, each `dt-cloud static-names KIND -m /gcs/$B ARGS…`;
#                                                 # waits for it and exits nonzero unless every task succeeded
#   job/static-names.sh wait JOB                  # wait for a submitted job
#   job/static-names.sh ch-answers DATES TERMS    # reference answers from the ch-store VM's ClickHouse (`mega_names.answer`, postings `m`),
#                                                 # read-only, sequential; DATES comma-separated, TERMS a file of literals; JSON lines on stdout
#
# Env: MACHINE (n2-highmem-16), SSD (750; n2 16-vCPU needs ≥2 local SSDs of 375), PARALLELISM (TASKS), SPOT (1: spot VMs,
# 3 retries), MAX_RUN_SECONDS (14400), IMAGE (the pinned job image digest), SRC (a staged tree; default HEAD's), JOB_ID, DRY=1.
set -euo pipefail
cd "$(dirname "$0")/.."
PROJECT=oa-internal-450019
REGION=us-east1
B=oa-gcs-usage-dvx
REPO=us-central1-docker.pkg.dev/$PROJECT/cloud-run-source-deploy/gcs-usage-snapshot
# The gcs job image as of 2026-10-08 (duckdb 1.5.6, pyarrow 22.0.0), pinned so a rerun computes with the same engines.
IMAGE=${IMAGE:-$REPO@sha256:6974a8b71e56469bb8180495c13c170decdc10aefe695cd0f8c5200fb3ac520a}
MACHINE=${MACHINE:-n2-highmem-16}
SSD=${SSD:-750}

tree() {
  if ! git diff --quiet HEAD -- cloud/src/dt_cloud; then
    echo "cloud/src/dt_cloud has uncommitted changes: commit them (the staged source is HEAD's tree)" >&2
    exit 1
  fi
  git rev-parse HEAD:cloud/src/dt_cloud
}

stage_src() {
  local t d
  t=$(tree)
  if gcloud storage ls "gs://$B/static-names/src/$t/dt_cloud/__init__.py" > /dev/null 2>&1; then echo "$t"; return; fi
  d=tmp/static-names-src/$t
  rm -rf "$d" && mkdir -p "$d/dt_cloud"
  git archive HEAD:cloud/src/dt_cloud | tar -x -C "$d/dt_cloud"
  gcloud storage rsync -r --verbosity=error "$d/dt_cloud" "gs://$B/static-names/src/$t/dt_cloud" > /dev/null
  echo "$t"
}

wait_job() {
  local job=$1 state="" delay=20 counts
  while :; do
    state=$(gcloud batch jobs describe "$job" --project "$PROJECT" --location "$REGION" --format='value(status.state)')
    counts=$(gcloud batch jobs describe "$job" --project "$PROJECT" --location "$REGION" --format=json \
      | python3 -c 'import json,sys; d=json.load(sys.stdin); print(" ".join(f"{k}={v}" for g in (d.get("status",{}).get("taskGroups") or {}).values() for k,v in sorted(g.get("counts",{}).items())))')
    echo "$(date -u +%H:%M:%S) $job $state $counts" >&2
    case $state in
      SUCCEEDED) return 0 ;;
      FAILED|DELETION_IN_PROGRESS|CANCELLED) return 1 ;;
    esac
    sleep "$delay"
    [ "$delay" -lt 120 ] && delay=$((delay * 2))
  done
}

case ${1:?stage-src|run|wait|ch-answers} in
stage-src) stage_src ;;
ch-answers)
  job/ch-store.sh sh "sudo mkdir -p /data/sn && sudo tee /data/sn/ch-answers.py > /dev/null" < job/static-names/ch-answers.py
  job/ch-store.sh sh "sudo tee /data/sn/terms.txt > /dev/null" < "${3:?TERMS}"
  job/ch-store.sh sh "sudo docker run --rm --network host -v /data:/data -e PYTHONPATH=/data/src --entrypoint python3 \$(cat /data/image) -u /data/sn/ch-answers.py ${2:?DATES} /data/sn/terms.txt"
  ;;
wait) wait_job "${2:?JOB}" ;;
run)
  KIND=${2:?KIND}
  TASKS=${3:?TASKS}
  shift 3
  SRC=${SRC:-$(stage_src)}
  JOB_ID=${JOB_ID:-sn-$KIND-$(date -u +%Y%m%d-%H%M%S)}
  CPU=$(( ${MACHINE##*-} * 1000 ))
  MEM_MIB=$(( ${MACHINE##*-} * 7700 ))
  cmd="set -euo pipefail; mkdir -p /stage/src /stage/tmp /stage/out && cp -r /gcs/$B/static-names/src/$SRC/dt_cloud /stage/src/ && cd /stage && \
PYTHONPATH=/stage/src python3 -u -m dt_cloud.static_names $KIND -m /gcs/$B $(printf '%q ' "$@")"
  if [ -n "${SPOT:-}" ]; then MODEL=SPOT; RETRIES=3; else MODEL=STANDARD; RETRIES=0; fi
  spec=$(mktemp)
  cat > "$spec" <<EOF
{
  "taskGroups": [{
    "taskCount": $TASKS,
    "parallelism": ${PARALLELISM:-$TASKS},
    "taskSpec": {
      "runnables": [{"container": {
        "imageUri": "$IMAGE",
        "entrypoint": "bash",
        "commands": $(python3 -c 'import json, sys; print(json.dumps(["-c", sys.argv[1]]))' "$cmd"),
        "volumes": ["/mnt/disks/gcs/$B:/gcs/$B:ro", "/mnt/disks/stage:/stage:rw"]
      }}],
      "computeResource": {"cpuMilli": $CPU, "memoryMib": $MEM_MIB},
      "maxRetryCount": $RETRIES,
      "maxRunDuration": "${MAX_RUN_SECONDS:-14400}s",
      "volumes": [
        {"gcs": {"remotePath": "$B"}, "mountPath": "/mnt/disks/gcs/$B", "mountOptions": ["--implicit-dirs"]},
        {"deviceName": "stage", "mountPath": "/mnt/disks/stage"}
      ]
    }
  }],
  "allocationPolicy": {
    "instances": [{"policy": {
      "machineType": "$MACHINE",
      "provisioningModel": "$MODEL",
      "bootDisk": {"type": "pd-balanced", "sizeGb": "100"},
      "disks": [{"newDisk": {"type": "local-ssd", "sizeGb": "$SSD"}, "deviceName": "stage"}]
    }}],
    "serviceAccount": {"email": "gcs-usage-job@$PROJECT.iam.gserviceaccount.com"},
    "location": {"allowedLocations": ["regions/$REGION"]}
  },
  "labels": {"purpose": "static-names", "stage": "$KIND"},
  "logsPolicy": {"destination": "CLOUD_LOGGING"}
}
EOF
  if [ -n "${DRY:-}" ]; then cat "$spec"; rm -f "$spec"; exit 0; fi
  gcloud batch jobs submit "$JOB_ID" --project "$PROJECT" --location "$REGION" --config "$spec" >&2
  rm -f "$spec"
  echo "submitted $JOB_ID (src $SRC, $TASKS tasks on $MACHINE $MODEL)" >&2
  wait_job "$JOB_ID"
  echo "$JOB_ID"
  ;;
esac

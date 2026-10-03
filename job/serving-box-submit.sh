#!/usr/bin/env bash
# Submit a serving-box bake-off job (job/serving-box-bench.sh) to GCP Batch in
# us-east1, beside the data bucket: stages this checkout's `dt_cloud` source and
# the bench script under gs://oa-gcs-usage-dvx/scratch/bench/serving-box/, then
# runs the gcs job image with that source on PYTHONPATH.
#
#   job/serving-box-submit.sh build                 # n2-highmem-32: the index build
#   job/serving-box-submit.sh target                # n2-highmem-8: the box's size
#   job/serving-box-submit.sh py x.py [args…]       # n2-highmem-16: a one-off script (tmp/x.py, staged)
#
# Env: MACHINE, GEN, INDEX, THREADS, DUCK_MEM, ENGINE_THREADS, STEPS (target: mem,mmap,serve,duckdb,mounts), LAYERS, MOUNT_MEM, SCAN, PROBE_ARGS (forwarded); DRY=1 prints the spec.
set -euo pipefail
cd "$(dirname "$0")/.."
MODE=${1:?build|target}
PROJECT=oa-internal-450019
REGION=us-east1
B=oa-gcs-usage-dvx
SB=gs://$B/scratch/bench/serving-box
IMAGE=${IMAGE:-us-central1-docker.pkg.dev/$PROJECT/cloud-run-source-deploy/gcs-usage-snapshot:latest}
case $MODE in
  build) MACHINE=${MACHINE:-n2-highmem-32}; SSD=1500; MEM_MIB=250000 ;;
  target) MACHINE=${MACHINE:-n2-highmem-8}; SSD=375; MEM_MIB=62000 ;;
  py) MACHINE=${MACHINE:-n2-highmem-16}; SSD=750; MEM_MIB=$(( ${MACHINE##*-} * 7700 )) ;;
  *) echo "mode: build|target" >&2; exit 2 ;;
esac
CPU=$(( ${MACHINE##*-} * 1000 ))
JOB_ID=${JOB_ID:-sb-$MODE-$(date -u +%Y%m%d-%H%M%S)}

if [ -z "${DRY:-}" ]; then
  gcloud storage rsync -r -x '.*__pycache__.*' cloud/src/dt_cloud "$SB/src/dt_cloud" >&2
  gcloud storage cp job/serving-box-bench.sh "$SB/scripts/" >&2
  if [ "$MODE" = py ]; then gcloud storage cp "tmp/$2" "$SB/scripts/" >&2; fi
fi

env_json=$(python3 - "$JOB_ID" <<'EOF'
import json, os, sys
v = {"BENCH_JOB": sys.argv[1]}
for k in ["GEN", "INDEX", "THREADS", "DUCK_MEM", "ENGINE_THREADS", "STEPS", "LAYERS", "MOUNT_MEM", "SCAN", "PROBE_ARGS"]:
    if os.environ.get(k):
        v[k] = os.environ[k]
print(json.dumps(v))
EOF
)
spec=$(mktemp)
cat > "$spec" <<EOF
{
  "taskGroups": [{
    "taskCount": 1,
    "taskSpec": {
      "runnables": [{"container": {
        "imageUri": "$IMAGE",
        "entrypoint": "bash",
        "commands": $(python3 -c 'import json, sys; print(json.dumps(sys.argv[1:]))' "/gcs/$B/scratch/bench/serving-box/scripts/serving-box-bench.sh" "$MODE" "${@:2}"),
        "options": "--privileged",
        "volumes": ["/mnt/disks/gcs/$B:/gcs/$B:ro", "/mnt/disks/stage:/stage:rw"]
      }}],
      "computeResource": {"cpuMilli": $CPU, "memoryMib": $MEM_MIB},
      "maxRetryCount": 0,
      "maxRunDuration": "${MAX_RUN_SECONDS:-7200}s",
      "volumes": [
        {"gcs": {"remotePath": "$B"}, "mountPath": "/mnt/disks/gcs/$B", "mountOptions": ["--implicit-dirs"]},
        {"deviceName": "stage", "mountPath": "/mnt/disks/stage"}
      ],
      "environment": {"variables": $env_json}
    }
  }],
  "allocationPolicy": {
    "instances": [{"policy": {
      "machineType": "$MACHINE",
      "bootDisk": {"type": "pd-balanced", "sizeGb": "100"},
      "disks": [{"newDisk": {"type": "local-ssd", "sizeGb": "$SSD"}, "deviceName": "stage"}]
    }}],
    "serviceAccount": {"email": "gcs-usage-job@$PROJECT.iam.gserviceaccount.com"},
    "location": {"allowedLocations": ["regions/$REGION"]}
  },
  "labels": {"purpose": "serving-box-bench"},
  "logsPolicy": {"destination": "CLOUD_LOGGING"}
}
EOF
if [ -n "${DRY:-}" ]; then cat "$spec"; rm -f "$spec"; exit 0; fi
gcloud batch jobs submit "$JOB_ID" --project "$PROJECT" --location "$REGION" --config "$spec" >&2
rm -f "$spec"
echo "$JOB_ID"

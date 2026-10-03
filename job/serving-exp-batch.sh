#!/usr/bin/env bash
# Run one `dt-cloud` command on GCP Batch in us-east1 (beside the data) for the
# serving experiments (specs/serving-options.md): stages this checkout's
# `dt_cloud` under gs://oa-gcs-usage-dvx/scratch/bench/serving-exp/src/ (not
# serving-box's, which another run may be using) and runs the gcs job image
# with it on PYTHONPATH, a local SSD at /stage. Log: Cloud Logging.
#
#   job/serving-exp-batch.sh bench-ch-export -i gs://…/mem-index/2026-10-01 -s /stage -o /stage/ch -u gs://…/ch/2026-10-01 GEN
#
# Env: MACHINE (n2-highmem-16), SSD (375 per disk; n2-16 needs ≥2), JOB_ID, DRY=1 prints the spec.
set -euo pipefail
cd "$(dirname "$0")/.."
PROJECT=oa-internal-450019
REGION=us-east1
B=oa-gcs-usage-dvx
X=gs://$B/scratch/bench/serving-exp
IMAGE=${IMAGE:-us-central1-docker.pkg.dev/$PROJECT/cloud-run-source-deploy/gcs-usage-snapshot:latest}
MACHINE=${MACHINE:-n2-highmem-16}
SSD=${SSD:-750}
CPU=$(( ${MACHINE##*-} * 1000 ))
MEM_MIB=$(( ${MACHINE##*-} * 7700 ))
JOB_ID=${JOB_ID:-sx-$(date -u +%Y%m%d-%H%M%S)}
[ -n "${DRY:-}" ] || gcloud storage rsync -r -x '.*__pycache__.*' cloud/src/dt_cloud "$X/src/dt_cloud" >&2
cmd="mkdir -p /stage/src && cp -r /gcs/$B/scratch/bench/serving-exp/src/. /stage/src/ && cd /stage && PYTHONPATH=/stage/src python3 -u -m dt_cloud.cli $(printf '%q ' "$@")"
spec=$(mktemp)
cat > "$spec" <<EOF
{
  "taskGroups": [{
    "taskCount": 1,
    "taskSpec": {
      "runnables": [{"container": {
        "imageUri": "$IMAGE",
        "entrypoint": "bash",
        "commands": $(python3 -c 'import json, sys; print(json.dumps(["-c", sys.argv[1]]))' "$cmd"),
        "volumes": ["/mnt/disks/gcs/$B:/gcs/$B:ro", "/mnt/disks/stage:/stage:rw"]
      }}],
      "computeResource": {"cpuMilli": $CPU, "memoryMib": $MEM_MIB},
      "maxRetryCount": 0,
      "maxRunDuration": "${MAX_RUN_SECONDS:-7200}s",
      "volumes": [
        {"gcs": {"remotePath": "$B"}, "mountPath": "/mnt/disks/gcs/$B", "mountOptions": ["--implicit-dirs"]},
        {"deviceName": "stage", "mountPath": "/mnt/disks/stage"}
      ]
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
  "labels": {"purpose": "serving-exp"},
  "logsPolicy": {"destination": "CLOUD_LOGGING"}
}
EOF
if [ -n "${DRY:-}" ]; then cat "$spec"; rm -f "$spec"; exit 0; fi
gcloud batch jobs submit "$JOB_ID" --project "$PROJECT" --location "$REGION" --config "$spec" >&2
rm -f "$spec"
echo "$JOB_ID"

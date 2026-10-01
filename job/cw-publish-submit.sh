#!/usr/bin/env bash
# Submit a GCP Batch array job that runs the cw job's 4c stage — `dt-cloud
# publish-r2 <scan>` — for a list of scans: the R2 backfill over old scans, or a
# re-publish, using the exact image + secrets the scheduled scan uses
# (specs/done/r2-serving-migration.md step 4). Idempotent per scan (size + md5), so
# re-running over already-published scans only moves what's missing.
#
#   job/cw-publish-submit.sh 2026-09-25T0001            # one scan
#   job/cw-publish-submit.sh $(cat scans.txt)           # many, one task each
#   DRY=1 job/cw-publish-submit.sh …                    # print the body only
#   PARALLELISM=8 job/cw-publish-submit.sh …            # concurrent tasks (default 4)
#
# Each task streams its scan GCS → R2 through the APIs (no bucket mount); the
# job SA reads GCS via ADC and the R2 creds are the `cw-s3-r2-*` secrets, as
# in cw-batch-submit.sh.
set -euo pipefail

if [ $# -eq 0 ]; then echo "usage: $0 <scan-id>…" >&2; exit 2; fi
SCANS=("$@")

PROJECT=oa-internal-450019
REGION=us-central1
SA=gcs-usage-job@$PROJECT.iam.gserviceaccount.com
IMAGE=${IMAGE:-us-central1-docker.pkg.dev/$PROJECT/cloud-run-source-deploy/gcs-usage-snapshot:cw}
DATA_BUCKET=${DATA_BUCKET:-oa-gcs-usage-dvx}
R2_BUCKET=${R2_BUCKET:-oa-cw-s3-usage-index}
PARALLELISM=${PARALLELISM:-4}
JOB_ID=${JOB_ID:-cw-publish-r2-$(date -u +%Y%m%d-%H%M%S)}

# The scan list rides in the command as a bash array; task N publishes scan N.
export PROJECT SA IMAGE DATA_BUCKET R2_BUCKET PARALLELISM
body=$(python3 - "${SCANS[@]}" <<'EOF'
import json, os, sys
scans = sys.argv[1:]
cmd = (
    "set -euo pipefail\n"
    f"SCANS=({' '.join(scans)})\n"
    'SNAP_ID=${SCANS[$BATCH_TASK_INDEX]}\n'
    'echo "task $BATCH_TASK_INDEX → publish-r2 $SNAP_ID"\n'
    'dt-cloud publish-r2 "$SNAP_ID"\n'
    'echo PUBLISH_DONE "$SNAP_ID"'
)
P = os.environ["PROJECT"]
print(json.dumps({
    "taskGroups": [{
        "taskCount": len(scans),
        "parallelism": int(os.environ["PARALLELISM"]),
        "taskSpec": {
            "runnables": [{"container": {"imageUri": os.environ["IMAGE"], "entrypoint": "bash", "commands": ["-c", cmd]}}],
            "computeResource": {"cpuMilli": 4000, "memoryMib": 8192},
            "maxRetryCount": 1,
            "maxRunDuration": "7200s",
            "environment": {
                "variables": {"DATA_BUCKET": os.environ["DATA_BUCKET"], "R2_BUCKET": os.environ["R2_BUCKET"]},
                "secretVariables": {
                    "R2_ENDPOINT": f"projects/{P}/secrets/cw-s3-r2-endpoint/versions/latest",
                    "R2_ACCESS_KEY_ID": f"projects/{P}/secrets/cw-s3-r2-access-key-id/versions/latest",
                    "R2_SECRET_ACCESS_KEY": f"projects/{P}/secrets/cw-s3-r2-secret-access-key/versions/latest",
                },
            },
        },
    }],
    "allocationPolicy": {
        "instances": [{"policy": {"machineType": "n2-standard-4", "bootDisk": {"type": "pd-balanced", "sizeGb": "50"}}}],
        "serviceAccount": {"email": os.environ["SA"]},
    },
    "logsPolicy": {"destination": "CLOUD_LOGGING"},
}, indent=1))
EOF
)

if [ -n "${DRY:-}" ]; then echo "$body"; exit 0; fi
echo "$body" > "tmp/$JOB_ID.json" 2>/dev/null || true
gcloud batch jobs submit "$JOB_ID" --project "$PROJECT" --location "$REGION" --config - <<<"$body" >/dev/null
echo "submitted $JOB_ID (${#SCANS[@]} scans, parallelism $PARALLELISM)"
echo "  gcloud batch jobs describe $JOB_ID --project $PROJECT --location $REGION --format='value(status.state)'"

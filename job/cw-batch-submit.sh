#!/usr/bin/env bash
# Submit the CoreWeave S3 scan to GCP Batch (see job/cw-run.sh for the chain).
#
# Deliberately a separate submitter from batch-submit.sh rather than a flag on
# it: the two jobs share almost no shape. The GCS job fans out over six buckets,
# mounts all of them, needs highmem-16 + local SSD for DuckDB spill, and posts a
# digest. This one hits a single third-party S3 endpoint, mounts one bucket for
# output, and peaks under 2 GB.
#
#   ./job/cw-batch-submit.sh              # submit, print job id
#   DRY=1 ./job/cw-batch-submit.sh        # print the spec (for a scheduler body)
#
# Prereq (one-time): the CAIOS S3 keypair in Secret Manager, readable by the SA.
#   printf %s "$KEY_ID" | gcloud secrets create cw-s3-access-key-id --data-file=-
#   printf %s "$SECRET" | gcloud secrets create cw-s3-secret-access-key --data-file=-
set -euo pipefail

PROJECT=oa-internal-450019
REGION=us-central1
SA=gcs-usage-job@$PROJECT.iam.gserviceaccount.com
IMAGE=${IMAGE:-us-central1-docker.pkg.dev/$PROJECT/cloud-run-source-deploy/gcs-usage-snapshot:latest}
# Measured on a 92M-object scan: 1.1 GB RSS listing, 1.8 GB import. The lister
# is network-bound (many concurrent range requests), so vCPUs buy more than RAM.
MACHINE=${MACHINE:-n2-standard-8}
MEMORY_MIB=${MEMORY_MIB:-30000}
DATA_BUCKET=${DATA_BUCKET:-oa-gcs-usage-dvx}
CW_BUCKET=${CW_BUCKET:-marin-us-east-02a}
JOB_ID=${JOB_ID:-cw-scan-$(date -u +%Y%m%d-%H%M%S)}

vars() {
  python3 - <<'EOF'
import json, os
v = {
    "CW_BUCKET": os.environ.get("CW_BUCKET", "marin-us-east-02a"),
    "CW_ENDPOINT": os.environ.get("CW_ENDPOINT", "https://cwobject.com"),
    "DATA_BUCKET": os.environ.get("DATA_BUCKET", "oa-gcs-usage-dvx"),
    "WORK_DIR": os.environ.get("WORK_DIR", "/stage/cw"),
    # boto/aws-sdk needs a region even though CAIOS ignores it
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_EC2_METADATA_DISABLED": "true",
}
for k in ["SNAP_ID", "LISTING_PROCS", "LISTING_WORKERS", "IMPORT_JOBS"]:
    if k in os.environ:
        v[k] = os.environ[k]
print(json.dumps(v))
EOF
}

spec=$(mktemp)
cat > "$spec" <<EOF
{
  "taskGroups": [{
    "taskCount": 1,
    "taskSpec": {
      "runnables": [{
        "container": {
          "imageUri": "$IMAGE",
          "entrypoint": "bash",
          "commands": ["job/cw-run.sh"],
          "volumes": ["/mnt/disks/gcs/$DATA_BUCKET:/gcs/$DATA_BUCKET:rw", "/mnt/disks/stage:/stage:rw"]
        }
      }],
      "computeResource": {"cpuMilli": 8000, "memoryMib": $MEMORY_MIB},
      "maxRetryCount": 0,
      "maxRunDuration": "14400s",
      "volumes": [
        {"gcs": {"remotePath": "$DATA_BUCKET"}, "mountPath": "/mnt/disks/gcs/$DATA_BUCKET", "mountOptions": ["--implicit-dirs"]},
        {"deviceName": "stage", "mountPath": "/mnt/disks/stage"}
      ],
      "environment": {
        "variables": $(vars),
        "secretVariables": {
          "AWS_ACCESS_KEY_ID": "projects/$PROJECT/secrets/cw-s3-access-key-id/versions/latest",
          "AWS_SECRET_ACCESS_KEY": "projects/$PROJECT/secrets/cw-s3-secret-access-key/versions/latest"
        }
      }
    }
  }],
  "allocationPolicy": {
    "instances": [{
      "policy": {
        "machineType": "$MACHINE",
        "bootDisk": {"type": "pd-balanced", "sizeGb": "100"},
        "disks": [{"newDisk": {"type": "pd-ssd", "sizeGb": "200"}, "deviceName": "stage"}]
      }
    }],
    "serviceAccount": {"email": "$SA"},
    "location": {"allowedLocations": ["regions/$REGION"]}
  },
  "logsPolicy": {"destination": "CLOUD_LOGGING"}
}
EOF

if [ -n "${DRY:-}" ]; then cat "$spec"; rm -f "$spec"; exit 0; fi
gcloud batch jobs submit "$JOB_ID" --project "$PROJECT" --location "$REGION" --config "$spec" >&2
rm -f "$spec"
echo "$JOB_ID"

#!/usr/bin/env bash
# Submit the CoreWeave S3 scan to GCP Batch (see job/cw-run.sh for the chain).
#
# Deliberately a separate submitter from batch-submit.sh rather than a flag on
# it: the two jobs share almost no shape. The GCS job fans out over six buckets,
# mounts all of them, needs highmem-16 + local SSD for DuckDB spill, and posts a
# digest. This one lists a couple of buckets on a single third-party S3 endpoint
# (`CW_BUCKETS`, specs/cw-multi-bucket.md), mounts one bucket for output, and
# peaks under 2 GB.
#
#   ./job/cw-batch-submit.sh              # submit, print job id
#   DRY=1 ./job/cw-batch-submit.sh        # print the spec (for a scheduler body)
#
# Prereq (one-time): the CAIOS S3 keypair in Secret Manager, readable by the SA.
#   printf %s "$KEY_ID" | gcloud secrets create cw-s3-access-key-id --data-file=-
#   printf %s "$SECRET" | gcloud secrets create cw-s3-secret-access-key --data-file=-
set -euo pipefail

# PIN=1: emit the canonical/cron spec — drop every ambient override so the body is
# byte-identical regardless of the caller's shell (the IaC in ops/gcp/gcs-usage
# generates the Cloud Scheduler body from `PIN=1 DRY=1`). Must precede the
# ${VAR:-default} knobs below; the python vars() honors PIN too.
if [ -n "${PIN:-}" ]; then unset IMAGE MACHINE MEMORY_MIB DATA_BUCKET CW_BUCKET; fi

PROJECT=oa-internal-450019
REGION=us-central1
SA=gcs-usage-job@$PROJECT.iam.gserviceaccount.com
IMAGE=${IMAGE:-us-central1-docker.pkg.dev/$PROJECT/cloud-run-source-deploy/gcs-usage-snapshot:cw}
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
# PIN=1 emits the canonical/cron spec: ignore ambient overrides so the body is
# byte-stable regardless of the caller's shell (see the PIN note at the top and
# ops/gcp/gcs-usage). Without PIN the knobs and passthrough list stay live.
pin = bool(os.environ.get("PIN"))
g = (lambda k, d="": d) if pin else os.environ.get
v = {
    "CW_BUCKET": g("CW_BUCKET", "marin-us-east-02a"),
    "CW_ENDPOINT": g("CW_ENDPOINT", "https://cwobject.com"),
    "DATA_BUCKET": g("DATA_BUCKET", "oa-gcs-usage-dvx"),
    "WORK_DIR": g("WORK_DIR", "/stage/cw"),
    # boto/aws-sdk needs a region even though CAIOS ignores it
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_EC2_METADATA_DISABLED": "true",
    # CAIOS rejects path-style requests, and boto's auto degrades to path
    # for custom endpoints when no ~/.aws/config says otherwise.
    "DT_S3_ADDRESSING_STYLE": "virtual",
    # Slack digest target (specs/cw-slack-digest.md): chat.postMessage (bot
    # token) so per-message avatars apply
    "SLACK_CHANNEL": g("SLACK_CHANNEL", "C0C1YR7D0KU"),  # #cw-s3-usage
    # failure alerts go to #gcs-usage-alerts (shared with the GCS job), not the digest channel
    "SLACK_ALERT_CHANNEL": g("SLACK_ALERT_CHANNEL", "C0BTUNT3B5Z"),
    # CF account for the digest plot's `wrangler pages deploy` (token is a secretVariable)
    "CLOUDFLARE_ACCOUNT_ID": g("CLOUDFLARE_ACCOUNT_ID", "74981a43be0de7712369306c7b19133d"),
}
if not pin:  # one-off overrides forwarded only for manual submits, never the cron spec
    for k in ["CW_BUCKETS", "SNAP_ID", "LISTING_PROCS", "LISTING_WORKERS", "IMPORT_JOBS"]:
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
          "AWS_SECRET_ACCESS_KEY": "projects/$PROJECT/secrets/cw-s3-secret-access-key/versions/latest",
          "SLACK_BOT_TOKEN": "projects/$PROJECT/secrets/cw-s3-slack-bot-token/versions/latest",
          "CLOUDFLARE_API_TOKEN": "projects/$PROJECT/secrets/cf-pages-token/versions/latest"
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

#!/usr/bin/env bash
# Submit the "meta" self-scan to GCP Batch: a daily scan of the storage the
# deployments themselves write — the GCS data bucket (`oa-gcs-usage-dvx`: gcs's
# `listing/` + `access/`, cw's `cw-l2/`, snapshots, sweep records) and cw's R2
# serving bucket — published as the cw-s3 deployment's secondary store `meta`
# (specs/multi-store.md phase 3; the chain is job/cw-meta-run.sh).
#
#   ./job/cw-meta-submit.sh              # submit, print job id
#   DRY=1 ./job/cw-meta-submit.sh        # print the spec (for a scheduler body)
#   PIN=1 DRY=1 ./job/cw-meta-submit.sh  # the canonical/cron body (ambient env ignored)
#
# Scheduling (once, after the first manual run is verified — the go is Ryan's):
# its own daily Cloud Scheduler job, independent of the 12-hourly `cw-usage-
# snapshot`. Under specs/batch-iac.md it is one more line in the ops Pulumi
# program (`infra/gcp/__main__.py`):
#     meta = cron("cw-meta-snapshot", "0 9 * * *", "cw-meta-submit.sh")
# which POSTs `PIN=1 DRY=1` of this script to the Batch API as the job SA. By
# hand it is the same body via `gcloud scheduler jobs create http cw-meta-snapshot
# --schedule '0 9 * * *' --time-zone Etc/UTC --uri <batch jobs URI> --http-method
# POST --oauth-service-account-email <job SA> --message-body-from-file <body>`.
# 09:00 UTC keeps it clear of both scans' 00:01 / 12:01 starts.
#
# Same image as the cw scan (`:cw`, built by `JOB=cw-run.sh job/build.sh`; it
# needs the build that carries multi-store's `dt-cloud --store`), same job SA
# (reads + writes the data bucket via the FUSE mount; lists it via ADC), and the
# `cw-s3-r2-*` secrets twice: as `R2_*` for `publish-r2`, and as `AWS_*` so the
# lister's `r2://` backend can list the R2 bucket. No CAIOS creds — this job
# never touches CoreWeave.
set -euo pipefail

# PIN=1: emit the canonical/cron spec — drop every ambient override so the body is
# byte-identical regardless of the caller's shell (see cw-batch-submit.sh).
if [ -n "${PIN:-}" ]; then unset IMAGE MACHINE MEMORY_MIB DATA_BUCKET; fi

PROJECT=oa-internal-450019
REGION=us-central1
SA=cw-s3-job@$PROJECT.iam.gserviceaccount.com
IMAGE=${IMAGE:-us-central1-docker.pkg.dev/$PROJECT/cloud-run-source-deploy/gcs-usage-snapshot:cw}
# The objects here are few and large (parquet shards, JSONs): ~4.3k in R2 and
# low millions in GCS (gcs's listing/dir-cache shards + access logs), so this is
# a fraction of the CoreWeave scan's 54 M keys. Half its machine.
MACHINE=${MACHINE:-n2-standard-4}
MEMORY_MIB=${MEMORY_MIB:-15000}
DATA_BUCKET=${DATA_BUCKET:-oa-gcs-usage-dvx}
JOB_ID=${JOB_ID:-cw-meta-scan-$(date -u +%Y%m%d-%H%M%S)}

vars() {
  python3 - <<'EOF'
import json, os
pin = bool(os.environ.get("PIN"))
g = (lambda k, d="": d) if pin else os.environ.get
v = {
    "DATA_BUCKET": g("DATA_BUCKET", "oa-gcs-usage-dvx"),
    # The scan's roots (`<scheme>://<bucket>`, space-separated; the first is the
    # store's primary root): the GCS data bucket, then cw's R2 serving bucket.
    "META_ROOTS": g("META_ROOTS", "gcs://oa-gcs-usage-dvx r2://oa-cw-s3-usage-index"),
    "WORK_DIR": g("WORK_DIR", "/stage/meta"),
    # boto needs a region even though R2 ignores it
    "AWS_DEFAULT_REGION": "auto",
    "AWS_EC2_METADATA_DISABLED": "true",
    # failure alerts go to #gcs-usage-alerts, as the other two jobs' do
    "SLACK_ALERT_CHANNEL": g("SLACK_ALERT_CHANNEL", "C0BTUNT3B5Z"),
    "CLOUDFLARE_ACCOUNT_ID": g("CLOUDFLARE_ACCOUNT_ID", "74981a43be0de7712369306c7b19133d"),
    # publish-r2's target: the same R2 bucket the primary serves from, under
    # this store's own prefixes (`meta-l2/`, `snapshots/meta/`)
    "R2_BUCKET": g("R2_BUCKET", "oa-cw-s3-usage-index"),
}
if not pin:  # one-off overrides forwarded only for manual submits, never the cron spec
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
          "commands": ["job/cw-meta-run.sh"],
          "volumes": ["/mnt/disks/gcs/$DATA_BUCKET:/gcs/$DATA_BUCKET:rw", "/mnt/disks/stage:/stage:rw"]
        }
      }],
      "computeResource": {"cpuMilli": 4000, "memoryMib": $MEMORY_MIB},
      "maxRetryCount": 0,
      "maxRunDuration": "7200s",
      "volumes": [
        {"gcs": {"remotePath": "$DATA_BUCKET"}, "mountPath": "/mnt/disks/gcs/$DATA_BUCKET", "mountOptions": ["--implicit-dirs"]},
        {"deviceName": "stage", "mountPath": "/mnt/disks/stage"}
      ],
      "environment": {
        "variables": $(vars),
        "secretVariables": {
          "AWS_ACCESS_KEY_ID": "projects/$PROJECT/secrets/cw-s3-r2-access-key-id/versions/latest",
          "AWS_SECRET_ACCESS_KEY": "projects/$PROJECT/secrets/cw-s3-r2-secret-access-key/versions/latest",
          "R2_ENDPOINT": "projects/$PROJECT/secrets/cw-s3-r2-endpoint/versions/latest",
          "R2_ACCESS_KEY_ID": "projects/$PROJECT/secrets/cw-s3-r2-access-key-id/versions/latest",
          "R2_SECRET_ACCESS_KEY": "projects/$PROJECT/secrets/cw-s3-r2-secret-access-key/versions/latest",
          "SLACK_BOT_TOKEN": "projects/$PROJECT/secrets/cw-s3-slack-bot-token/versions/latest",
          "CLOUDFLARE_API_TOKEN": "projects/$PROJECT/secrets/cf-pages-token/versions/latest",
          "GCS_USAGE_TOKEN": "projects/$PROJECT/secrets/cw-s3-job-grant/versions/latest"
        }
      }
    }
  }],
  "allocationPolicy": {
    "instances": [{
      "policy": {
        "machineType": "$MACHINE",
        "bootDisk": {"type": "pd-balanced", "sizeGb": "100"},
        "disks": [{"newDisk": {"type": "pd-ssd", "sizeGb": "100"}, "deviceName": "stage"}]
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

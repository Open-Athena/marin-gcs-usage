#!/usr/bin/env bash
# Submit the daily snapshot job to GCP Batch (n2-highmem-8, real disk for DuckDB spill).
# Cloud Run jobs cap at 32Gi with tmpfs-only storage — DuckDB spill lands in RAM and the
# 470M-row webdata step OOMs there; Batch VMs give 64G RAM + a 200G boot disk for spill.
#
# Env overrides: SNAPSHOT_DATE (default today UTC, resolved inside run.sh), IMAGE,
# DUCKDB_MEM (default 48GB), DUCKDB_THREADS (default 8), SNAP_PATH,
# LISTING_MACHINE/PROCS/WORKERS (DIY fan-out sizing) — forwarded when set.
set -euo pipefail

PROJECT=oa-internal-450019
REGION=us-central1
SA=gcs-usage-job@$PROJECT.iam.gserviceaccount.com
IMAGE=${IMAGE:-us-central1-docker.pkg.dev/$PROJECT/cloud-run-source-deploy/gcs-usage-snapshot:latest}
# tree-rows aggregates 34M+ dir groups; its hash-agg state doesn't fully spill
# and blows a 48GB DuckDB limit, so the job wants a highmem-16 (128G) machine
MACHINE=${MACHINE:-n2-highmem-16}
MEMORY_MIB=${MEMORY_MIB:-124000}
LOCAL_SSD_GB=${LOCAL_SSD_GB:-750}  # n2 16-vCPU machines require >=2 local SSDs (375G each)
JOB_ID=${JOB_ID:-gcs-usage-snapshot-$(date -u +%Y%m%d-%H%M%S)}

vars() {  # container env: defaults + optional passthroughs
  python3 - <<'EOF'
import json, os
v = {
    # 90 (not 100): DuckDB at its cap plus process overhead must fit the
    # container — 100GB inside 117GiB got kernel-OOM-killed (exit 137) twice
    "DUCKDB_MEM": os.environ.get("DUCKDB_MEM", "90GB"),
    # access ingest runs before (not concurrent with) the webdata step, so
    # it can take a big slice of the 128G node; 24GB OOM'd on a row-heavy
    # 53GB chunk (2026-08-23)
    "DUCKDB_MEM_ACCESS": os.environ.get("DUCKDB_MEM_ACCESS", "48GB"),
    "DUCKDB_THREADS": os.environ.get("DUCKDB_THREADS", "16"),
    "DATA_BUCKET": os.environ.get("DATA_BUCKET", "oa-gcs-usage-dvx"),
    # stage inputs to the local-SSD mount (see disks below); STAGE_DIR="" disables
    "STAGE_DIR": os.environ.get("STAGE_DIR", "/stage"),
    # Slack digest target: chat.postMessage (bot token) so per-message avatars apply
    "SLACK_CHANNEL": os.environ.get("SLACK_CHANNEL", "C0BNWAASXFW"),  # #gcs-usage
    # CF account for index-sync's `wrangler d1 execute` (token is a secretVariable)
    "CLOUDFLARE_ACCOUNT_ID": os.environ.get("CLOUDFLARE_ACCOUNT_ID", "74981a43be0de7712369306c7b19133d"),
}
v = {k: s for k, s in v.items() if s}
for k in ["SNAPSHOT_DATE", "SNAP_PATH", "REPROC",
          "ACCESS_ONLY", "SKIP_ACCESS", "ACCESS_ARGS",
          "LISTING_MACHINE", "LISTING_PROCS", "LISTING_WORKERS",
          "GCS_ALERT_CEILING_TB", "GCS_ALERT_SPIKE_PCT"]:  # SLACK_WEBHOOK is a secretVariable (see below)
    if k in os.environ:
        v[k] = os.environ[k]
print(json.dumps(v))
EOF
}

# Batch only auto-creates host mount dirs under /mnt/disks — mount there, then map
# into the container at the /gcs/<bucket> paths run.sh expects.
BUCKETS_PY='["marin-us-east1", "marin-us-east5", "marin-us-central1",
             "marin-us-central2", "marin-eu-west4", "marin-us-west4",
             "oa-gcs-usage-dvx"]'

volumes() {
  python3 - <<EOF
import json
print(json.dumps([
    {"gcs": {"remotePath": b}, "mountPath": f"/mnt/disks/gcs/{b}", "mountOptions": ["--implicit-dirs"]}
    for b in $BUCKETS_PY
]))
EOF
}

container_volumes() {
  python3 - <<EOF
import json
print(json.dumps([f"/mnt/disks/gcs/{b}:/gcs/{b}:rw" for b in $BUCKETS_PY]
                 + ["/mnt/disks/stage:/stage:rw"]))
EOF
}

spec=$(mktemp)
cat > "$spec" <<EOF
{
  "taskGroups": [{
    "taskCount": 1,
    "taskSpec": {
      "runnables": [{"container": {"imageUri": "$IMAGE", "volumes": $(container_volumes)}}],
      "computeResource": {"cpuMilli": 8000, "memoryMib": $MEMORY_MIB},
      "maxRetryCount": 0,
      "maxRunDuration": "${MAX_RUN_SECONDS:-21600}s",
      "volumes": $(volumes | python3 -c 'import json,sys; v=json.load(sys.stdin); v.append({"deviceName": "stage", "mountPath": "/mnt/disks/stage"}); print(json.dumps(v))'),
      "environment": {
        "variables": $(vars),
        "secretVariables": {
          "SLACK_BOT_TOKEN": "projects/$PROJECT/secrets/gcs-alert-slack-bot-token/versions/latest",
          "SLACK_WEBHOOK": "projects/$PROJECT/secrets/gcs-alert-slack-webhook/versions/latest",
          "CLOUDFLARE_API_TOKEN": "projects/$PROJECT/secrets/cf-pages-token/versions/latest",
          "GCS_USAGE_TOKEN": "projects/$PROJECT/secrets/gcs-sheet-sync-token/versions/latest"
        }
      }
    }
  }],
  "allocationPolicy": {
    "instances": [{
      "policy": {
        "machineType": "$MACHINE",
        "bootDisk": {"type": "pd-balanced", "sizeGb": "100"},
        "disks": [{"newDisk": {"type": "local-ssd", "sizeGb": "$LOCAL_SSD_GB"}, "deviceName": "stage"}]
      }
    }],
    "serviceAccount": {"email": "$SA"},
    "location": {"allowedLocations": ["regions/$REGION"]}
  },
  "logsPolicy": {"destination": "CLOUD_LOGGING"}
}
EOF

# DRY=1: print the job spec (e.g. for the Cloud Scheduler daily body) instead of submitting
if [ -n "${DRY:-}" ]; then cat "$spec"; rm -f "$spec"; exit 0; fi
gcloud batch jobs submit "$JOB_ID" --project "$PROJECT" --location "$REGION" --config "$spec" >&2
rm -f "$spec"
echo "$JOB_ID"

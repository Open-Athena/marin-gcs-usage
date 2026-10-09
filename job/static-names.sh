#!/usr/bin/env bash
# The static name-search build (specs/architecture/static-name-search.md, "Pipeline") on GCP Batch in
# us-east1, beside the data bucket. Every stage is `dt-cloud static-names …` from this checkout's
# committed `cloud/src/dt_cloud` (staged content-addressed by its git tree), in the pinned job image,
# with the bucket mounted at /gcs/$B and a local SSD at /stage.
#
#   job/static-names.sh stage-src                 # upload HEAD's dt_cloud tree → gs://$B/static-names/src/<tree>/ (prints <tree>)
#   job/static-names.sh run KIND TASKS ARGS…      # one Batch job of TASKS tasks, each `python -m dt_cloud.$MODULE KIND -m /gcs/$B ARGS…`
#                                                 # (MODULE: static_names, static_catalog or static_append); the scratch bucket ($S: the suffix shuffle and
#                                                 # other intermediates, 7-day expiry, no soft delete) is mounted at /gcs/$S;
#                                                 # waits for it and exits nonzero unless every task succeeded
#   job/static-names.sh wait JOB                  # wait for a submitted job
#   job/static-names.sh r2 GEN [ARGS…]            # `static-names r2-copy -g GEN` on the ch-store VM (it holds the R2 keys, /data/r2-index.env),
#                                                 # from the staged source tree; GCS → R2 bucket oa-gcs-usage-index, idempotent
#   job/static-names.sh r2-batch GEN [ARGS…]      # the same copy as a 1-task Batch job, the R2 key from Secret Manager
#                                                 # (`gcs-static-index-r2-{key-id,secret}`); R2_ENDPOINT or CLOUDFLARE_ACCOUNT_ID
#   job/static-names.sh ch-answers DATES TERMS    # reference answers from the ch-store VM's ClickHouse (`mega_names.answer`, postings `m`),
#                                                 # read-only, sequential; DATES comma-separated, TERMS a file of literals; JSON lines on stdout
#
# Env: MODULE (static_names), MACHINE (n2-highmem-16), SSD (750; n2 16-vCPU needs ≥2 local SSDs of 375), PARALLELISM (TASKS), SPOT (1: spot VMs,
# 3 retries), MAX_RUN_SECONDS (14400), IMAGE (the pinned job image digest), SRC (a staged tree; default HEAD's; `image`: the image's
# own installed dt_cloud + pyrmts, for a job image built from the lock), NO_MOUNT=1 (no `-m`), ENV_JSON (the tasks'
# Batch `environment`), JOB_ID, DRY=1.
set -euo pipefail
cd "$(dirname "$0")/.."
PROJECT=oa-internal-450019
REGION=us-east1
B=oa-gcs-usage-dvx
S=oa-gcs-usage-scratch
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

# `pyrmts` (pure Python; `pyrmts.intervals`) is not in the job image: stage the locked venv's copy, content-addressed by
# the archive commit `cloud/pyproject.toml` pins, and put it on the tasks' PYTHONPATH beside `dt_cloud`.
pyrmts_rev() { grep -o 'pyrmts/archive/[0-9a-f]*' cloud/pyproject.toml | head -1 | cut -d/ -f3; }
stage_pyrmts() {
  local r d src
  r=$(pyrmts_rev)
  if gcloud storage ls "gs://$B/static-names/src/pyrmts-$r/pyrmts/intervals.py" > /dev/null 2>&1; then echo "$r"; return; fi
  src=$(${PYTHON:-.venv/bin/python} -c 'import os, pyrmts; print(os.path.dirname(pyrmts.__file__))')
  d=tmp/static-names-src/pyrmts-$r
  rm -rf "$d" && mkdir -p "$d"
  cp -r "$src" "$d/pyrmts" && find "$d" -name __pycache__ -prune -exec rm -rf {} +
  gcloud storage rsync -r --verbosity=error "$d/pyrmts" "gs://$B/static-names/src/pyrmts-$r/pyrmts" > /dev/null
  echo "$r"
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

case ${1:?stage-src|run|r2|r2-batch|wait|ch-answers} in
stage-src) stage_src ;;
r2)
  GEN=${2:?GEN}
  shift 2
  SRC=${SRC:-$(stage_src)}
  EXTRA=""
  if [ "$#" -gt 0 ]; then EXTRA=$(printf '%q ' "$@"); fi
  job/ch-store.sh sh "sudo rm -rf /data/sn/src && sudo mkdir -p /data/sn/src && sudo gcloud storage cp -r --verbosity=error gs://$B/static-names/src/$SRC/dt_cloud /data/sn/src/ && \
    sudo docker run --rm --network host -v /data:/data -e PYTHONPATH=/data/sn/src:/data/src --env-file /data/r2-index.env \
      -e R2_ENDPOINT=https://74981a43be0de7712369306c7b19133d.r2.cloudflarestorage.com -e R2_BUCKET=oa-gcs-usage-index \
      --entrypoint nice \$(cat /data/image) -n 10 python3 -u -m dt_cloud.static_names r2-copy -g $GEN $EXTRA"
  ;;
r2-batch)
  # The same copy as a 1-task Batch job: the R2 key from Secret Manager (`gcs-static-index-r2-{key-id,secret}`,
  # a token for bucket oa-gcs-usage-index only), the endpoint from R2_ENDPOINT (or CLOUDFLARE_ACCOUNT_ID).
  GEN=${2:?GEN}
  shift 2
  R2_ENDPOINT=${R2_ENDPOINT:-https://${CLOUDFLARE_ACCOUNT_ID:?R2_ENDPOINT or CLOUDFLARE_ACCOUNT_ID}.r2.cloudflarestorage.com}
  ENV_JSON=$(python3 -c 'import json, sys
s = "projects/%s/secrets/gcs-static-index-r2-%s/versions/latest"
print(json.dumps({"variables": {"R2_ENDPOINT": sys.argv[1], "R2_BUCKET": "oa-gcs-usage-index"},
                  "secretVariables": {"AWS_ACCESS_KEY_ID": s % (sys.argv[2], "key-id"), "AWS_SECRET_ACCESS_KEY": s % (sys.argv[2], "secret")}}))' "$R2_ENDPOINT" "$PROJECT")
  ENV_JSON=$ENV_JSON MODULE=static_names NO_MOUNT=1 MACHINE=${R2_MACHINE:-n2-highmem-4} SSD=375 PARALLELISM=1 \
    JOB_ID=${JOB_ID:-sn-r2-$(date -u +%Y%m%d-%H%M%S)} exec "$0" run r2-copy 1 -g "$GEN" "$@"
  ;;
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
  JOB_ID=${JOB_ID:-sn-$KIND-$(date -u +%Y%m%d-%H%M%S)}
  CPU=$(( ${MACHINE##*-} * 1000 ))
  MEM_MIB=$(( ${MACHINE##*-} * 7700 ))
  MOUNT_ARG=""
  if [ -z "${NO_MOUNT:-}" ]; then MOUNT_ARG="-m /gcs/$B"; fi
  if [ "${SRC:-}" = image ]; then
    cmd="set -euo pipefail; mkdir -p /stage/tmp /stage/out && cd /stage && \
python3 -u -m dt_cloud.${MODULE:-static_names} $KIND $MOUNT_ARG $(printf '%q ' "$@")"
  else
    SRC=${SRC:-$(stage_src)}
    PYRMTS=$(stage_pyrmts)
    cmd="set -euo pipefail; mkdir -p /stage/src /stage/tmp /stage/out && cp -r /gcs/$B/static-names/src/$SRC/dt_cloud /gcs/$B/static-names/src/pyrmts-$PYRMTS/pyrmts /stage/src/ && cd /stage && \
PYTHONPATH=/stage/src python3 -u -m dt_cloud.${MODULE:-static_names} $KIND $MOUNT_ARG $(printf '%q ' "$@")"
  fi
  ENVJ=${ENV_JSON:-'{}'}
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
        "volumes": ["/mnt/disks/gcs/$B:/gcs/$B:ro", "/mnt/disks/gcs/$S:/gcs/$S:ro", "/mnt/disks/stage:/stage:rw"]
      }}],
      "environment": $ENVJ,
      "computeResource": {"cpuMilli": $CPU, "memoryMib": $MEM_MIB},
      "maxRetryCount": $RETRIES,
      "maxRunDuration": "${MAX_RUN_SECONDS:-14400}s",
      "volumes": [
        {"gcs": {"remotePath": "$B"}, "mountPath": "/mnt/disks/gcs/$B", "mountOptions": ["--implicit-dirs"]},
        {"gcs": {"remotePath": "$S"}, "mountPath": "/mnt/disks/gcs/$S", "mountOptions": ["--implicit-dirs"]},
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

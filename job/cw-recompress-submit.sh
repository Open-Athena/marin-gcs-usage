#!/usr/bin/env bash
# Submit a GCP Batch array job that rewrites cw's old-format (v1) layer-2
# listings in place as v2 — `disk-tree recompress` (specs/listing-slim.md
# phase 2): drops the derivable `uri` column and the class pivot that equals
# `size`, re-encodes in 64K-row groups, verifies row count + a content digest
# against the original, then swaps. Lossless; a file that fails verification is
# left untouched and reported. Already-v2 files are skipped, so the job is
# idempotent and re-runnable. Measured on cw's 2026-09-29T1201 listing: the
# v2 file is ≈ 61 % of the original under Snappy (the default; zstd is off
# until `DISK_TREE_PARQUET_CODEC=zstd` and the site's viewer can read it —
# then ≈ 23 %). Nothing served reads these files (the site reads the index
# tiers), so a rewrite is invisible to viewers.
#
#   job/cw-recompress-submit.sh 2026-08-19T0303 2026-08-20T0001   # these scans
#   job/cw-recompress-submit.sh $(job/cw-recompress-submit.sh -l)  # every v1 scan
#   job/cw-recompress-submit.sh -l                                 # list scan dirs (local, no submit)
#   DRY=1 job/cw-recompress-submit.sh …                            # print the body only
#   RECOMPRESS_DRY=1 job/cw-recompress-submit.sh …                 # tasks run `recompress -n` (report, write nothing)
#   PARALLELISM=8 job/cw-recompress-submit.sh …                    # concurrent tasks (default 4)
#
# One task per scan dir (`gs://$DATA_BUCKET/cw-l2/<scan>/`, its bucket parquets;
# the `index/` tiers underneath are not listings and are skipped by the format
# check). Each task streams through the GCS API (no bucket mount) as the job SA.
# **The R2 mirror is not touched**: if raw listings keep being mirrored,
# `job/cw-publish-submit.sh <scans>` re-syncs them afterwards (idempotent by
# size + md5, so it re-uploads exactly the rewritten files); if the mirror is to
# drop raw listings instead, that is a `publish-r2` prefix change, not this job.
#
# Needs the `:cw` image built from a tree that has `disk-tree recompress`
# (cloud `e957f8d` or later). Run `-l`'s first scan alone before the rest, and
# read its report (`recompress` prints old → new per file and a totals line).
set -euo pipefail
PROJECT=oa-internal-450019
REGION=us-central1
SA=gcs-usage-job@$PROJECT.iam.gserviceaccount.com
IMAGE=${IMAGE:-us-central1-docker.pkg.dev/$PROJECT/cloud-run-source-deploy/gcs-usage-snapshot:cw}
DATA_BUCKET=${DATA_BUCKET:-oa-gcs-usage-dvx}
PARALLELISM=${PARALLELISM:-4}
JOB_ID=${JOB_ID:-cw-recompress-$(date -u +%Y%m%d-%H%M%S)}

if [ "${1:-}" = "-l" ]; then
  # Every scan dir under cw-l2/, oldest first (the caller picks; `recompress`
  # itself skips v2 files, so passing all of them is safe).
  # (scan ids only — `cw-l2/` also holds non-scan dirs such as `over-time-dev/`)
  gcloud storage ls "gs://$DATA_BUCKET/cw-l2/" | sed -n 's#^gs://[^/]*/cw-l2/\([0-9]\{4\}-[0-9][0-9]-[0-9][0-9]T[0-9]\{4\}\)/$#\1#p' | sort
  exit 0
fi
if [ $# -eq 0 ]; then echo "usage: $0 <scan-id>… | -l" >&2; exit 2; fi
SCANS=("$@")
export PROJECT SA IMAGE DATA_BUCKET PARALLELISM
body=$(python3 - "${SCANS[@]}" <<'EOF'
import json, os, sys
scans = sys.argv[1:]
flag = "-n " if os.environ.get("RECOMPRESS_DRY") else ""
cmd = (
    "set -euo pipefail\n"
    f"SCANS=({' '.join(scans)})\n"
    'SNAP_ID=${SCANS[$BATCH_TASK_INDEX]}\n'
    'DIR="gs://$DATA_BUCKET/cw-l2/$SNAP_ID"\n'
    'echo "task $BATCH_TASK_INDEX → recompress $DIR"\n'
    # the bucket parquets sit directly in the scan dir; index/<gen>/ tiers are
    # not listings (`recompress` refuses them, exit 1) so only the top level is
    # passed — globbed with gcsfs (the image has no gcloud CLI)
    'FILES=$(python3 -c "import gcsfs,sys; print(\\" \\".join(\\"gs://\\"+p for p in gcsfs.GCSFileSystem().glob(sys.argv[1]+\\"/*.parquet\\")))" "$DIR")\n'
    '[ -n "$FILES" ] || { echo "no parquets under $DIR" >&2; exit 1; }\n'
    'disk-tree listing-format $FILES\n'
    f'disk-tree recompress {flag}$FILES\n'
)
spec = {
    "taskGroups": [{
        "taskCount": len(scans),
        "parallelism": int(os.environ["PARALLELISM"]),
        "taskSpec": {
            "runnables": [{"container": {"imageUri": os.environ["IMAGE"], "entrypoint": "bash", "commands": ["-c", cmd]}}],
            # one decoded source row group (≤ 1 M rows of the oldest blobs) + one 64K output batch
            "computeResource": {"cpuMilli": 2000, "memoryMib": 7000},
            "maxRetryCount": 0,
            "maxRunDuration": "7200s",
            "environment": {"variables": {"DATA_BUCKET": os.environ["DATA_BUCKET"]}},
        },
    }],
    "allocationPolicy": {
        "instances": [{"policy": {"machineType": "n2-standard-2", "bootDisk": {"type": "pd-balanced", "sizeGb": "50"}}}],
        "serviceAccount": {"email": os.environ["SA"]},
        "location": {"allowedLocations": [f"regions/{os.environ.get('REGION', 'us-central1')}"]},
    },
    "logsPolicy": {"destination": "CLOUD_LOGGING"},
}
print(json.dumps(spec, indent=2))
EOF
)
if [ -n "${DRY:-}" ]; then echo "$body"; exit 0; fi
spec=$(mktemp); echo "$body" > "$spec"
gcloud batch jobs submit "$JOB_ID" --project "$PROJECT" --location "$REGION" --config "$spec" >&2
rm -f "$spec"
echo "$JOB_ID"

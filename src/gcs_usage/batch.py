"""GCP Batch job specs + submit/wait for the DIY fleet-listing fan-out.

DIY mode lists all buckets ourselves instead of depending on SII reports
(whose generation times scatter 02:26-13:00 UTC, lag ~30h on day one, and are
plain unavailable in us-central2). Buckets are embarrassingly parallel — one
Batch *task* per bucket (``BATCH_TASK_INDEX`` picks from the bucket list), so
fleet wall-clock ~= the slowest bucket. Within a bucket, ``list-bucket``'s
own procs x threads prefix streams do the scaling.
"""
from __future__ import annotations

import time
from typing import Sequence

from .gcp import PROJECT, REGION, batch_job, session

FLEET_BUCKETS = [
    "marin-us-central2",
    "marin-eu-west4",
    "marin-us-central1",
    "marin-us-east5",
    "marin-us-east1",
    "marin-us-west4",
]
DATA_BUCKET = "oa-gcs-usage-dvx"
SERVICE_ACCOUNT = f"gcs-usage-job@{PROJECT}.iam.gserviceaccount.com"
IMAGE = f"us-central1-docker.pkg.dev/{PROJECT}/cloud-run-source-deploy/gcs-usage-snapshot:latest"


def listing_dir(data_bucket: str, date: str, bucket: str) -> str:
    """Canonical per-bucket listing location (DIY layout)."""
    return f"{data_bucket}/listing/{date}/{bucket}"


def listing_job_spec(
    date: str,
    buckets: Sequence[str] = tuple(FLEET_BUCKETS),
    data_bucket: str = DATA_BUCKET,
    machine: str = "n2-standard-16",
    procs: int = 12,
    threads: int = 10,
) -> dict:
    """One task per bucket; each runs ``list-bucket`` straight to gs://.

    Weights come from the newest prior completed listing of the same bucket
    (checked in the DIY layout, then the legacy ``central2-listing/`` one),
    found at runtime via the FUSE mount of the data bucket — the only volume
    the tasks need (object pages stream via the API, shards write via gs://).
    """
    script = f"""#!/usr/bin/env bash
set -euxo pipefail
BUCKETS=({" ".join(buckets)})
b=${{BUCKETS[$BATCH_TASK_INDEX]}}
W=()
for d in $(ls -d /gcs/{data_bucket}/listing/*/$b /gcs/{data_bucket}/central2-listing/* 2>/dev/null | sort -r); do
  case "$d" in */listing/{date}/$b) continue;; */central2-listing/*) [ "$b" = marin-us-central2 ] || continue;; esac
  if [ -f "$d/_SUCCESS.json" ]; then W=(-W "$d/*.parquet"); break; fi
done
gcs-usage list-bucket "$b" -o "gs://{listing_dir(data_bucket, date, "$b")}" -P {procs} -w {threads} -x reuse "${{W[@]}}"
"""
    return {
        "taskGroups": [
            {
                "taskCount": len(buckets),
                "parallelism": len(buckets),
                "taskSpec": {
                    "runnables": [
                        {
                            "container": {
                                "imageUri": IMAGE,
                                "entrypoint": "/bin/bash",
                                "commands": ["-c", script],
                                "volumes": [f"/mnt/disks/gcs/{data_bucket}:/gcs/{data_bucket}:rw"],
                            }
                        }
                    ],
                    "computeResource": {"cpuMilli": 15000, "memoryMib": 24000},
                    "maxRetryCount": 1,
                    "maxRunDuration": "14400s",
                    "volumes": [
                        {
                            "gcs": {"remotePath": data_bucket},
                            "mountPath": f"/mnt/disks/gcs/{data_bucket}",
                            "mountOptions": ["--implicit-dirs"],
                        }
                    ],
                },
            }
        ],
        "allocationPolicy": {
            "instances": [{"policy": {"machineType": machine, "bootDisk": {"type": "pd-balanced", "sizeGb": "50"}}}],
            "serviceAccount": {"email": SERVICE_ACCOUNT},
            "location": {"allowedLocations": [f"regions/{REGION}"]},
        },
        "logsPolicy": {"destination": "CLOUD_LOGGING"},
    }


def submit_job(spec: dict, job_id: str | None = None) -> str:
    """POST a Batch job; returns its short name (server-generated if no id)."""
    url = f"https://batch.googleapis.com/v1/projects/{PROJECT}/locations/{REGION}/jobs"
    params = {"job_id": job_id} if job_id else None
    r = session().post(url, json=spec, params=params)
    r.raise_for_status()
    return r.json()["name"].rsplit("/", 1)[-1]


def wait_job(name: str, interval: int = 60, log=None) -> str:
    """Poll a Batch job to a terminal state; returns the final state."""
    while True:
        state = batch_job(name)["status"].get("state", "?")
        if log:
            log(f"{name}: {state}")
        if state in ("SUCCEEDED", "FAILED", "DELETION_IN_PROGRESS"):
            return state
        time.sleep(interval)

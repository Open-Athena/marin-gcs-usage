from gcs_usage.batch import FLEET_BUCKETS, listing_dir, listing_job_spec


def test_listing_dir():
    assert listing_dir("oa-gcs-usage-dvx", "2026-07-30", "marin-us-east1") == (
        "oa-gcs-usage-dvx/listing/2026-07-30/marin-us-east1"
    )


def test_listing_job_spec_task_per_bucket():
    spec = listing_job_spec("2026-07-30", ["marin-us-east1", "marin-us-west4"], procs=4, threads=3)
    tg = spec["taskGroups"][0]
    assert (tg["taskCount"], tg["parallelism"]) == (2, 2)
    container = tg["taskSpec"]["runnables"][0]["container"]
    assert container["entrypoint"] == "/bin/bash"
    assert container["volumes"] == ["/mnt/disks/gcs/oa-gcs-usage-dvx:/gcs/oa-gcs-usage-dvx:rw"]
    assert container["commands"][0] == "-c"
    assert container["commands"][1] == (
        "#!/usr/bin/env bash\n"
        "set -euxo pipefail\n"
        "BUCKETS=(marin-us-east1 marin-us-west4)\n"
        "b=${BUCKETS[$BATCH_TASK_INDEX]}\n"
        "W=()\n"
        "for d in $(ls -d /gcs/oa-gcs-usage-dvx/listing/*/$b /gcs/oa-gcs-usage-dvx/central2-listing/* 2>/dev/null | sort -r); do\n"
        '  case "$d" in */listing/2026-07-30/$b) continue;; */central2-listing/*) [ "$b" = marin-us-central2 ] || continue;; esac\n'
        '  if [ -f "$d/_SUCCESS.json" ]; then W=(-W "$d/*.parquet"); break; fi\n'
        "done\n"
        'gcs-usage list-bucket "$b" -o "gs://oa-gcs-usage-dvx/listing/2026-07-30/$b" -P 4 -w 3 -x reuse "${W[@]}"\n'
    )
    assert tg["taskSpec"]["volumes"] == [
        {
            "gcs": {"remotePath": "oa-gcs-usage-dvx"},
            "mountPath": "/mnt/disks/gcs/oa-gcs-usage-dvx",
            "mountOptions": ["--implicit-dirs"],
        }
    ]
    policy = spec["allocationPolicy"]["instances"][0]["policy"]
    assert policy == {"machineType": "n2-standard-16", "bootDisk": {"type": "pd-balanced", "sizeGb": "50"}}


def test_listing_job_spec_default_fleet():
    spec = listing_job_spec("2026-07-30")
    assert spec["taskGroups"][0]["taskCount"] == len(FLEET_BUCKETS) == 6

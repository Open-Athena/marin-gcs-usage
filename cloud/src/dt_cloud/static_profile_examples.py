"""Example deployment profiles for the static name index (`static_profile.Profile`), selected with
`STATIC_NAMES_PROFILE=<name>`: Open Athena's two deployments, as full worked examples. Any field can be overridden by
its env var (`static_profile.ENV`); the R2 endpoint (it names the Cloudflare account) is always env (`R2_ENDPOINT`).
"""
from __future__ import annotations

from .static_profile import Profile

PROJECT = "oa-internal-450019"
#: The job image both build with (duckdb 1.5.6, pyarrow 22.0.0), pinned by digest so a rerun computes with the same engines.
IMAGE = f"us-central1-docker.pkg.dev/{PROJECT}/cloud-run-source-deploy/gcs-usage-snapshot@sha256:6974a8b71e56469bb8180495c13c170decdc10aefe695cd0f8c5200fb3ac520a"

#: GCS buckets: scans listed per scan id (`listing/<id>/`, before generations a single `path-index.parquet`); each run gets
#: the heavy-term drilldown.
GCS = Profile(
    name="gcs",
    layouts=("listing/{id}/path-index.parquet", "listing/{id}/index/{gen}/path-index.parquet"),
    bucket="oa-gcs-usage-dvx", scratch="oa-gcs-usage-scratch", gen="2026-10-08c",
    r2_bucket="oa-gcs-usage-index", r2_secrets={"key_id": "gcs-static-index-r2-key-id", "secret": "gcs-static-index-r2-secret"},
    project=PROJECT, region="us-east1", image=IMAGE, sa=f"gcs-usage-job@{PROJECT}.iam.gserviceaccount.com", drill=True,
)

#: CoreWeave S3 buckets: scans every 6 h (`cw-l2/<id>/index/<gen>/`), the same data and scratch buckets as gcs, its own
#: R2 bucket; the stages run as the account with the scratch bucket, the R2 copy as cw's job account with its R2 key.
CW = Profile(
    name="cw",
    layouts=("cw-l2/{id}/index/{gen}/path-index.parquet",),
    bucket="oa-gcs-usage-dvx", scratch="oa-gcs-usage-scratch", gen="2026-10-09cw",
    r2_bucket="oa-cw-s3-usage-index",
    r2_secrets={"endpoint": "cw-s3-r2-endpoint", "key_id": "cw-s3-r2-access-key-id", "secret": "cw-s3-r2-secret-access-key"},
    project=PROJECT, region="us-east1", image=IMAGE, sa=f"gcs-usage-job@{PROJECT}.iam.gserviceaccount.com",
    r2_sa=f"cw-s3-job@{PROJECT}.iam.gserviceaccount.com",
)

EXAMPLES = {p.name: p for p in (GCS, CW)}

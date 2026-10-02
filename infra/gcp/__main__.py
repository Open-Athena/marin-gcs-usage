"""cw-s3's GCP stack: the CoreWeave scan jobs' account, secrets, crons and grants.

Thin wiring over `gcp_jobs.py` (the shared components, from `cloud`). Each cron's
Batch spec comes from this branch's own `job/*-submit.sh` under `PIN=1 DRY=1`.

The jobs run as their own account, `cw-s3-job`, which this stack creates; the
secrets and crons predate it and are adopted. `legacyJobAccess` keeps gcs's
`gcs-usage-job` on the cw secrets through the cutover: adopt with it on, then
turn it off and `up` to drop those bindings.

CI and agents run `pulumi preview` only; `up` is a human's call.
"""

from pathlib import Path

import pulumi

from gcp_jobs import Adopt, BatchCron, JobAccount, Secrets, grant_bucket, grant_secret

cfg = pulumi.Config()
gcp_cfg = pulumi.Config("gcp")
project = gcp_cfg.require("project")
region = gcp_cfg.require("region")
adopt = Adopt(cfg.get_bool("adopting") or False)
job_dir = Path(__file__).resolve().parents[2] / "job"

STACK = "cw-s3"
if pulumi.get_stack() != STACK:
    raise ValueError(f"this branch's gcp/ manages only the {STACK!r} stack, not {pulumi.get_stack()!r}")

job = JobAccount(
    "cw-s3-job",
    project=project,
    account_id="cw-s3-job",
    display_name="cw-s3 CoreWeave scan jobs",
    roles=[
        "roles/artifactregistry.reader",
        "roles/batch.agentReporter",
        "roles/batch.jobsEditor",
        "roles/logging.logWriter",
    ],
    adopt=adopt,
)

CW_SECRETS = [
    "cw-s3-access-key-id",
    "cw-s3-secret-access-key",
    "cw-s3-slack-bot-token",
    "cw-s3-job-grant",
    "cw-s3-r2-access-key-id",
    "cw-s3-r2-secret-access-key",
    "cw-s3-r2-endpoint",
]
secrets = Secrets(
    "cw-secrets",
    project=project,
    secrets={sid: None for sid in CW_SECRETS},
    accessor=job.member,
    accessor_email=job.email_literal,
    adopt=adopt,
    existing=True,
    grants_existing=False,
)
# The Cloudflare token is gcs's secret (its stack owns the container); the cw
# jobs read it too.
cf_token = grant_secret("cf-pages-token-cw-accessor", project=project, secret_id="cf-pages-token", member=job.member, member_email=job.email_literal, adopt=adopt)

legacy = "gcs-usage-job@" + f"{project}.iam.gserviceaccount.com"
if cfg.get_bool("legacyJobAccess"):
    for sid in CW_SECRETS:
        grant_secret(f"{sid}-legacy-accessor", project=project, secret_id=sid, member=f"serviceAccount:{legacy}", member_email=legacy, adopt=adopt, existing=True)

# The jobs' work dir and snapshots live in gcs's data bucket (gcs's stack owns it).
data = grant_bucket("oa-gcs-usage-dvx-cw-job", bucket="oa-gcs-usage-dvx", role="roles/storage.objectAdmin", member=job.member, member_email=job.email_literal, adopt=adopt)

ready = [job, secrets, cf_token, data]
scan = BatchCron(
    "cw-usage-snapshot",
    project=project,
    region=region,
    schedule="0 */12 * * *",
    submitter=job_dir / "cw-batch-submit.sh",
    sa_email=job.email,
    adopt=adopt,
    existing=True,
    depends_on=ready,
)
meta = BatchCron(
    "cw-meta-snapshot",
    project=project,
    region=region,
    schedule="0 9 * * *",
    submitter=job_dir / "cw-meta-submit.sh",
    sa_email=job.email,
    description="Daily meta self-scan (job/cw-meta-submit.sh PIN=1 DRY=1 body) -> cw-s3.oa.dev/meta",
    adopt=adopt,
    existing=True,
    depends_on=ready,
)

pulumi.export("job_account", job.email)
pulumi.export("crons", [scan.job.name, meta.job.name])

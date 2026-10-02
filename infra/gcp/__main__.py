"""cw-s3's GCP stack: the CoreWeave scan jobs' account, secrets, crons and grants.

Thin wiring over `gcp_jobs.py` (the shared components, from `cloud`). Each cron's
Batch spec comes from this branch's own `job/*-submit.sh` under `PIN=1 DRY=1`.

The jobs run as their own account, `cw-s3-job`, and the site's sweep console
dispatches them as `cw-s3-dispatch` (its key is the Pages secret `GCP_SA_KEY`;
keys stay out of state); this stack creates both. The secrets and crons predate
them and are adopted. `legacyJobAccess` bridges the cutover: gcs's
`gcs-usage-job` keeps the cw secrets, and gcs's `gcs-usage-dispatch` (the key
cw-s3's Pages has now) may act as `cw-s3-job`. Adopt with it on; once the Pages
key is `cw-s3-dispatch`'s and a cron run is good, turn it off and `up`.

CI and agents run `pulumi preview` only; `up` is a human's call.
"""

from pathlib import Path

import pulumi
import pulumi_gcp as gcp

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

dispatch = JobAccount(
    "cw-s3-dispatch",
    project=project,
    account_id="cw-s3-dispatch",
    display_name="cw-s3 sweep dispatch",
    description="Submits sweep-executor Batch jobs from the cw-s3 sweep console; Batch-submit + actAs cw-s3-job only",
    roles=["roles/batch.jobsEditor"],
    acts_as_self=False,
    adopt=adopt,
)
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
    actors=["cw-s3-dispatch@" + f"{project}.iam.gserviceaccount.com"],
    actor_deps=[dispatch],
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
legacy_dispatch = "gcs-usage-dispatch@" + f"{project}.iam.gserviceaccount.com"
if cfg.get_bool("legacyJobAccess"):
    for sid in CW_SECRETS:
        grant_secret(f"{sid}-legacy-accessor", project=project, secret_id=sid, member=f"serviceAccount:{legacy}", member_email=legacy, adopt=adopt, existing=True)
    gcp.serviceaccount.IAMMember(
        "cw-s3-job-legacy-dispatch",
        service_account_id=job.account.name,
        role="roles/iam.serviceAccountUser",
        member=f"serviceAccount:{legacy_dispatch}",
    )

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

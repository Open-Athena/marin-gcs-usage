# Cloudflare resources as code (Pulumi) — inventory + recommendation

Written 2026-08-28 after splitting cw-s3.oa.dev onto its own Pages project by
hand (~12 API/CLI calls across two tokens). That exercise *is* the argument.

## Where things are deployed today

| resource | where | how it's managed |
|---|---|---|
| Pages project `oa-gcs-usage` (gcs.oa.dev) | CF account `74981a43…` | `site/deploy` (wrangler) on `gcs`; secrets by hand (`wrangler pages secret put`) |
| Pages project `oa-cw-s3-usage` (cw-s3.oa.dev) | same | `site/deploy` on `cw-s3`; secrets copied by hand today |
| Custom domains + `oa.dev` CNAMEs | Pages API + zone DNS | hand (API calls today; console before) |
| Access apps `e18304ed` (gcs `/auth/sso` SSO IdP) + `4c463052` (cw whole-host) + policies + IdPs | Zero Trust org `openathena-ai-pages` | hand (API/console); the D1 allowlist made the gcs policy include=Everyone |
| D1 `oa-gcs-usage-auth` + migrations | CF | `wrangler d1 migrations apply` (schema in repo; instance by hand) |
| Bindings/vars (`ACCESS_AUD`, `STAFF_DOMAIN`, D1 binding) | `site/wrangler.toml` | **in repo** — the only CF config that is |
| Job image + Batch + Scheduler crons + Secret Manager | GCP `oa-internal-450019` | `job/build.sh`, `batch-submit.sh`; cron bodies edited in place (drift bit us twice: `LISTING_MODE` 8/10, `DUCKDB_MEM` 8/28) |
| Pulumi (`~/c/oa/ops`) | AWS `oa-ci`/`oa-management`, GCP `golink` | **nothing for CF or this project yet** |

## Recommendation

**Yes — one Pulumi stack per deployment branch, in `~/c/oa/ops` (its Pulumi
setup already exists: GCS backend `gs://oa-pulumi`, KMS secrets provider).**
Two stacks, `gcs` and `cw-s3`, one program parameterized by store:

- CF: Pages project (+ production branch, env vars from a small config
  block, secrets as Pulumi secrets), custom domain, DNS CNAME, Access app +
  policy (+ the pages.dev hostnames), D1 database (the gcs stack only).
- GCP: Cloud Scheduler job with the **Batch body as code** (the cron-body
  drift class disappears — the body is rendered from the same source as
  `batch-submit.sh`), the job SA + roles, Secret Manager secrets.
- *Not* IaC'd: deployments themselves (`site/deploy` stays; Pulumi owns the
  container, wrangler fills it) and D1 migrations (schema stays with the app).

Pulumi has first-class providers for both (`@pulumi/cloudflare`:
`PagesProject`, `PagesDomain`, `DnsRecord`, `ZeroTrustAccessApplication`/
`Policy`, `D1Database`; `@pulumi/gcp`: `cloudscheduler.Job`,
`secretmanager`). Import the existing resources rather than recreate.

## Should upstream (disk-tree) offer primitives?

Probably a **component**, not primitives: `new CfnDashboard(name, { store,
domain, accessPolicy, dataBucket, schedule })` that stands up "the Vite+CFN
reference deploy" (Pages project + domain + Access + data-proxy secrets) — the
same shape marin's two stacks instantiate. It belongs with the `cfn`
reference branch proposed in disk-tree's `specs/two-reference-deploys.md`,
and it's exactly what the R2/AWS-S3 deployments would reuse. Marin's stacks
would then be `CfnDashboard` × per-branch config + the GCP job pieces.

## Order

1. Bootstrap `~/c/oa/ops/cf/` (or `gcp/marin-usage/`) Pulumi project; **import**
   today's live resources for `gcs` + `cw-s3` so the first `pulumi up` is a
   no-op diff. That alone gives drift detection.
2. Scheduler jobs → rendered bodies (retire hand-edited decoded JSON).
3. Extract the CF half into the upstream component when the `cfn` branch exists.

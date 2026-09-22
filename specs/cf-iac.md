# Cloudflare resources as code (Pulumi) — inventory + recommendation

Written 2026-08-28 after splitting cw-s3.oa.dev onto its own Pages project by
hand (~12 API/CLI calls across two tokens). That exercise *is* the argument.

**Status (2026-09-21): import-ready draft built, not applied.** The Pulumi
program lives at `~/c/oa/ops/cf/gcs-usage/` (untracked in `ops`; project
`gcs-usage-cf`, sibling to the GCP `gcs-usage` batch stack). It's the CF twin of
`specs/batch-iac.md` — the same posture: authored, dep-resolved, and validated
under Pulumi mocks for both stacks, but `pulumi stack init`/`import`/`up` are
prod-gated (the user runs the §Runbook below). Nothing has touched live CF.

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

## Should upstream (disk-tree) offer primitives? — the `CfnDashboard` component

A **component**, not primitives: one `ComponentResource` that stands up the
Cloudflare surface of a "Vite + CFN dashboard" so a deployment is one call plus
its config. The `__main__.py` here already *is* the extraction source — the body
of the `STORES` loop (Pages shell + domain + CNAME + Access app/policy + D1 +
optional KV) is exactly the component; adopting it means lifting that block into
`CfnDashboard.__init__` and having each stack pass a `Store`.

**Refinement of the 2026-08-28 sketch:** keep the component **CF-only**. The
original `{ dataBucket, schedule }` inputs conflated the data-plane job into it;
that job is per-cloud (GCP Batch here, would be a Lambda/Cloud Run elsewhere) and
already lives in its own stack (`specs/batch-iac.md`). A deployment is
`CfnDashboard` (this) **+** a data-plane stack — not one mega-component that
spans clouds. The CF half is the part that's genuinely identical across R2 /
AWS-S3 / GCS backends, so that's what's worth sharing.

```python
class CfnDashboardArgs:
    account_id: Input[str]
    zone_id: Input[str]            # the apex zone holding the custom-domain CNAME
    store: Store                   # pages_project, production_branch, domain,
                                   # d1_{name,id}, access: AccessApp, kv?

class CfnDashboard(pulumi.ComponentResource):
    """Pages project shell + custom domain + zone CNAME + Zero Trust Access
    app/policy + D1 + optional KV. Owns the *container*; wrangler still fills
    deployment_configs (vars/bindings/secrets) — see the deploy/config boundary.
    Exports: pages_project, custom_domain, d1_database, access_app_id/aud, kv?"""
    def __init__(self, name, args: CfnDashboardArgs, opts=None): ...
```

Marin's two stacks then collapse to (per stack):

```python
dash = CfnDashboard(stack, CfnDashboardArgs(account_id=…, zone_id=…, store=STORES[stack]))
```

**Home / language.** `ops` is Python; disk-tree is TS. A Pulumi component must be
importable by its consumer, so the pragmatic first home is a **shared Python
module in `ops`** (`ops/cf/_components/cfn_dashboard.py`), imported by both
stacks — no packaging, no cross-language bridge. "Belongs in disk-tree's `cfn`
branch" (`specs/two-reference-deploys.md`) stays the *eventual* home, but that
only pays off once a consumer **outside `ops`** (the R2/S3 reference deploy)
needs it — at which point extract it to a published package (a Python
`disk-tree-iac`, or a multi-language component provider if a TS consumer appears
too). Don't pay the packaging/cross-language cost for two in-repo consumers.

Preconditions before building it: (1) the `gcs`+`cw-s3` stacks are imported and
sitting at empty preview (so the refactor is provably a no-op — diff before/after
must be empty), and (2) a third consumer is on the horizon, else this is a
two-call DRY with negative ROI. Until then the `STORES`-loop form is fine.

## Order

1. Bootstrap `~/c/oa/ops/cf/` (or `gcp/marin-usage/`) Pulumi project; **import**
   today's live resources for `gcs` + `cw-s3` so the first `pulumi up` is a
   no-op diff. That alone gives drift detection. — **program built (2026-09-21);
   import is the §Runbook.**
2. Scheduler jobs → rendered bodies (retire hand-edited decoded JSON). —
   **done separately as the GCP stack (`specs/batch-iac.md`, `ops/gcp/gcs-usage/`).**
3. Extract the CF half into the `CfnDashboard` component (sketch above) — gated
   on both stacks at empty preview **and** a third (R2/S3) consumer existing.

## What the program models (and deliberately doesn't)

One program, one stack per deployment branch; the store descriptor is keyed by
`pulumi.get_stack()` (`gcs` | `cw-s3`) in `__main__.py`. Managed:

| program name | CF resource | notes |
|---|---|---|
| `pages` | `PagesProject` | container only, `ignore_changes=["deployment_configs"]` |
| `domain` | `PagesDomain` | `gcs.oa.dev` / `cw-s3.oa.dev` |
| `cname` | `DnsRecord` | the `oa.dev` CNAME → `<project>.pages.dev`, proxied |
| `d1` | `D1Database` | container; migrations stay with the app |
| `cache-kv` | `WorkersKvNamespace` | **gcs only** |
| `access-app` + `access-policy` | `ZeroTrustAccessApplication` + `ZeroTrustAccessPolicy` | gcs: `/auth/sso`, include Everyone (D1 allowlist gates); cw-s3: whole host, include OA + coreweave.com |

**The deploy/config boundary is the load-bearing decision.** `wrangler pages
deploy` (via `site/deploy`) fills the Pages project's *deployment_configs* — env
vars, D1/KV bindings, secrets — from `site/wrangler.toml` on every deploy. Pulumi
must not fight that, so `PagesProject` carries
`ignore_changes=["deployment_configs"]`: **Pulumi owns the container, wrangler
fills it.** Secret *values* (`SESSION_SECRET`, `GCS_HMAC_*`, `GCP_SA_KEY`) are
never in code or state. D1 *migrations* stay `wrangler d1 migrations apply`.

## Runbook

Prod-gated — the user runs these; expect to iterate step 4 before the diff
closes. The CF provider needs a `CLOUDFLARE_API_TOKEN` scoped for Pages + Access
+ DNS + D1 + Workers KV.

```bash
cd ~/c/oa/ops/cf/gcs-usage && uv sync            # already resolved: pulumi-cloudflare 6.21.0
# one stack per deployment; repeat init/config/import/refresh for cw-s3
pulumi stack init gcs \
  --secrets-provider=gcpkms://projects/oa-internal-450019/locations/global/keyRings/ops/cryptoKeys/pulumi/
pulumi config set cloudflare:accountId 74981a43…      # full account id
pulumi config set gcs-usage-cf:oaDevZoneId <oa.dev zone id>
```

Then `pulumi import <type> <name> <id>` each resource before any `up`. Known ids
are public (in `site/wrangler.toml`); the rest come from the CF API/console at
import time:

| name | import id (`pulumi import` 3rd arg) | id source |
|---|---|---|
| `pages` | `<account_id>/<project>` (`oa-gcs-usage` / `oa-cw-s3-usage`) | known |
| `domain` | `<account_id>/<project>/<domain>` | known |
| `d1` | `<account_id>/<database_id>` | wrangler.toml (`e52398b7…` / `7f1e1326…`) |
| `cache-kv` | `<account_id>/<namespace_id>` (`757702a0…`) | wrangler.toml (gcs) |
| `access-app` | `<account_id>/<app_id>` (gcs `e18304ed…`, cw `4c463052…`) | full UUID from API |
| `access-policy` | `<account_id>/<policy_id>` | from the app's policy (API) |
| `cname` | `<zone_id>/<record_id>` | oa.dev zone (API) |

4. `pulumi refresh` → iterate `pulumi preview` to empty. The Access app + policy
   are the likeliest to need field tweaks first (v6's `destinations` / `includes`
   shape vs. what's live); everything else should import clean. Only an empty
   preview makes the stack authoritative — do **not** `up` before that.

When applied, move this to `specs/done/`.

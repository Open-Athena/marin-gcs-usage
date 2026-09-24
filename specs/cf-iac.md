# Cloudflare resources as code (Pulumi) — inventory + recommendation

Written 2026-08-28 after splitting cw-s3.oa.dev onto its own Pages project by
hand (~12 API/CLI calls across two tokens). That exercise *is* the argument.

**Status (2026-09-24): the `gcs` stack is live and at empty preview; `cw-s3`
is import-ready but still blocked on its Zero Trust app.** The Pulumi program
lives at `cf/` in this repo (project `gcs-usage-cf`, sibling to the GCP
`gcs-usage` batch stack): a marin-agnostic `CfnDashboard` component
(`cfn_dashboard.py`) + thin per-stack wiring (`__main__.py`). It's the CF twin
of `specs/batch-iac.md`. gcs.oa.dev moved off Zero Trust on 2026-09-24
(`specs/done/oidc-cutover.md`), which removed the one resource that wouldn't import
under `pulumi-cloudflare` v6; the gcs stack then adopted Pages project, custom
domain, CNAME, D1 and the cache KV in place (`pulumi up -s gcs` with the
`CF_IAC_TOKEN` user token: 2 created, 5 imported, 0 errors; `importIds`
dropped afterwards; `pulumi preview -s gcs` = 7 unchanged). cw-s3 keeps its
real Access app, so its stack either waits for the same cutover or excludes
the Access pair (see the cw-s3 follow-up in `done/oidc-cutover.md`).

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

## `CfnDashboard` — the batteries-included component (built 2026-09-22)

The reusable batteries belong in the **disk-tree ecosystem**, and the goal is
that any DT user can stand up "a Vite + CFN dashboard" from them — so the
component is written **extraction-ready from the start**, not extracted-later.
(Earlier this section argued "keep it in `ops` until a third consumer exists";
that optimizes internal DRY, but if the *point* is a public batteries-included
path, the third consumer is "any DT user" — which is the point. Home is DT.)

Structure, as built in `ops/cf/gcs-usage/`:

- **`cfn_dashboard.py`** — the `CfnDashboard` `ComponentResource` + its `Store` /
  `AccessApp` descriptors. **Marin-agnostic**: no account ids, no zone ids, no
  store literals. Stands up Pages shell + custom domain + apex CNAME + Zero Trust
  Access app/policy + D1 + optional KV. Owns the *container*; wrangler fills
  `deployment_configs` (`ignore_changes=["deployment_configs"]`). Written to move
  **verbatim** to disk-tree's `cfn` reference-deploy branch
  (`specs/two-reference-deploys.md`) — its only import is `pulumi_cloudflare`.
- **`__main__.py`** — the thin instance wiring: the two `Store`s + account/zone
  config, `CfnDashboard(store=STORES[get_stack()])`. This is the only
  marin/private piece; when the component moves to DT it stays here (or wherever
  marin's stacks live) and just imports the DT package.

**Component is CF-only** (refines the 2026-08-28 `{ dataBucket, schedule }`
sketch): the data-plane job is per-cloud (GCP Batch here) and lives in its own
stack. A deployment is `CfnDashboard` **+** a data-plane stack, never one
cross-cloud mega-component. The CF surface is the part identical across GCS / R2
/ AWS-S3 backends — the part worth sharing.

**Language.** `ops` is Python; disk-tree is TS. The component is Python, so the
DT home is a Python package (a `disk-tree-iac` on PyPI, or a `pulumi/` subdir on
the `cfn` branch), not the TS app tree — a multi-language component provider is
only worth it if a TS Pulumi consumer ever appears. Marin's wiring stays Python
and imports it.

## Least-privilege CI — preview-only, and the one GCP broad-grant

**CI runs `pulumi preview` only** (read-only drift alarm); a human runs `up`
locally (as deploys are gated today). That keeps the CI credential to *read*
scopes and sidesteps most of the "scarily broad CI token" problem:

- **Cloudflare: a non-issue.** CF API tokens scope per-product × per-account/zone
  — a CI token is exactly Pages/Access/DNS/D1/KV **read** on this account. Add
  scopes reactively; there's no "admin everything" floor.
- **GCP: one genuine broad grant, now isolated.** Creating the resources needs
  per-product admin (all scopable). The trap is granting the job SA its
  *project-level* roles: that needs `resourcemanager.projectIamAdmin`, and GCP
  has **no way to scope "may grant only role X"** — so it's effectively
  project-IAM-admin. Fix (implemented in `ops/gcp/gcs-usage/__main__.py`): the
  `IAMMember` project-role bindings are guarded by a `manage_project_iam` config
  flag, **default off**. The SA's four roles change ~never, so grant them by hand
  once; CI (and a normal `up`) then manage scheduler + secrets + *secret-level*
  IAM (which **is** per-secret scopable) + the SA itself, needing no
  projectIamAdmin. Flip the flag on only for the rare human-run `up` that holds
  the broad credential.

Preconditions before the component leaves `ops` for DT: the `gcs`+`cw-s3` stacks
are imported and at empty preview (so any later move is a provable no-op).

## Order

1. Bootstrap `~/c/oa/ops/cf/` (or `gcp/marin-usage/`) Pulumi project; **import**
   today's live resources for `gcs` + `cw-s3` so the first `pulumi up` is a
   no-op diff. That alone gives drift detection. — **program built (2026-09-21);
   import is the §Runbook.**
2. Scheduler jobs → rendered bodies (retire hand-edited decoded JSON). —
   **done separately as the GCP stack (`specs/batch-iac.md`, `ops/gcp/gcs-usage/`).**
3. Extract the CF half into the `CfnDashboard` component — **built 2026-09-22**
   (see above); lives in `ops` written extraction-ready, moves to DT's `cfn`
   branch once both stacks sit at empty preview.

## What the component models (and deliberately doesn't)

`CfnDashboard` (in `cfn_dashboard.py`), instantiated once per stack from
`__main__.py`. Each resource is a component child named `<stack>-<key>` (e.g.
`gcs-pages`); the `key` is what the import-ids map is keyed on:

| key | CF resource | notes |
|---|---|---|
| `pages` | `PagesProject` | container only, `ignore_changes=["deployment_configs"]` |
| `domain` | `PagesDomain` | `gcs.oa.dev` / `cw-s3.oa.dev` |
| `cname` | `DnsRecord` | the `oa.dev` CNAME → `<project>.pages.dev`, proxied |
| `d1` | `D1Database` | container; migrations stay with the app |
| `kv` | `WorkersKvNamespace` | **gcs only** |
| `access-policy` + `access-app` | `ZeroTrustAccessPolicy` + `ZeroTrustAccessApplication` | gcs: `/auth/sso`, include Everyone (D1 allowlist gates); cw-s3: whole host, include OA + coreweave.com |

**The deploy/config boundary is the load-bearing decision.** `wrangler pages
deploy` (via `site/deploy`) fills the Pages project's *deployment_configs* — env
vars, D1/KV bindings, secrets — from `site/wrangler.toml` on every deploy. Pulumi
must not fight that, so `PagesProject` carries
`ignore_changes=["deployment_configs"]`: **Pulumi owns the container, wrangler
fills it.** Secret *values* (`SESSION_SECRET`, `GCS_HMAC_*`, `GCP_SA_KEY`) are
never in code or state. D1 *migrations* stay `wrangler d1 migrations apply`.

## Runbook

Prod-gated. The resources are already declared (as component children), so this
**adopts them in place** via the component's `importIds` config — not CLI
`pulumi import` (which is for code-gen of undeclared resources). Needs a
`CLOUDFLARE_API_TOKEN` scoped for Pages + Access + DNS + D1 + KV, and (for the
KMS secrets provider + `gs://oa-pulumi` backend) `pulumi login gs://oa-pulumi`
under gcloud creds. Do the `gcs` stack, then repeat for `cw-s3`.

```bash
cd ~/c/oa/ops/cf/gcs-usage && uv sync    # already resolved: pulumi-cloudflare 6.21.0
pulumi login gs://oa-pulumi
pulumi stack init gcs \
  --secrets-provider=gcpkms://projects/oa-internal-450019/locations/global/keyRings/ops/cryptoKeys/pulumi/
pulumi config set cloudflare:accountId 74981a43…       # full account id
pulumi config set gcs-usage-cf:oaDevZoneId <oa.dev zone id>
```

Set an import id per resource `key` (the component adopts each one instead of
creating it). Known ids are public (`site/wrangler.toml`); the rest come from the
CF API at import time:

| key | import id (`--path importIds.<key>`) | id source |
|---|---|---|
| `pages` | `<account_id>/<project>` (`oa-gcs-usage` / `oa-cw-s3-usage`) | known |
| `domain` | `<account_id>/<project>/<domain>` | known |
| `d1` | `<account_id>/<database_id>` | wrangler.toml (`e52398b7…` / `7f1e1326…`) |
| `kv` | `<account_id>/<namespace_id>` (`757702a0…`) | wrangler.toml (gcs only) |
| `access-app` | `<account_id>/<app_id>` (gcs `e18304ed…`, cw `4c463052…`) | full UUID from API |
| `access-policy` | `<account_id>/<policy_id>` | from the app's policy (API) |
| `cname` | `<zone_id>/<record_id>` | oa.dev zone (API) |

```bash
pulumi config set --path 'importIds.pages'  "$ACCT/oa-gcs-usage"
pulumi config set --path 'importIds.d1'     "$ACCT/e52398b7-5538-4bc4-83db-3355a1b5ef9a"
pulumi config set --path 'importIds.kv'     "$ACCT/757702a080a549418f16a5ae096529c4"
# …domain, cname, access-app, access-policy similarly (fetch the API-only ids first)
pulumi preview          # shows the adoptions + any input diffs to reconcile
pulumi up               # human-run: adopts each resource into state
pulumi config rm importIds     # once adopted, drop the ids; state is authoritative
pulumi preview          # must now be EMPTY
```

Expect the Access app + policy to need field tweaks first (v6's
`destinations` / `includes` shape vs. what's live); everything else should adopt
clean. Only an empty post-adoption preview makes the stack authoritative.

**CI**, once adopted: `pulumi preview` on PRs as a drift alarm (read-scoped
token); a human runs `up`. When both stacks are applied, move this to
`specs/done/`.

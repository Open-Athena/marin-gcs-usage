"""gcs's Cloudflare stack, thin wiring over the CfnDashboard component.

One `cf/` per deployment branch, matching the branching model: `cfn_dashboard.py`
is the shared, marin-agnostic component (byte-identical across branches, like
`packages/`; extraction-ready for disk-tree's `cfn` branch) and this file is the
branch's own instance wiring — its one `Store` + the account/zone config. The
cw-s3 deployment's twin lives on the `cw-s3` branch. Both stacks share the
backend project `gcs-usage-cf` (`gs://oa-pulumi`, keyed `<project>/<stack>`), so
each branch only ever selects its own stack.

Live since 2026-09-24 (5 adopted + 2 created, empty preview since); see
../specs/cf-iac.md for the inventory and runbook.

CI RUNS `pulumi preview` ONLY (read-only drift alarm); a human runs `up`. So the
CI token needs only Cloudflare *read* scopes (Pages/DNS/D1/KV). Nothing here
needs any broad grant — the CF token model is per-product, per-account/zone.
"""

import pulumi

from cfn_dashboard import BranchAlias, CfnDashboard, Store

# ---------------------------------------------------------------------------
# Config (shared, account-global)
# ---------------------------------------------------------------------------
cfg = pulumi.Config()
# Both in this project's own namespace (`gcs-usage-cf:`), NOT `cloudflare:` —
# that namespace is the provider's, which only accepts its own keys (apiToken,
# etc.). The provider reads the token from CLOUDFLARE_API_TOKEN in the env.
account_id = cfg.require("accountId")            # 74981a43…
oa_dev_zone_id = cfg.require("oaDevZoneId")      # oa.dev zone id
# First-`up` adoption of the hand-built resources: a `{key: cf-import-id}` map
# (keys: pages/domain/cname/d1/kv). Set per key during import
# (`pulumi config set --path 'importIds.pages' <id>`), `up`, then clear.
# Absent → normal create. See specs/cf-iac.md §Runbook. (Cleared for gcs after
# the 2026-09-24 adoption.)
import_ids = cfg.get_object("importIds") or None

# ---------------------------------------------------------------------------
# Instance wiring — this branch's one deployment
# ---------------------------------------------------------------------------
STACK = "gcs"
# gcs.oa.dev — public shell + app-gated data; no Zero Trust at all since the
# 2026-09-24 cutover (own Google OIDC client + emailed codes, D1 allowlist —
# specs/done/oidc-cutover.md). The Google OAuth client is console-managed (no API
# for Web-app clients); its secrets are `wrangler pages secret`s.
STORE = Store(
    pages_project="oa-gcs-usage",
    production_branch="main",   # CF Pages production branch (wrangler deploys `--branch main`)
    domain="gcs.oa.dev",
    d1_name="oa-gcs-usage-auth",
    # The dev stack (`site/deploy --dev` → Pages branch `dev`, `[env.preview]`
    # in site/wrangler.toml) at its own hostname; dev.oa-gcs-usage.pages.dev
    # keeps resolving too. Both hosts are registered on the Google client.
    branch_aliases=(BranchAlias(domain="dev.gcs.oa.dev", branch="dev"),),
    kv_name="oa-gcs-usage-cache",
)

stack = pulumi.get_stack()
if stack != STACK:
    raise pulumi.RunError(
        f"this branch's cf/ wires the {STACK!r} stack only; selected {stack!r} "
        "(the cw-s3 deployment's stack lives in the cw-s3 branch's cf/)"
    )

dash = CfnDashboard(
    stack,
    account_id=account_id,
    zone_id=oa_dev_zone_id,
    store=STORE,
    import_ids=import_ids,
)

# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------
pulumi.export("pages_project", dash.pages.name)
pulumi.export("custom_domain", dash.domain.name)
for branch, dom in dash.branch_domains.items():
    pulumi.export(f"{branch}_domain", dom.name)
pulumi.export("d1_database", dash.d1.name)
if dash.kv is not None:
    pulumi.export("cache_kv", dash.kv.title)
    pulumi.export("cache_kv_id", dash.kv.id)   # → wrangler.toml `[[kv_namespaces]] id`
if dash.r2 is not None and dash.r2_token is not None:
    # The site's `STORE_*` secrets + the job's `R2_*` env come from these.
    pulumi.export("r2_bucket", dash.r2.name)
    pulumi.export("r2_s3_access_key_id", pulumi.Output.secret(dash.r2_token.id))
    pulumi.export("r2_s3_secret_access_key", pulumi.Output.secret(
        dash.r2_token.value.apply(lambda v: __import__("hashlib").sha256(v.encode()).hexdigest())
    ))

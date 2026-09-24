"""DRAFT — marin's Cloudflare stacks, thin wiring over the CfnDashboard component.

STATUS: DRAFT. Not yet a live stack. Do NOT `pulumi up` before importing every
existing resource and confirming `pulumi preview` is empty. See
../specs/cf-iac.md for the inventory, import command
sequence, and adoption plan.

All the reusable logic is in `cfn_dashboard.py` (marin-agnostic, extraction-ready
for disk-tree's `cfn` branch). This file is just the instance wiring: the two
per-deployment `Store`s and the account/zone config, selected by stack name.
Adding a third deployment is one dict entry + one `pulumi stack init`.

CI RUNS `pulumi preview` ONLY (read-only drift alarm); a human runs `up`. So the
CI token needs only Cloudflare *read* scopes (Pages/Access/DNS/D1/KV). Nothing
here needs project-IAM or any broad grant — the CF token model is per-product,
per-account/zone (unlike GCP; see the GCP job stack for that story).
"""

import pulumi

from cfn_dashboard import AccessApp, CfnDashboard, Store

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
# (keys: pages/domain/cname/d1/kv/access-policy/access-app). Set per key during
# import (`pulumi config set --path 'importIds.pages' <id>`), `up`, then clear.
# Absent → normal create. See specs/cf-iac.md §Runbook.
import_ids = cfg.get_object("importIds") or None

# ---------------------------------------------------------------------------
# Instance wiring — one Store per deployment branch, keyed by stack name
# ---------------------------------------------------------------------------
STORES: dict[str, Store] = {
    # gcs.oa.dev — public shell + Tier-2 SSO. Access gates only `/auth/sso`; the
    # policy includes Everyone because the D1 grants table is the real gate.
    "gcs": Store(
        pages_project="oa-gcs-usage",
        production_branch="main",   # CF Pages production branch (wrangler deploys `--branch main`)
        domain="gcs.oa.dev",
        d1_name="oa-gcs-usage-auth",
        kv_name="oa-gcs-usage-cache",
        access=AccessApp(
            name="GCS usage (Marin storage attribution)",
            uris=(
                "gcs.oa.dev/auth/sso",
                "oa-gcs-usage.pages.dev/auth/sso",
                "*.oa-gcs-usage.pages.dev/auth/sso",
            ),
            include_everyone=True,
            session_duration="168h",
            policy_name="Access as IdP only (allowlist enforced in-app via D1 allowed_emails)",
        ),
    ),
    # cw-s3.oa.dev — whole host behind Access; OA staff + CoreWeave viewers.
    "cw-s3": Store(
        pages_project="oa-cw-s3-usage",
        production_branch="main",   # CF Pages production branch (wrangler deploys `--branch main`)
        domain="cw-s3.oa.dev",
        d1_name="oa-cw-s3-usage-db",
        # Global cache tier (`CACHE_KV`, the edge cache's second tier) — cw's
        # wrangler.toml stanza is commented out until this exists; paste the
        # `cache_kv` output there. NOT created yet (user-gated `up`).
        kv_name="oa-cw-s3-usage-cache",
        # R2 serving bucket (specs/r2-serving-migration.md): the job's
        # `dt-cloud publish-r2` stage lands the served artifacts here and the
        # site reads them via `STORE_*`. NOT created yet (user-gated `up`).
        r2_bucket="oa-cw-s3-usage-index",
        # Machine identity for the Batch job's warm-cache stage (`job/cw-run.sh`
        # 4b): its client id/secret go to Secret Manager as
        # `cw-s3-access-client-id` / `cw-s3-access-client-secret`. NOT created
        # yet (user-gated `up`).
        service_token="oa-cw-s3-usage job (warm-cache)",
        access=AccessApp(
            name="CoreWeave usage (cw-s3.oa.dev)",
            uris=(
                "*.oa-cw-s3-usage.pages.dev",
                "cw-s3.oa.dev",
                "oa-cw-s3-usage.pages.dev",
            ),
            email_domains=("openathena.ai", "coreweave.com"),
            session_duration="168h",
            app_launcher_visible=True,
            policy_name="OA + CoreWeave domains (CW data is not for Stanford/external)",
        ),
    ),
}

stack = pulumi.get_stack()
if stack not in STORES:
    raise pulumi.RunError(
        f"stack {stack!r} has no store descriptor; expected one of {sorted(STORES)}"
    )

dash = CfnDashboard(
    stack,
    account_id=account_id,
    zone_id=oa_dev_zone_id,
    store=STORES[stack],
    import_ids=import_ids,
)

# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------
pulumi.export("pages_project", dash.pages.name)
pulumi.export("custom_domain", dash.domain.name)
pulumi.export("d1_database", dash.d1.name)
pulumi.export("access_app_id", dash.access_app.id)
pulumi.export("access_app_aud", dash.access_app.aud)
if dash.kv is not None:
    pulumi.export("cache_kv", dash.kv.title)
    pulumi.export("cache_kv_id", dash.kv.id)   # → wrangler.toml `[[kv_namespaces]] id`
if dash.service_token is not None:
    # The job's Access credentials (`CF_ACCESS_CLIENT_ID` / `_SECRET`).
    pulumi.export("service_token_client_id", pulumi.Output.secret(dash.service_token.client_id))
    pulumi.export("service_token_client_secret", pulumi.Output.secret(dash.service_token.client_secret))
if dash.r2 is not None and dash.r2_token is not None:
    # The site's `STORE_*` secrets + the job's `R2_*` env come from these.
    pulumi.export("r2_bucket", dash.r2.name)
    pulumi.export("r2_s3_access_key_id", pulumi.Output.secret(dash.r2_token.id))
    pulumi.export("r2_s3_secret_access_key", pulumi.Output.secret(
        dash.r2_token.value.apply(lambda v: __import__("hashlib").sha256(v.encode()).hexdigest())
    ))

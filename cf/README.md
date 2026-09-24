# gcs-usage-cf — Cloudflare resources as code

**Status (2026-09-23): initialized, not yet applied.** Both stacks (`gcs`,
`cw-s3`) exist in the `gs://oa-pulumi` backend with their config + adoption
`importIds` set, and `pulumi preview` shows the expected adopt-in-place imports
(gcs 7, cw-s3 6) plus the creates; no `up` has been run, so the live resources
are still hand-managed (Pages API / Zero Trust console / zone DNS). This
directory captures them as code so the first `up` adopts them and later `up`s
create the rest (cw-s3: KV cache tier, R2 bucket + token, warm-cache service
token).

Full inventory, gap analysis, import command sequence, and adoption plan live in
the spec: `../specs/cf-iac.md`.

**Structure.** `cfn_dashboard.py` is the reusable, marin-agnostic `CfnDashboard`
component (extraction-ready for disk-tree's `cfn` reference-deploy branch — no
account ids, no store literals). `__main__.py` is just the instance wiring: the
two per-deployment `Store`s + account/zone config, selected by stack name.
Adding a third deployment is one dict entry + one `pulumi stack init`.

**CI is `pulumi preview` only** (read-only drift alarm); a human runs `up`. The
CF token model is per-product, per-account/zone, so the CI (read) token scopes
to exactly Pages/Access/DNS/D1/KV read on this account — no broad grant. (The
GCP job stack carries the one broad-IAM caveat; see `../../ops/gcp/gcs-usage/`.)

## What this manages (once adopted)

- `cloudflare.PagesProject` — the project **shell** only (name + production
  branch + build config). Managed with `ignore_changes=["deployment_configs"]`:
  `wrangler pages deploy` (via `site/deploy`) fills env vars, D1/KV bindings, and
  secrets from `site/wrangler.toml` on every deploy. **Pulumi owns the container,
  wrangler fills it.**
- `cloudflare.PagesDomain` — the custom domain (`gcs.oa.dev` / `cw-s3.oa.dev`).
- `cloudflare.DnsRecord` — the `oa.dev` CNAME the custom domain resolves through.
- `cloudflare.ZeroTrustAccessApplication` + `ZeroTrustAccessPolicy` — the Access
  gate, only when a store sets `access` (cw-s3: whole host, policy = OA +
  coreweave.com email domains). gcs has **no** Access app since the 2026-09-24
  cutover to its own Google OIDC client + emailed codes (`../specs/done/oidc-cutover.md`);
  that also removed the one resource pulumi-cloudflare 6.21 couldn't import
  (`destinations` + auto-mirrored `self_hosted_domains`).
- `cloudflare.D1Database` — the database resource (migrations stay with the app).
- `cloudflare.WorkersKvNamespace` — each stack's `CACHE_KV` global cache tier (`gcs` today; `cw-s3` declared, not yet created — its `wrangler.toml` stanza stays commented until the `cache_kv_id` output exists).
- `cloudflare.ZeroTrustAccessServiceToken` + a second, `non_identity` `ZeroTrustAccessPolicy` on the app (`cw-s3` only) — the machine identity the Batch job's warm-cache stage uses to call the site through Access; client id/secret are the secret outputs `service_token_client_id` / `service_token_client_secret`, destined for Secret Manager, never git.
- `cloudflare.R2Bucket` + `cloudflare.ApiToken` (`cw-s3` only) — the R2 serving bucket the job publishes the served artifacts to (`dt-cloud publish-r2`) and the token whose S3-API credentials both the publish step and the site's `STORE_*` seam use: access key id = the token id, secret = sha256(token value) — exported as the secret outputs `r2_s3_access_key_id` / `r2_s3_secret_access_key`. See `../specs/r2-serving-migration.md`. The token's permission groups resolve by name at program time; a read-scoped CI token can't list them, so `pulumi preview` in CI needs `Store.r2_token_permission_group_ids` set explicitly (the two R2 bucket-item read/write group ids).

## Backfill: publish step vs Sippy vs Super Slurper

- **Steady state:** the job's final stage, `dt-cloud publish-r2 <scan>` — idempotent (size + md5), so it is also a safe backfill over old scans (`for s in …; do dt-cloud publish-r2 $s; done`).
- **One-time bulk backfill: Super Slurper (recommended).** Console-driven, copies the existing prefixes GCS → R2 in bulk with no compute of ours; the publish step then keeps it current. Run it once before the serve flip.
- **Sippy: not recommended here.** It migrates *on read* (a miss in R2 fetches from GCS and stores it), which (a) needs a GCS service-account key stored in Cloudflare, (b) makes the first read of every object a cross-provider fetch — the exact latency the migration removes — and (c) buys nothing once the publish step runs on every scan. `cloudflare.R2BucketSippy` exists in the provider if that ever changes.

## Do NOT

- `pulumi up` before importing every listed resource and getting an empty
  `pulumi preview`.
- Manage secret **values** here (SESSION_SECRET, GCS_HMAC_*, GCP_SA_KEY) — they
  stay `wrangler pages secret put` (public repo; no secret material in state).
- Assert `deployment_configs` (vars / bindings) — that's wrangler's, from
  `wrangler.toml`.

## Adoption (see the spec for exact commands)

1. `pulumi stack init gcs --secrets-provider=gcpkms://…/cryptoKeys/pulumi/`
   (and again for `cw-s3`); set `cloudflare:accountId` + `gcs-usage-cf:oaDevZoneId`.
   The CF provider needs a `CLOUDFLARE_API_TOKEN` scoped for Pages + Access + DNS
   + D1 + Workers KV.
2. `pulumi import` the Pages project, custom domain, CNAME, D1, (gcs) KV, then the
   Access app + policy.
3. `pulumi refresh` → iterate until `pulumi preview` is empty. The Access
   app/policy are the likeliest to need field tweaks (v6 `destinations` /
   `includes` shape) before the diff closes. Only then is the stack authoritative.

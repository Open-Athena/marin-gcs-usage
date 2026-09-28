# Move gcs.oa.dev off Cloudflare Zero Trust → `@open-athena/auth` OIDC (our own Google client)

**Status: P0 + P1 done; P2 backend done (dormant) — needs Resend setup + the cutover frontend; then deploy/verify/cutover.** Prereq that unblocks [`cf-iac.md`](../cf-iac.md) (the ZT Access app/policy is the one resource that won't import cleanly under `pulumi-cloudflare` v6).

**Progress:**
- **P0 ✓** — Google "Web application" OAuth client `gcs.oa.dev (marin-gcs-usage)` created in `oa-internal-450019` (JS origin `https://gcs.oa.dev`, redirect `https://gcs.oa.dev/auth/google/callback`); `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` set as **production** Pages secrets on `oa-gcs-usage`.
- **P1 ✓** — OIDC site code, dormant + typechecking (`tsc -p functions` clean): `functions/auth/google.ts` (`oidcStart`), `functions/auth/google/callback.ts` (`oidcCallback`), shared `functions/_lib/oidc.ts` (`redirectUri` derived from request origin), `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` on `Env`. Routes 503 without secrets; `signInUrl()` still targets `/auth/sso`, so nothing changes for users pre-cutover.
- **Allowlist audit (50 rows):** 47 can Google-auth (stanford.edu / gmail.com / berkeley.edu / cs.stanford.edu, + nelsonliu.me confirmed Google Workspace by MX). **3 cannot** (need the email-code fallback): `michael_ryan_2000@yahoo.com`, `tonyh.lee@yahoo.com` (Yahoo), `me@joelniklaus.ch` (stackmail MX).
- **Decision: wire the email-code (`emailCodeAuth` / Resend) fallback (option B)** — don't shut out the non-Google tail. This must be live + verified **before** ZT is deleted.
- **Resend is shared OA infra** (discovered via `/read` of `~/c/oa/auth`, where it was set up for `oa-auth-demo`): `RESEND_API_KEY` already in `$oa/.envrc` (send-only), decided `from = noreply@oa.dev`, `_dmarc.oa.dev` live. **`RESEND_API_KEY` + `MAIL_FROM="Open Athena <noreply@oa.dev>"` now set as `oa-gcs-usage` production secrets** (reusing the shared key). **Blocker: `oa.dev` is not yet verified in Resend** — DKIM (`resend._domainkey.oa.dev`) + SPF/MX (`send.oa.dev`) records aren't published (dig empty). That verification is a one-time shared step being handled in the **auth session** (it has the record values + the CF admin token); mgu does not touch the oa.dev zone for this. Delivery works once oa.dev verifies. Header-From stays `noreply@oa.dev` (DMARC passes via DKIM alignment `d=oa.dev`).

## Why

Two independent pressures, one fix:

1. **ZT is the chronically brittle piece of the CF Pulumi stack.** The `ZeroTrustAccessApplication`/`Policy` resources needed hand-found import-id discriminators (`accounts/<acct>/<id>` for the app, bare `<acct>/<id>` for the policy), a cluster of spurious diffs (`domain`/`self_hosted_domains` legacy mirrors, `app_launcher_visible`, `http_only_cookie`), and now a hard **import blocker**: the live app exposes *both* `destinations` and `self_hosted_domains` (CF auto-mirrors them, paths and all), which v6 rejects as mutually exclusive on import-read. `ignore_changes` can't fix it (it's input validation, not diffing). `pulumi-cloudflare` 6.21.0 is already the latest *stable*; only `6.22.0a*` alphas are newer, with no guarantee of a fix.
2. **ZT's 50-seat free-tier cap** bit during the CW quota incident (hit 50/50; users bounced). A 52-email allowlist against a 50-seat ceiling is structurally wrong. (This is the exact rationale in the auth repo's `specs/done/google-oidc-idp.md`.)

Removing ZT from gcs deletes the brittle Pulumi resource **and** the seat cap in one move — and `@open-athena/auth`'s `oidc` adapter was *built for this* (that spec names "the mgu-requested consumer shape"; the adapter is shipped + tested but has **no live consumer yet — mgu is the first**).

## Scope: gcs first. cw-s3 is a bigger lift (deferred).

- **gcs** (`EDGE_TRUSTED` unset): the app gate already owns authorization — the D1 `allowed_emails` allowlist + `@open-athena/auth` sessions authorize *everything*. CF Access does exactly **one** thing: the `/auth/sso` hand-off, where `sso.ts` verifies the Access JWT to get a verified email, then calls `gate.signIn(email)`. Swapping that one hand-off for OIDC is **surgical and self-contained.**
- **cw-s3** (`EDGE_TRUSTED` set): the **whole host** sits behind ZT, with **no app session and no D1 allowlist** — the OA-vs-`coreweave.com` gate lives *in the ZT policy itself*. Moving it off ZT means giving it the session model + relocating that domain gate in-app. Out of scope here; see "cw-s3 follow-up" below.

## The swap surface (identity layer only — everything else unchanged)

Verified against `/Users/ryan/c/oa/auth`. **Unchanged:** the `gate` (`createGate`), `SESSION_SECRET` (same HMAC key — the OIDC adapter reuses it to sign its `state`/nonce), the `DB` binding + all migrations, `d1GrantStore`/`d1Allowlist`/etc., `scopesFor`/`allowlistPolicy`, `verifyRs256Jwt`, the session-cookie codec, and the whole mounted `/api/auth/*` surface (`whoami`, `logout`, `exchange`, `request`, admin `grants`/`allowed`/`log`). Share links never touched Access — entirely unaffected.

**Changes (all on the gcs branch, in `site/`):**

- **Add** `functions/auth/google.ts` = `oidcStart(cfg)` (→ `/auth/google`) and `functions/auth/google/callback.ts` = `oidcCallback(cfg)` (→ `/auth/google/callback`), where
  ```ts
  import { GOOGLE, oidcStart, oidcCallback } from '@open-athena/auth/oidc'
  const cfg = { gate: gateFor(env), clientId: env.GOOGLE_CLIENT_ID,
                clientSecret: env.GOOGLE_CLIENT_SECRET,
                redirectUri: 'https://gcs.oa.dev/auth/google/callback' }
  ```
  No `hd` domain hint (we serve non-OA allowlisted addresses too). Full auth-code flow, confidential client, no PKCE (CSRF/replay defense = HMAC-signed `state` + double-submit nonce cookie + `id_token` nonce).
- **Point the sign-in UI** at `/auth/google` instead of the CF Access `/auth/sso` login (check `SignInPanel` / the SPA's sign-in trigger).
- **Add secrets** `GOOGLE_CLIENT_ID` + `GOOGLE_CLIENT_SECRET` (Pages secrets — never git, never Pulumi).
- **After cutover:** remove `functions/auth/sso.ts` and the `ACCESS_TEAM_DOMAIN`/`ACCESS_AUD` env vars. Leave `verifyAccessJwt`/`edgeIdentity` in `_lib/auth.ts` — dormant on gcs (no Cf-Access header), still needed by cw-s3.

## Phases (gated steps flagged)

**P0 — Google OAuth client (DONE ✓; was GATED — Ryan ran).** Not IaC-able: Google has no API to create a Web-application OAuth client (only IAP-brand clients are Terraform-manageable, and those don't serve a generic redirect flow — auth repo `specs/done/oauth-client-iac.md`). Use the auth repo helper:
```
scripts/provision-oauth-client.mjs \
  --project oa-internal-450019 \
  --app-origin https://gcs.oa.dev \
  --redirect-uri https://gcs.oa.dev/auth/google/callback \
  --pages-project oa-gcs-usage            # add --run to orient gcloud + store secrets
```
It's dry-run by default (prints the pre-filled console deep-link + the exact `wrangler` calls); `--run` orients gcloud and pipes the pasted id/secret into `wrangler pages secret put` (values only on stdin, never logged). One client per deployment. Add a localhost redirect (`http://localhost:3254/auth/google/callback`) if we want to test the real flow locally rather than on a preview.

**P1 — OIDC site code (DONE; additive + reversible).** The two Functions + shared config; `signInUrl` left on `/auth/sso` until cutover.

**P2 — Email-code fallback (Resend) — the One-Time-PIN replacement.** Backend DONE (dormant, typechecks): `functions/auth/email/[[path]].ts` mounts the four `emailCodeAuth` handlers at `/auth/email/{start,code,verify,poll}` (deliberately off the `/api/auth/*` `authRoutes` catch-all), + `functions/_lib/emailcode.ts` (config) + `RESEND_API_KEY`/`MAIL_FROM` on `Env`. Reuses the existing gate/allowlist/session; **no new migration** — `pending_auth` (site `0029`) is already applied. Converges on the same `gate.signIn`. Still to do: the frontend `<EmailCodeForm>` (bundled into the cutover, P4) and the external Resend setup. **Dormant (503) until `RESEND_API_KEY` + `MAIL_FROM` exist.**

External dependency (unavoidable): a Resend account + a **verified sending domain** (DKIM/SPF/DMARC DNS on the sending domain) + an API key. IaC-able parts: Resend has a TF provider (`resend_domain` emits the records, `resend_api_key`), and the DNS records go on the `oa.dev` Cloudflare zone (dovetails with `cf-iac.md`). Not automatable: account creation + the async domain-verification wait; the key lands in Pages secrets (scriptable, like the Google flow).

**P3 — Verify both paths off-prod, then deploy.** Verify on the local full stack first: `PORT=3257 site/dev --local-db` (Vite + wrangler Functions, `/auth/*` proxied, local D1 seeded with the allowlist), `.dev.vars` carrying `GOOGLE_CLIENT_ID/SECRET` + `RESEND_API_KEY` + `MAIL_FROM`, and `http://localhost:<PORT>/auth/google/callback` registered on the Google client (Google permits localhost redirect URIs on Web clients; the secret can't be re-read from the console — a second "Add secret" is the way to get one for `.dev.vars`). Exercise at `/?wall`: Google sign-in (session minted, allowlist enforced, non-allowlisted → `/?denied=<email>`), email-code sign-in to a non-Google address, share links still redeem. Then deploy (user-run `site/deploy`; classifier-gated for the agent) **alongside** the still-live ZT `/auth/sso` and re-check both on gcs.oa.dev. A persistent `dev.gcs.oa.dev` (second CFP project, own client) is the longer-term shape — a third `CfnDashboard` store, and the first stack to `up` clean since it never had Access.

**P4 — Cutover.** Both paths verified on the local stack 2026-09-23 (Google on localhost:3257; emailed link over the tailnet from a phone, to an OA address). Flip DONE in code: `signInUrl()` → the app's own `/signin?next=` page (the wall as an ungated route), so nothing links to `/auth/sso` any more; the request-access form + how-to prose fold behind a disclosure that a `?denied=` bounce unfolds. Deployed 2026-09-24 and both paths re-verified on prod by the user (Google on desktop, emailed code from a phone). Second deploy removed `functions/auth/sso.ts` + the `ACCESS_*` vars (`edgeIdentity` short-circuits unless `ACCESS_AUD` is set). The ZT Access app `e18304ed` + its policy `0fc07581` were deleted by the user from the Cloudflare One dashboard on 2026-09-24 (the agent's API delete was classifier-blocked; pre-delete JSON saved at `site/tmp/zt-app-pre-delete.json`). Verified from outside: `/auth/sso` now serves the SPA shell (200, no Access 302) and `/data/*` still 401s without a session. gcs.oa.dev has no Zero Trust footprint left; the cw-s3 app was deliberately kept (it is cw-s3's only gate).

**P5 — CF-IaC unblock (the original goal). DONE 2026-09-24.** With ZT gone from gcs: `Store.access` is optional (the Access block lives in `CfnDashboard._access()`, only called when set) and the gcs store has none, so the stack declares just Pages/domain/CNAME/D1/KV. User-run `CLOUDFLARE_API_TOKEN=$CF_IAC_TOKEN pulumi up -s gcs --yes`: 2 created (stack + component), 5 imported, no errors; `importIds` then dropped from `Pulumi.gcs.yaml` and a follow-up `pulumi preview -s gcs --diff` reports 7 unchanged — the gcs stack is authoritative. Only noise: the provider's `deploymentConfigs.*.usageModel` deprecation warnings on the Pages project (read-side, under `ignore_changes` anyway). cw-s3 stays as below.

## cw-s3 follow-up (separate)

Either (a) port cw-s3 off ZT too — give it the session model + move the OA/`coreweave.com` gate in-app (a `domainPolicy` in `gate.policy`, or a D1 allowlist), a larger change; or (b) keep cw-s3's ZT app hand-managed and just exclude the Access app/policy from the cw-s3 Pulumi stack (import the other 5). **Decided 2026-09-24: (a)** — cw-s3 follows gcs off ZT (own Google client or a second redirect URI on the gcs one, Resend, `/signin`); its Access app `4c463052` stays until that lands, then gets deleted the same way and `Store.access` comes off the cw-s3 store.

## Open questions

- **Non-Google allowlist tail — RESOLVED to "wire email-codes" (P2).** Audited: 3 addresses can't Google-auth (2 Yahoo + 1 stackmail). Rather than migrate them, wire the `emailCodeAuth` fallback so the tail (and future non-Google users) keep a self-serve path.
- **Resend sending domain.** Which `from` address / domain do we send codes from (e.g. `noreply@oa.dev`)? Needs a verified domain on Resend (DKIM/SPF/DMARC DNS on `oa.dev`) — those records are Pulumi-able and dovetail with `cf-iac.md`.
- **Google consent screen audience** — Ryan set this during P0. (Was "Internal"/OA-only per `ops/specs/gcs-oa-dev-site.md`; serving Stanford/Gmail via our client needs External+published — noting it also opens the project's other clients to non-org users.)
- **Upstream ask (auth pkg) — "90% baked incl. IaC":** the runtime is already ~95% baked (handlers, `pending_auth` store, `EmailCodeForm`/`SignInPanel`, migration all shipped + tested). The gaps a "90%-baked" version would close, none of which exist today: (1) a **reference Pages-Function mount** for the four handlers (there's no `authRoutes` entry, no demo, no README recipe — mgu's `functions/auth/email/[[path]].ts` could become that reference); (2) a **Pulumi/TF module** that creates the Resend domain + key and writes the DKIM/SPF/DMARC records onto a Cloudflare zone; (3) a short **README/spec recipe**. Irreducible manual residue for any consumer: Resend account, domain-verification wait, accepting the ESP dependency. Captured for the `@open-athena/auth` repo.

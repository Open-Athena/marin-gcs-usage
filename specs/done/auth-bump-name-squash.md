# Bump `@open-athena/auth`: one `name`, no CF Access, squashed migrations (cw-s3)

**DONE 2026-09-28** (cw-s3, on the disk-tree `cloud` base). What each item became is under its checkbox; the one step outside git is the production bookkeeping rewrite at the end.

*(From the `$oa/auth` session, 2026-09-25.)* This is the cw-s3 twin of `main`'s `specs/auth-bump-name-squash.md`; read that file for the full list of upstream changes. Per Ryan: move to the current version, then delete anything that only existed for earlier versions, because it's all alpha.

## Upstream changes, in brief

- **One `name` replaces `first`/`last`** (auth `26bf7b7`).
  - Read `subject.name` / `profile.name`.
  - `POST /grants` takes the recipient as `subjectName`.
  - `PUT /profile` takes `{ name, avatar }`.
  - `RequestAccessForm`'s `askName` is a boolean.
  - The DB change is `git -C $oa/auth show 26bf7b7:migrations/0013_single_name.sql`.
- **`@open-athena/auth/cf-access` is deleted**: no `verifyAccessJwt` or `ssoHandler`, and `SignInPanel` has no `signInUrl`.
- **Auth's migrations are squashed** into `migrations/0001_init.sql`. `scripts/d1-rebaseline.mjs` moves an existing DB across a squash.


**Also removed or renamed in the same cleanup** (auth `76c23b5`):
- `AppWhoami` → `Whoami`. `EdgeWhoami` is gone.
- `WhoamiSource` is just `{ endpoint? }`, and `AuthGate`'s `source` is optional (default `/api/auth/whoami`).
- `DEFAULT_ENDPOINTS` → `DEFAULT_WHOAMI_ENDPOINT`.
- `SignInPanel` loses `signInUrl` / `signInLabel`.
- Root `schema.sql` and the `./schema.sql` export are gone. A fresh install applies `migrations/0001_init.sql`.

**Target pin:** auth dist **`7442ab0`** (`0.1.0-dist.4c28b9d`) or later.

## To do on this branch

- [x] **Finish `specs/oidc-cutover-cw.md` first.** *Done 2026-09-28: P0–P5, Access app `4c463052` deleted; that spec has the record.* This bump deletes the adapter `functions/auth/sso.ts` and `functions/_lib/auth.ts` use. So cw-s3.oa.dev has to sign people in through `/auth/google` + `/auth/email/*` (already built, dormant until the secrets exist) before the Access edge goes away. The cutover's Google client should be created in **`oa-auth-509611`** ("Open Athena" branding; see `main`'s spec), not in Jesse's `oa-internal-450019`.
  - Pre-fill the client form in the OA Chrome profile and let Ryan click Create.
  - Store it with:
    ```
    direnv exec $oa/auth node $oa/auth/scripts/provision-oauth-client.mjs --from-json <downloaded JSON> --app-origin https://cw-s3.oa.dev --redirect-uri https://cw-s3.oa.dev/auth/google/callback --pages-project <cw-s3 Pages project> --wrangler <OA-account wrangler wrapper> --run
    ```
  - Set `RESEND_API_KEY` / `MAIL_FROM` for email codes. `noreply@oa.dev` is verified in Resend.
- [x] **Bump the pin** *(`7442ab0`, arrived with the base at the `e2887c9` rebuild; `0020_auth_single_name` = the package's `0013`)* in `site/package.json` to auth's latest dist SHA, which must include the squash.
- [x] **Remove CF Access:** *`functions/auth/sso.ts` + `login.ts` deleted (P4, `72fcded`); `edgeIdentity`, `_lib/cfAccess.ts`, `ACCESS_*`/`EDGE_TRUSTED`, the client's `edge` branches and the `edge` build mode removed as `[base]` `f6fb291`; `wrangler.toml` clean; `cf/` never modeled the app; the Zero Trust app deleted 2026-09-28.*
  - Delete `functions/auth/sso.ts`, the `Cf-Access-Jwt-Assertion` source in `functions/_lib/auth.ts` (`verifyAccessJwt`, `ACCESS_TEAM_DOMAIN`, `ACCESS_AUD`, `TEAM_DOMAIN`) and `auth.test.ts`'s Access cases.
  - Remove `ACCESS_TEAM_DOMAIN` / `ACCESS_AUD` from `wrangler.toml`, and any Access app in `cf/`'s stack.
  - Then Ryan deletes the Zero Trust app **"CoreWeave usage (cw-s3.oa.dev)"** (Access → Applications). The auth session saw it still exists, covering `cw-s3.oa.dev`, `oa-cw-s3-usage.pages.dev` and `*.oa-cw-s3-usage.pages.dev`.
  - Delete it only after Google/email sign-in works on cw-s3.oa.dev.
- [x] **Move to `name`:** *nothing on cw referenced `first`/`last` beyond the migration; `0020` migrated the rows.* any `first`/`last` for a person.
- [x] **Catch up the auth tables** *(`0020_auth_single_name` was the only gap vs the package's `0001_init.sql`; applied to production 2026-09-28)* copied into `site/migrations/cw/`, which currently stop around `0018_allowed_emails`. Diff them against `$oa/auth/migrations/0001_init.sql`, then add and apply one catch-up migration: local first, then remote with Ryan's OK.
- [x] **Squash `site/migrations/cw/`** *(`6048548`: `0001_init.sql` generated from the exact schema the 20 files build, verified structurally, `d1-rebaseline` dry-run vs production: schemas match, 222 objects). **Remaining, by hand (a D1 write):** `direnv exec . node ~/c/oa/auth/scripts/d1-rebaseline.mjs --db oa-cw-s3-usage-db --migrations-dir site/migrations/cw --remote --wrangler site/tmp/oa-wrangler.sh --run` from `wt/cw-s3`; until then `wrangler d1 migrations apply --remote` must NOT be run against production (it would try to apply the baseline on top).* into one `0001_init.sql` (app and auth tables, commented by what they are now).
  - Get every DB to head first.
  - Then run `node $oa/auth/scripts/d1-rebaseline.mjs --db <cw-s3 D1 name> --migrations-dir site/migrations/cw [--local|--remote] --wrangler <wrapper>`: dry-run, then `--run`. The remote write needs Ryan's OK.
- [x] **Remove leftovers:** *auth module header, whoami, AuthGate, vite, wrangler comments rewritten in `f6fb291`/`27ac5b5`; `emailcode.ts`'s "ZT One-Time-PIN replacement" line is the package's wording and stays.* comments narrating past auth versions or Access / Zero Trust ("ZT One-Time-PIN replacement", "Tier-1", etc.). Leave `specs/done/` alone.
- [x] **Parity:** *ledger entry 2026-09-28 ("auth-bump squash closed").* this and `main`'s spec cover the same auth surface. Record in the CP ledger which parts are shared and which are branch-specific (cw-s3's Access removal and cutover).

## Done when

- cw-s3.oa.dev signs people in with Google or an emailed code, and the Access app is deleted.
- No `cf-access` / `ACCESS_*` / `first`/`last` references remain.
- `migrations/cw/` is a single rebaselined `0001_init.sql`.
- CI, deploy and sign-in all verified.

# Bump `@open-athena/auth`: one `name`, no CF Access, squashed migrations

*(From the `$oa/auth` session, 2026-09-25.)* Upstream went through three alpha-cleanup changes. Per Ryan: "each repo just [moves to the current version], then rm evidence of the previous versions — this is all so alpha that we don't need to preserve stuff about every abandoned early version forever." So this bump is also a cleanup pass: once gcs is on the new version, delete anything that only existed to support older ones.

## Upstream changes

1. **One `name` for a person, not `first`/`last`** (auth `26bf7b7`), because a given/family split is lossy across cultures.
   - `Subject`, `Profile` and `ProfileInput` carry `name`; `cleanSubject({ name })`.
   - `GET`/`PUT /profile` speak `{ name, avatar }`, and the multipart field is `name`.
   - `POST /grants` takes the recipient as **`subjectName`**. The body's `name` is still the link's admin label (the memo).
   - `POST /request`'s `name` also becomes the request's `subject.name`.
   - `RequestAccessForm`'s `askName` is a boolean; `'split'` is gone.
   - `ProfilePanel` has one `name` label.
   - When seeding a profile from Google at sign-in, the `name` claim wins; `given_name` + `family_name` are joined only as a fallback.
   - The DB change is auth's old `migrations/0013_single_name.sql`, which is no longer in auth's tree after the squash: `git -C $oa/auth show 26bf7b7:migrations/0013_single_name.sql`. It adds `profiles.name` backfilled from the pair, drops `profiles.first`/`last`, and rewrites `first`/`last` keys in `grants.subject_json` and `access_requests.subject_json` into `name`.
2. **The `@open-athena/auth/cf-access` adapter is deleted**, along with `verifyAccessJwt`, `ssoHandler` and `SignInPanel`'s `signInUrl` generic-SSO button.
3. **Auth's migrations are squashed** into `migrations/0001_init.sql`: the current auth schema, with no history. Auth also ships `scripts/d1-rebaseline.mjs`, which tells an existing D1 database that a squashed migrations directory is already applied. It first checks that the live schema matches what the directory builds, and refuses to rewrite if not. It's dry-run by default, with `--run` to write.

## To do here

- [ ] **Bump the pin** in `site/package.json` to auth's latest dist SHA (`git -C $oa/auth fetch o dist && git -C $oa/auth log -1 o/dist`); it must include the squash.
- [ ] **Move to `name`:**
  - `site/src/AdminPage.tsx` currently stores one freeform name in `subject.first` (lines ~56–60, ~128). Use `subject.name`, and pass `subjectName` when minting.
  - Update anything else that reads `first`/`last` for a person, including `displayName` workarounds.
- [ ] **Migration for the auth tables** (gcs copies them into its own sequence):
  - Bring the copied auth tables level with auth's final schema. Diff your auth tables against `$oa/auth/migrations/0001_init.sql`: gcs's copies stop around `0030_auth_grant_rotate`. `pending_auth.ip_hash` and the `name` change are likely missing; check.
  - Add one `NNNN_auth_catch_up.sql` and apply it: local first, then remote with Ryan's OK.
- [ ] **Remove CF Access:**
  - Delete the `Cf-Access-Jwt-Assertion` path in `site/functions/_lib/auth.ts` (identity source 1: `verifyAccessJwt`, `ACCESS_TEAM_DOMAIN`, `ACCESS_AUD`, `TEAM_DOMAIN`) and its tests.
  - gcs.oa.dev has had no Access app since the OIDC cutover, so this is dead code.
  - Also remove the matching comments in `wrangler.toml`.
  - This is shared with the `cw-s3` branch (see `specs/branch-parity-discipline.md`). cw-s3 drops Access in its own spec (`wt/cw-s3/specs/auth-bump-name-squash.md`), so the parity ledger can mark this as landed on both.
- [ ] **Move the OAuth client off Jesse's project.** gcs.oa.dev's Google client lives in `oa-internal-450019`, whose consent screen is branded "OA GDrive Upload" (Jesse's). Auth now has an OA project for this: **`oa-auth-509611`** ("Open Athena" branding, External, In production, support/contact `auth@openathena.ai`, authorized domains `oa.dev`, `tail4a3a97.ts.net`, `rbw.sh`).
  - Create a "gcs.oa.dev" Web client there with origins `https://gcs.oa.dev` (plus any dev origins) and matching `/auth/google/callback` redirect URIs. Ryan clicks Create; the auth session or yours can pre-fill the form in the OA Chrome profile.
  - Download its JSON, then run:
    ```
    direnv exec $oa/auth node $oa/auth/scripts/provision-oauth-client.mjs --from-json <json> --app-origin https://gcs.oa.dev --redirect-uri https://gcs.oa.dev/auth/google/callback --pages-project oa-gcs-usage --wrangler <OA-account wrangler wrapper> --run
    ```
    This checks the client lists those origins, then stores `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET`. Delete the JSON after.
  - Redeploy, then verify sign-in.
  - Only after that, delete the old client in `oa-internal-450019` (Ryan's click).
- [ ] **Squash gcs's own migrations.** gcs has ~30 files, many of them transitions to abandoned shapes (e.g. `0019`→`0021` index_groups).
  1. Get every DB (local and remote `oa-gcs-usage-auth`) to the current head.
  2. Replace `site/migrations/` with one `0001_init.sql` holding the current schema, app and auth tables together, grouped with comments about what each table is (not its history).
  3. Run `node $oa/auth/scripts/d1-rebaseline.mjs --db oa-gcs-usage-auth --migrations-dir site/migrations [--local|--remote] --wrangler <wrapper>`. Dry-run it first; it must report the schemas match. Then `--run`; the remote write needs Ryan's OK.
  - Update `wrangler.toml`'s migration comment, which lists package migration numbers.
- [ ] **Remove other old-version remnants** found along the way: comments narrating past auth versions, compat branches, and "Tier-1" (CF Access) wording. Leave `specs/done/` alone; those are records.

## Done when

- gcs is on an auth pin that includes the squash.
- Nothing references `first`/`last` for a person, or `cf-access` / `ACCESS_*`.
- `site/migrations/` is `0001_init.sql` plus nothing else (until the next change), and both DBs are rebaselined.
- The Google client lives in `oa-auth-509611`.
- CI, deploy and sign-in all verified.

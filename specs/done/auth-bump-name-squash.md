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


**Also removed or renamed in the same cleanup** (auth `76c23b5`):
- `AppWhoami` → `Whoami`. `EdgeWhoami` is gone.
- `WhoamiSource` is just `{ endpoint? }`, and `AuthGate`'s `source` is optional (default `/api/auth/whoami`).
- `DEFAULT_ENDPOINTS` → `DEFAULT_WHOAMI_ENDPOINT`.
- `SignInPanel` loses `signInUrl` / `signInLabel`.
- Root `schema.sql` and the `./schema.sql` export are gone. A fresh install applies `migrations/0001_init.sql`.

**Target pin:** auth dist **`7442ab0`** (`0.1.0-dist.4c28b9d`) or later.

## To do here

*Progress (gcs session, 2026-09-25):* everything below is done and deployed (`d765a4e` + the squash commit). Left for Ryan: verify a real Google sign-in on gcs.oa.dev with the new client, then delete the old client in `oa-internal-450019`; after that this spec moves to `specs/done/`.

- [x] **Bump the pin** in `site/package.json` to auth's latest dist SHA (`git -C $oa/auth fetch o dist && git -C $oa/auth log -1 o/dist`); it must include the squash. → `7442ab0` (`0.1.0-dist.4c28b9d`). With it: `WhoamiSource` is `{ endpoint? }` with a default, so gcs's `WHOAMI_SOURCE` went; the dev stub is a full `SsoWhoami` (kind/admin/scopes), so the scope hooks treat a missing identity as signed out rather than dev-full.
- [x] **Move to `name`:** `AdminPage.tsx` sends `subjectName`, its `Subject` type is `{ name?, email?, avatar? }`, and the holder column reads `subject.name`. Nothing else read `first`/`last` for a person. Verified on the local stack: a link minted as "Lovelace Ada (家族名先)" stores `subject_json = {"name": …}` and shows that string as the holder.
- [x] **Migration for the auth tables:** gcs's copies (`0001`–`0006`, `0027`–`0030`) already covered `pending_auth.ip_hash` (`0029`) and grant rotate (`0030`); the only gap was the `name` change → `0031_auth_single_name.sql` (auth's `0013` verbatim). Applied locally, then remotely on Ryan's OK (2026-09-25, before the deploy): `profiles.name` present, `first`/`last` gone, one grant carries `subject.name`. Known delta, deliberate: gcs's `allowed_emails` (`0008`) has its own shape (`note`/`who`/`ts`, consulted by `scopesFor`), not auth's `0012` (`scopes`/`source`/`added_by`); the squash keeps gcs's.
- [x] **Remove CF Access:** `functions/_lib/auth.ts` no longer imports `cf-access`; `edgeIdentity`, `isAdmin`, `TEAM_DOMAIN`, `ACCESS_TEAM_DOMAIN`, `ACCESS_AUD` and the `EDGE_TRUSTED` seam are gone (`Identity.via` is `'session' | 'grant'`). The client's `VITE_AUTH_MODE=edge` branch (`/login`, `/cdn-cgi/access/logout`) went with it: `WHOAMI_SOURCE` is `{ kind: 'app' }`. There were no tests for the edge path. `wrangler.toml`'s comments updated, and its `remote = true` D1 flag restored (it had been committed stripped in `b533957`, from a dev stack running in `--local-db` mode). Parity: cw-s3 drops the same code in its own spec.
- [ ] **Move the OAuth client off Jesse's project.** gcs.oa.dev's Google client lives in `oa-internal-450019`, whose consent screen is branded "OA GDrive Upload" (Jesse's). Auth now has an OA project for this: **`oa-auth-509611`** ("Open Athena" branding, External, In production, support/contact `auth@openathena.ai`, authorized domains `oa.dev`, `tail4a3a97.ts.net`, `rbw.sh`).
  - Create a "gcs.oa.dev" Web client there with origins `https://gcs.oa.dev` (plus any dev origins) and matching `/auth/google/callback` redirect URIs. Ryan clicks Create; the auth session or yours can pre-fill the form in the OA Chrome profile.
  - Download its JSON, then run:
    ```
    direnv exec $oa/auth node $oa/auth/scripts/provision-oauth-client.mjs --from-json <json> --app-origin https://gcs.oa.dev --redirect-uri https://gcs.oa.dev/auth/google/callback --pages-project oa-gcs-usage --wrangler <OA-account wrangler wrapper> --run
    ```
    This checks the client lists those origins, then stores `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET`. Delete the JSON after.
  - Redeploy, then verify sign-in.
  - Only after that, delete the old client in `oa-internal-450019` (Ryan's click).
  - → Done 2026-09-25: client `190203990714-ssg5…` "gcs.oa.dev (marin-gcs-usage)" in `oa-auth-509611` (origins + callbacks for gcs.oa.dev, localhost:3253, localhost:3257; the agent pre-filled the form, Ryan clicked Create). Secrets stored on the Pages project + `site/.dev.vars` via the provisioning script with `--wrangler site/tmp/oa-wrangler.sh` (untracked; `direnv exec $oa/auth` swaps the env to auth's, so the wrapper falls back to its `CLOUDFLARE_ADMIN_TOKEN`). JSON deleted. Prod `/auth/google` now redirects with the new client id. **Ryan: verify a real Google sign-in on gcs.oa.dev, then delete the old client `965762293918-f92j…` in `oa-internal-450019`.**
- [ ] **Squash gcs's own migrations.** gcs has ~30 files, many of them transitions to abandoned shapes (e.g. `0019`→`0021` index_groups).
  1. Get every DB (local and remote `oa-gcs-usage-auth`) to the current head.
  2. Replace `site/migrations/` with one `0001_init.sql` holding the current schema, app and auth tables together, grouped with comments about what each table is (not its history).
  3. Run `node $oa/auth/scripts/d1-rebaseline.mjs --db oa-gcs-usage-auth --migrations-dir site/migrations [--local|--remote] --wrangler <wrapper>`. Dry-run it first; it must report the schemas match. Then `--run`; the remote write needs Ryan's OK.
  - Update `wrangler.toml`'s migration comment, which lists package migration numbers.
  - → Done 2026-09-25: `site/migrations/0001_init.sql` (auth tables first, then allowlist/identity, ledger, index metadata, plans + sweeps; each table introduced by what it's for). Dry runs matched on both databases (266 objects/columns), then `--run` rewrote `d1_migrations` on local and remote; `wrangler d1 migrations list` is clean on both. `wrangler.toml`'s comment no longer cites numbers.
- [x] **Remove other old-version remnants** found along the way: comments narrating past auth versions, compat branches, and "Tier-1" (CF Access) wording. Leave `specs/done/` alone; those are records. → "Tier 2", "ZT-free replacement for `/auth/sso`", "behind CF Access" and "edge session" wording gone from `src/`, `functions/`, `README.md` and `site/deploy`.

## Done when

- gcs is on an auth pin that includes the squash.
- Nothing references `first`/`last` for a person, or `cf-access` / `ACCESS_*`.
- `site/migrations/` is `0001_init.sql` plus nothing else (until the next change), and both DBs are rebaselined.
- The Google client lives in `oa-auth-509611`.
- CI, deploy and sign-in all verified.

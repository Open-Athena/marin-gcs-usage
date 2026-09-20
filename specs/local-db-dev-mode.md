# Off-prod dev: a local-D1 mode + upstream the dev sign-in

Testing changes that span the stack (auth + DB writes) currently runs against the
**real prod ledger**: `site/wrangler.toml` pins the auth D1 with `remote = true`,
so a local `mint` / `rotate` / `revoke` / `mark` / `stage` writes prod rows. Reads
are harmless; mutations are a footgun (the project's standing "a stage/trash
gesture from localhost writes REAL rows to prod" hazard).

This spec is **not blocking** any current change — it's the durable fix for the
recurring "I need to exercise write flows without touching prod" need. File it,
schedule it when the mutation-testing need actually bites.

## Context — what already works (2026-09-19)

Local `/admin` is now testable at all: `.dev.vars` gained a `SESSION_SECRET`, and
`functions/api/auth/[[path]].ts` auto-signs-in `DEV_EMAIL` on localhost (host +
`DEV_EMAIL` guarded → inert on gcs.oa.dev). See commit 3e0413d. What's left is
**data isolation** — that dev session still reads/writes prod D1.

## 1. `./dev --local-db` (this repo, ~45 min)

A dev mode that points the auth D1 at wrangler's built-in **local** SQLite instead
of the remote prod DB.

- **The toggle.** `wrangler.toml` can't switch `remote` by flag, so the clean
  options are (pick one during impl):
  - a second config (`wrangler.local.toml` / a `[env.local]` block) that omits
    `remote = true`, selected via `wrangler pages dev --config` / `--env`; or
  - `wrangler pages dev --d1 DB=oa-gcs-usage-auth` to bind a local D1 by name,
    overriding the toml's remote binding.
  `./dev --local-db` sets `WPORT`'s wrangler invocation accordingly; default
  `./dev` stays remote (some workflows deliberately drive the prod ledger from
  localhost — keep that possible).
- **Schema.** `wrangler d1 migrations apply oa-gcs-usage-auth` (no `--remote`)
  builds all migrations into the local SQLite under `.wrangler/state`.
- **Seed (`site/seed-local-db`).** The real work. Minimum for auth testing: one
  `allowed_emails` row (a non-staff test viewer) — grants can then be minted
  through the working local console. For mark/owner flows, add a handful of
  `actions` rows. Keep it a small, re-runnable script (idempotent upserts), not a
  dump of prod (no PII in a tracked seed — synthetic addresses only).
- **Out of scope here:** `/data`, treemap, `/api/subtree`, mark/**sweep** read the
  GCS bucket + snapshot JSONs, not D1 — unaffected by the D1 toggle. Testing those
  off-prod is a separate (bigger) concern; don't fold it in.

Acceptance: `./dev --local-db` → `localhost:3253/admin` mints/rotates/revokes with
**zero** writes to the prod `oa-gcs-usage-auth` D1 (verify: prod grant count
unchanged before/after a local mint).

## 2. Upstream the dev sign-in (auth package — coordinate with the auth session)

The localhost auto-sign-in in `[[path]].ts` is the reusable piece: every
`@open-athena/auth` consumer hits the same wall (the admin routes authenticate by
session cookie, not the app's own dev-identity stub), and each re-derives this by
hand. Propose a guarded helper in the package — e.g. a `devSignIn(gate, req, {
email, when })` or a `RouteOptions.devIdentity` hook — living alongside the
`dist/testing/` memory stores. The app then drops its bespoke shim and calls the
package helper (still host-guarded on the app side).

This is the highest-leverage generalization: the DB layer differs per stack (no
shared lever), but local auth is identical for every consumer of this package.
Owned by the auth session (`~/c/oa/auth/specs/`); this repo just adopts once it
lands.

## Cross-project note

The recurring pain across projects isn't "stand up a local DB" (each stack's
runtime usually ships one — wrangler here). It's **seed discipline**: realistic
enough data to exercise the flow, re-runnable, no prod PII. Worth standardizing a
`seed-*` script convention rather than solving per-repo each time.

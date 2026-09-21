# Off-prod dev: a local-D1 mode + upstream the dev sign-in

> **Status (2026-09-20): §1 shipped** — `./dev --local-db` binds the auth D1 to
> wrangler's local miniflare SQLite (prod untouched); verified live (startup
> banner reports `env.DB … D1 Database local`). §2 (upstream the dev sign-in to
> `@open-athena/auth`) remains, owned by the auth session — this repo adopts once
> it lands. Moved to `done/` on the strength of §1; §2 is a cross-project
> follow-on, not remaining mgu work.

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

## 1. `./dev --local-db` (this repo) — DONE 2026-09-20

A dev mode that points the auth D1 at wrangler's built-in **local** SQLite instead
of the remote prod DB.

- **The toggle (as built).** The anticipated levers all failed: `wrangler pages
  dev` has **no `--config`/`--env`** (only global `--cwd`, which relocates the
  project root and breaks `functions/` discovery), and `--d1 DB=…` to override the
  toml binding is unverified — and unsafe, since a silent remote-bind would write
  prod (the exact hazard). What works, correct-by-construction: `remote = true` is
  the *only* thing forcing the binding remote, so `--local-db` runs `pages dev`
  against a copy of `wrangler.toml` with that one line stripped (backup → `grep -v`
  → restore on exit via `trap`; a hard kill leaves the strip in place,
  `git checkout wrangler.toml` recovers). Default `./dev` stays remote (some
  workflows deliberately drive the prod ledger from localhost). Local mode also
  drops the `CLOUDFLARE_API_TOKEN` mask + account pin + tunnel-relaunch loop (none
  needed with no remote binding). Verified: startup banner reports
  `env.DB (oa-gcs-usage-auth)  D1 Database  local`.
- **Schema.** First `--local-db` with no local D1 yet runs `wrangler d1 migrations
  apply oa-gcs-usage-auth --local` (all 30 migrations, empty ledger) — fully
  offline, no OAuth. `--refresh`/`--staging` instead mirrors the real prod ledger
  in via the existing `./refresh-db` (`d1 export --remote` → local import; needs a
  live `wrangler login`).
- **Seed.** Covered by the two schema paths above (`--refresh` = prod mirror,
  gitignored dump, local-only, never pushed back; bare `--local-db` = empty
  schema). A small synthetic idempotent `seed-local-db` (no-PII test rows) was
  **not** added — `refresh-db` already gives realistic data and the empty schema
  suffices to mint from scratch; revisit only if a no-login realistic seed is
  wanted.
- **Out of scope (unchanged):** `/data`, treemap, `/api/subtree`, mark/**sweep**
  read the GCS bucket + snapshot JSONs, not D1 — unaffected by the D1 toggle.

Acceptance (met): `./dev --local-db` boots with the D1 binding in `local` mode
(startup banner) — zero writes reach the prod `oa-gcs-usage-auth` D1. Full
`--refresh` mint/rotate/revoke round-trip pends a live `wrangler login` (OAuth was
expired at impl time); the binding-mode proof already establishes prod isolation.

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

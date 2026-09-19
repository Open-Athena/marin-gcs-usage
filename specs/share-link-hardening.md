# Share-link hardening: read-only scope, rotate, token self-ID

Three related gaps surfaced while wiring the `/admin` share-link console for a
guest link (Rob). This spec covers all three; **(1) is the priority** and is
entirely app-side (this repo), **(2)/(3)** touch the `@open-athena/auth` package
(coordinate with the cw-s3 / auth session that owns `auth/specs/`).

## 1. A real read-only scope (`gcs:read`)

### Problem
"Read-only" is not a scope today — it is an ad-hoc `if (!id.email) → 403` guard
in `functions/api/actions.ts`. Two consequences:

- A share link minted **with an email** (magic-link binding) gets `id.email` set
  (`authIdentity`: `email: auth.grant.email ?? null`), so it **passes** the
  write-guard and can `POST /api/actions` (mark keep/sweep, reassign owners) on
  the prod ledger. Binding a guest's email silently un-read-only's them.
- `POST /api/plans/stage` (the trash gesture) is gated only by `requireViewer`
  (the base `gcs` scope) with **no email check at all**, so *any* `gcs` viewer —
  even an emailless guest — can stage deletion proposals into the prod plan.

### Design
Introduce `GCS_READ_SCOPE = 'gcs:read'` — a read-only viewer scope.

- `gcs` = full viewer (read **and** write: mark + stage).
- `gcs:read` = read-only viewer (every GET/read endpoint; no writes).

Gate changes in `functions/_lib/auth.ts`:

- `requireViewer` accepts **either** `gcs` or `gcs:read` (all read endpoints
  keep calling it — one change covers them). Implement as `requireAnyScope(ctx,
  [baseScope, baseReadScope])`.
- New `requireWriter(ctx)` requires the full `gcs` scope. The write endpoints
  switch to it, and the ad-hoc `!id.email` guard is **removed** (email stays,
  for owner attribution only).

Endpoint audit (from `grep requireViewer|requireAdmin`):

- **→ `requireWriter` (needs `gcs`)**: `api/actions.ts` POST, `api/plans/[[path]].ts`
  `/stage` POST. (`api/marks.ts` POST — verify; it also has a `!id.email` guard.)
- **stays `requireAdmin`**: plans create/close/items, `sweep/dispatch`, `sweep/stop`.
- **stays `requireViewer` (now accepts `gcs:read`)**: `resolve`, `todo`, `subtree`,
  `path-index`, `series`, `estate`, `sweep-owners`, `marks` GET, `marks/totals`,
  `diff`, `bench`, `claims`, `data/[[path]]`, `plans` GET, `sweep/jobs` GET.

Mint + UI:

- `/admin` mint form gets a **Read-only** checkbox, default **on**. On → mint
  with `scopes: ['gcs:read']`; off → `['gcs']`. (`POST /api/auth/grants` already
  takes arbitrary `scopes`, no package change.)
- Self-tokens (`/api/token`) keep `gcs` — they exist to drive `dt-cloud mark`.
- Frontend: hide write affordances (the children-table trash button, mark
  controls) when `whoami.scopes` lacks `gcs` (has only `gcs:read`), so a
  read-only holder sees a clean read-only dashboard rather than buttons that 403.

Branch parity: cw-s3 mirrors this as `cw:read` (see `branch-parity-discipline.md`).

## 2. Rotate (in addition to revoke)

Rotate = issue a **new token** for the **same grant** (same subject / scopes /
expiry), invalidating the old one — for when a link leaks but the grant is still
wanted. Agent tokens already rotate (`POST /api/token` is "mint/rotate"); share
links have only revoke / disable / enable / PATCH.

- **Upstream** (`@open-athena/auth`): add `gate.rotate(id)` → new `token_hash`
  on the existing grant row (or mint-new-copying-metadata + revoke-old), return
  the raw token once. Add `POST <base>/grants/:id/rotate`.
- **gcs**: a "rotate" button beside "revoke"; on success show the new
  `?key=…` once (reuse the mint reveal panel).

## 3. Token self-ID / last-N chars

"Can a holder ID their token to an admin later?" — **already yes.** `gate.whoami`
returns the grant **`id`** (plus `name`, `subject`, `scopes`, `expiresAt`) to the
holder; the id is explicitly the non-secret identifier "a person can quote back
to an admin." A CLI-token user runs `curl /api/auth/whoami -H 'Authorization:
Bearer <token>'` and reads their grant id/name.

So last-N-chars is a **convenience, not a requirement** — the grant id already
correlates holder ↔ admin table without exposing any token material. If still
wanted (eyeball the table without asking the holder):

- **Upstream**: store a `token_hint` (last 4–6 chars) at mint; return it in the
  grants list. Small, permanent, low-sensitivity.
- **gcs**: show it dim in the holder/created cell.

Prefer surfacing the existing grant **`id`** in the admin table first (zero
security cost) and only add `token_hint` if that proves insufficient.

## Sequencing

1. Read-only scope (this repo) — unblocks minting Rob as genuinely read-only.
2. Surface grant `id` in the admin table (this repo, trivial) — the self-ID path.
3. Rotate (upstream + gcs UI).
4. `token_hint` (upstream + gcs UI) — only if grant-id proves insufficient.

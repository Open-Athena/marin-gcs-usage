# Auto-sync the per-user mark-status → Percy's Google Sheet

> **✅ Shipped 2026-08-31.** Option B (hourly, GCP-side, no stored key), as a
> **standalone lightweight image** (`sheet-sync/`) rather than the heavy daily
> Batch image — the sync needs only the `gcs-usage` CLI, so a dedicated
> `python:3.12-slim` + `marin[sheets]` image keeps hourly runs cheap and
> cold-start fast. Cloud Run job `gcs-sheet-sync` + hourly Cloud Scheduler
> `gcs-sheet-sync-hourly` (`5 * * * *` UTC), both as the snapshot SA
> `gcs-usage-job@…`. Validated end-to-end incl. the scheduler→job invoke path.
> Path ① (grant token → `/api/actions`). See `sheet-sync/README.md`.

**Commitment:** keep the shared GSheet
(`docs.google.com/spreadsheets/d/1k_11LA21g8uqMckPhkKvwrnRENVKF8yHxbW5NnUiRFc`)
updated with the per-user "who still needs to mark & sweep" rollup, without a
human re-exporting it. Ideally it refreshes through the day so marks people make
show up within ~an hour.

## What we already have

- **`gcs-usage report -a <actions> -o mark-status.csv`** — produces exactly the
  sheet's rows: per-user keep/sweep/undecided, `keep_last_ckpt` decomposed, live
  claims applied as a WAL, sorted by undecided bytes (nag order), ownerless pools
  last. Mirrors the `/users` page. `actions` may be the `/api/actions` URL.
- **`/api/marks/totals`** — the *same* numbers, exact, computed server-side in the
  Worker (ledger × floor-free path index). So a Cloudflare-side syncer needs **no
  Python and no snapshot read** — it can format the CSV straight from this.
- **The daily Batch job** (GCP) already has gcsfs creds, `DATA_BUCKET`, and the
  CLI installed, and runs once/day.

The only genuinely new thing, whichever path we pick, is **Google Sheets write
auth** + a place to run it on a schedule.

## The constraint that shapes this

**Cloudflare *Pages* Functions can't run cron** — scheduled handlers are a
*Workers* feature. So "an hourly job inside the existing site" isn't a thing; an
hourly Cloudflare syncer must be a **separate Worker** with `[triggers] crons`.
That splits the options by *where the schedule lives* and *what auth it uses*:

### Option A — daily, from the existing Batch job (ambient GCP auth) ✅ ship now

Add a step to `job/run.sh` after the snapshot:

```bash
gcs-usage report -a https://gcs.oa.dev/api/actions -o /tmp/mark-status.csv
gcs-usage sheet-push 1k_11LA…RFc /tmp/mark-status.csv   # new tiny subcommand
```

`sheet-push` uses **Application Default Credentials** — the Batch job's GCP
service account — so **no key material anywhere**: just
`gspread`/`google-api-python-client` with the SA's ambient token.

- **Auth setup (one-time, needs you):** enable the Sheets API in the GCP
  project, and **share the sheet (Editor) with the Batch job's SA email** (the SA
  in `job/batch-submit.sh`). That's the whole ask.
- **Freshness:** up to 24 h stale (refreshes on the 07:00 UTC daily run).
- **Cost:** ~zero new infra; one CLI subcommand + one `run.sh` line.

### Option C — hourly, a Cloudflare Worker cron (the "CFW /1hr")

A standalone Worker `gcs-sheet-sync`, `[triggers] crons = ["0 * * * *"]`, that:

1. `GET`s its own `/api/marks/totals` (numbers already exact — no recompute),
2. formats the CSV,
3. pushes to the sheet via the Sheets REST API.

Sheets auth in a Worker = **Google service-account JWT**: sign a JWT with the SA
private key using WebCrypto (`RS256`), exchange at `oauth2.googleapis.com/token`
for an access token, `PUT
spreadsheets/{id}/values/{range}?valueInputOption=RAW`.

- **Auth setup (needs you):** create a Google service account, download its JSON
  key, **store the private key as a CF secret** (`wrangler secret put`), and share
  the sheet (Editor) with the SA email. ⚠️ This stashes a **Google private key in
  Cloudflare** — a real secret-handling decision; flag before doing it.
- **Freshness:** ~hourly (Cron Triggers fire within a couple minutes of the hour).
- **Cost:** a new Worker + a stored Google key; but the CSV builder is trivial
  because `/api/marks/totals` already did the hard part.

### Option B — hourly, GCP-side (Cloud Scheduler → Cloud Run)

Cloud Scheduler (hourly) → a one-shot Cloud Run job running the same
`report` + `sheet-push` as Option A, **reusing the Batch image and ambient SA
auth** (no key material). True hourly, no Google key in Cloudflare — at the cost
of a second GCP schedule + Run job.

## Decision: Option B (hourly, GCP-side, no stored key)

Chosen over A because the sheet's consumer expects current numbers, not an
artifact of a daily cron; chosen over C to avoid stashing a Google private key in
Cloudflare. It reuses the Batch image + ambient SA auth and adds one hourly
Cloud Scheduler → Cloud Run job. The `report` CSV and `/api/marks/totals` are the
same numbers, so the rows are identical either way — this is purely *schedule +
auth model*.

## Implementation

### `gcs-usage sheet-push` — **built** (`cli.py`)

`gcs-usage sheet-push [-w tab] [-n] <sheet-id> [csv|-]`: reads the CSV (stdin by
default), auths via **ADC** (`google.auth.default(scopes=[…/spreadsheets])`),
and **full-replaces one existing tab in place** (default: the first). The sheet
is a **read-only mirror** of the site's `/users` table — the audience is
Commenter, the job's SA the sole Editor — so there are no manual cells to
preserve, and Google's version history is the audit trail. `clear()` is
values-only, so header styling / frozen rows survive. Idempotent. Pipe from
`report`:

```bash
gcs-usage report -a ledger.json | gcs-usage sheet-push <sheet-id>
```

Deps: the `sheets` extra (`gspread>=6.0`) — installed in the job image.

### Reading the ledger headless — token vs. D1-direct

`report -a <actions>` needs the live actions ledger. Two ways to get it headless,
and the auth model decides:

`GET /api/actions` is **not** behind the browser Access gate — `requireScope`
tries `Cf-Access-Jwt` first but then the `@open-athena/auth` gate, which accepts
`Authorization: Bearer` / `?key=` (D1-backed grant tokens). It needs only the
`gcs` scope. So the API is reachable headlessly with a token — that's the whole
point of the token system.

- **(1) Minted grant token → `/api/actions`** — mint a `gcs`-scoped grant (a
  named, revocable `$oa/auth` token — or, quick-start, a personal `/api/token`),
  put it in Secret Manager, and run `report -a https://gcs.oa.dev/api/actions`
  with the token in env. **Reuses the API's expansion/join** ("expanded rows
  joined to their raw action's provenance") — zero logic duplicated, and it's
  exactly what `$oa/auth` grants are for. Cost: one scoped, revocable secret.
- **(2) Straight from D1** — read the raw `actions` table over the CF D1 HTTP API
  (the daily job already does this for `index-sync`) and reshape to `report`'s
  `{keeps, owners}`. No new secret, but it **re-implements `/api/actions`'s
  expansion in Python** — drift risk on the one piece of logic that matters.

**Recommend (1).** It reuses the tested API path verbatim, and a `gcs`-**read**
grant is *narrower* than handing the hourly job the CF D1 token (D1 write). The
earlier token leak was a logging-hygiene bug (fixed: `set +x` around the token
test) — not a reason to avoid a scoped, revocable grant. (The snapshot tree/meta
still come from GCS via the job's ambient SA; only the *marks* need to be hourly,
and the token fetches those live.)

### Hourly trigger — shipped (`sheet-sync/`)

A **standalone** dir, independent of the daily Batch image:

- `sheet-sync/Dockerfile` — `python:3.12-slim` + `pip install ./marin[sheets]`
  (no site build, no wrangler, no disk-tree engine — imported lazily only by
  `access`/`reactive`, which this path never touches).
- `sheet-sync/sync.sh` (entrypoint) — fetch `/api/actions` (SM token + real UA,
  via **urllib**, since the slim base has no `curl`) → `report` → `sheet-push -w
  <tab> -D <auto footer>`. Runs as the SA: ambient ADC covers the GCS snapshot
  read *and* the Sheets write (SA is Editor) — no key, no impersonation.
- `sheet-sync/build.sh` → Cloud Build (context = repo root, so it can copy
  `marin/`; Dockerfile under `sheet-sync/` → a `cloudbuild.yaml`, since
  `builds submit --tag` only finds a root Dockerfile).
- `sheet-sync/deploy.sh` — idempotent: `run jobs deploy` (upsert) +
  create-or-update Scheduler + the two IAM grants (`secretmanager.secretAccessor`
  on the token, `run.invoker` on the job so the Scheduler can trigger it).

## One-time setup — all done (2026-08-31)

1. ✅ Sheet shared (Editor) with the SA `gcs-usage-job@…`; Sheets+Drive APIs
   enabled in `oa-internal-450019`.
2. ✅ Read-only `gcs` grant minted (`id=j4wYCiDECdUG`, `email=NULL`), raw token
   in Secret Manager `gcs-sheet-sync-token`; the SA reads it via
   `secretmanager.secretAccessor`.
3. ⓘ Sheet **not** locked down (Percy stays Editor, by his choice) — the job
   rewrites only the one named tab in place, so derived tabs are safe. An "AUTO
   — regenerated hourly" footer disclaims the mirror.

## The reusable pattern (worth generalizing)

"Here's the www table — and pop it open as a recently-synced GSheet" is a nice,
reusable shape. It's already ~half-generic: `sheet-push` is table-agnostic (any
CSV), and `/users` already carries a **Google Sheet ↗** link. To lift the pattern
you need, per table: (1) a `report`-style CSV producer, (2) `sheet-push` on a
cron, (3) the "↗ GSheet" link on the page + a small "synced hourly" note for
freshness. Candidate to fold into the shared table/file-tree component later, so
any site table can declare a synced-sheet mirror. Out of scope for the first
instance (the mark-status nag list) — noted so we build this one in that
direction (keep `sheet-push` CSV-generic; keep the page link/note as the surface).

## Open questions

- **Sheet shape:** overwrite the whole first tab, or a dedicated "auto" tab so
  Percy can keep manual notes elsewhere? (Recommend a dedicated tab, full-replace,
  so we never clobber hand-edited cells.)
- **Columns:** match the `/users` CSV export exactly (user, group, keep, sweep,
  undecided, %, $/mo, page link)? Confirm against what Percy expects.
- **History:** keep only the latest, or append a dated snapshot tab per run?

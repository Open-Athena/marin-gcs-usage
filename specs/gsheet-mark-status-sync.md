# Auto-sync the per-user mark-status → Percy's Google Sheet

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

### Hourly trigger (turnkey once the sheet is shared)

Reuse the Batch/Cloud Build image; add a lightweight entrypoint (or a `run.sh
--sheet-only` flag) that does: export ledger from D1 → `report` → `sheet-push`.
Then:

```bash
# a Cloud Run *job* running the same image, sheet-only entrypoint
gcloud run jobs create gcs-sheet-sync --image <IMAGE> --region us-central1 \
  --service-account <SA> --set-env-vars SHEET_ID=1k_11LA…RFc,MODE=sheet-only
# hourly Scheduler → run the job
gcloud scheduler jobs create http gcs-sheet-sync-hourly --schedule "5 * * * *" \
  --uri "https://<region>-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/<PROJECT>/jobs/gcs-sheet-sync:run" \
  --oauth-service-account-email <SA> --location us-central1
```

(Mirrors the existing `gcs-usage-snapshot-daily` Scheduler → Batch wiring; the
`5 * * * *` offset just avoids top-of-hour contention.)

## Blocked on you (one-time)

1. **Share the sheet (Editor)** with the job's SA —
   `gcs-usage-job@oa-internal-450019.iam.gserviceaccount.com` (the daily-snapshot
   SA; reuse it for the hourly Cloud Run job) — and **enable the Sheets API** in
   `oa-internal-450019`.
2. **A `gcs`-scoped grant token** for the job to read `/api/actions` (mint a named
   `$oa/auth` grant, or quick-start with a personal `/api/token`); I'll stash it
   in Secret Manager and bind it to the job.
3. **Lock the sheet to read-only** — demote everyone (incl. Percy) to
   Commenter/Viewer; **keep the SA `gcs-usage-job@…` as Editor** (it writes). The
   job full-replaces the first tab in place; derivations are "Make a copy".

Everything else (CLI, image entrypoint, IaC commands) is ready to wire the moment
that's done.

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

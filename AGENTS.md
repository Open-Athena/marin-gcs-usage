# Marking GCS data for keep / sweep — CLI & API

This repo powers the **mark & sweep** cleanup of the `marin-*` GCS buckets: browse
usage at <https://gcs.oa.dev>, then mark prefixes you want to **keep** (everything
unmarked is swept — deleted — after the deadline). You can mark from the web UI,
but the CLI and HTTP API below let an **agent** do it autonomously: browse the
tree, check a prefix's status, and mark in bulk (including from a regex).

Nothing here deletes on the spot — marking only records a decision in the ledger;
the sweep runs later.

## 1. Get a token

Every write is authenticated as **you** by a personal bearer token.

- **Browser:** sign in at <https://gcs.oa.dev> with your `@openathena.ai` (or
  whitelisted) email, then reveal/mint your token from the user menu (top-right).
- **API:** `POST /api/token` from a signed-in browser session mints (or rotates)
  it; the raw token is shown **once**. `GET /api/token` reports status (never the
  token); `DELETE /api/token` revokes it.

The token carries only the `gcs` scope (least privilege — it can mark, but not
touch admin/cw routes), and re-checks your email on every request, so revoking it
or removing your access is instant.

```bash
export GCS_USAGE_TOKEN=…        # the token you copied
export GCS_USAGE_URL=https://gcs.oa.dev   # optional; this is the default
```

## 2. CLI

Install the `gcs-usage` CLI (Python ≥ 3.12):

```bash
pip install "git+https://github.com/Open-Athena/marin-gcs-usage.git#subdirectory=marin"
```

Prefixes are always `gs://marin-<bucket>/<dir>/…/` — **directory prefixes only**
(trailing slash), one of the six `marin-*` buckets.

### Check a prefix's status

```bash
gcs-usage status gs://marin-us-east5/checkpoints/my-run/
```

Prints the **effective** keep-state and owner of that path (marks are inherited
from ancestors; the most recent mark covering a prefix wins). `-j` for raw JSON.

### List what still needs a decision (the "todo" backlog)

```bash
gcs-usage todo                 # largest undecided prefixes first
gcs-usage todo -p | head       # bare prefixes, one per line (pipe into `mark`)
```

Options: `-n/--limit N`, `-f/--min-frac F` (ignore prefixes below F of total
bytes), `-j/--json`.

### Mark prefixes

```bash
# keep specific prefixes
gcs-usage mark gs://marin-us-east5/checkpoints/keep-me/ gs://marin-us-central2/data/gold/

# keep only the newest checkpoint under each run, sweep the older ones
gcs-usage mark -k keep_last_ckpt gs://marin-us-east5/checkpoints/my-run/

# explicitly sweep (delete) a prefix
gcs-usage mark -k sweep gs://marin-us-east5/scratch/

# from a file / stdin (one prefix per line) — e.g. straight from `todo`
gcs-usage todo -p | gcs-usage mark -k keep -f -
```

Keep actions (`-k`): `keep`, `keep_last_ckpt`, `sweep` (or `none` to leave the
keep axis untouched and only set ownership). Other options: `-o/--owner`
(`@me` by default — claims the prefix as yours; `""` leaves ownership untouched),
`-m/--memo` (note stored with every action), `-n/--dry-run` (print, send
nothing), `-t/--token`, `-u/--url`.

## 3. HTTP API

All under `https://gcs.oa.dev`. Reads are open to any signed-in viewer; writes
need `Authorization: Bearer $GCS_USAGE_TOKEN`.

| Method & path | Purpose |
|---|---|
| `GET /api/resolve?path=<gs://…/>` | Effective keep + owner of a path, with provenance (what the CLI `status` calls). |
| `GET /api/todo?limit=&min_frac=` | Largest prefixes still undecided (the review backlog). |
| `GET /api/actions` | The live ledger: `{ keeps: [...], owners: [...] }` — every expanded prefix joined to the raw action that set it. |
| `POST /api/actions` | Append one action, or an array. Body: `{ pattern, keep?, set_keep?, owner?, set_owner?, memo?, scan? }`. |
| `GET/POST/DELETE /api/token` | Manage your own token (browser SSO session only). |

`pattern` is a `gs://marin-<bucket>/<dir>/…/` prefix. `set_keep`/`set_owner`
default to "true if the corresponding field is present", so send `keep` to set the
keep axis and `owner` to set ownership; omit an axis to leave it unchanged. The
ledger keeps the full who-did-what trail — a later action re-paints deeper marks
it covers (recency beats specificity).

```bash
# keep a prefix via the raw API
curl -X POST https://gcs.oa.dev/api/actions \
  -H "Authorization: Bearer $GCS_USAGE_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"pattern":"gs://marin-us-east5/checkpoints/keep-me/","keep":"keep"}'
```

## Notes for agents

- **Marking is reversible until the sweep.** Re-mark or clear (`keep: null`) any
  time before the deadline; nothing is deleted at mark time.
- **Prefer the CLI** — it batches, validates prefixes client-side, and shares one
  token/URL resolution across `status` / `todo` / `mark`.
- **Browse first:** `gcs-usage todo` surfaces the biggest undecided prefixes;
  `status` confirms what a mark will actually affect (inheritance can mean a
  parent already decided it).

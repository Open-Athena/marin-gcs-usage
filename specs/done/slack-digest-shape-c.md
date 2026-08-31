# Shape C: monthly-thread `#gcs-usage` digest, driven by `thrds`

## Goal

Replace the current one-line daily digest with a **monthly thread** that reads
as a month-at-a-glance and drills into per-scan detail:

- **OP** (one per calendar month): a `:calendar:` headline, **per-week rollup
  bullets**, a dashboard link, month-to-date total, and a **daily-updating card
  image** (per-scan table + sparkline). OP + bullets + card re-converge on every
  scan.
- **Replies** (one per **scan** — gcs daily, cw 12-hourly): a Δ-arrow emoji +
  `[M/D](dashboard) — <TB> (Δ, Δ%) · <$/mo> (Δ$)`, with a breach glyph when a
  ceiling/spike fires.

One reply per *scan* (not per day): rolling scans up into a day message would add
an aggregation layer that hides the per-scan ground truth this project exists to
surface — negative ROI. The channel sees ~1 OP/month; the thread grows one reply
per scan.

The Shape C draft already exists in the `slck/gcs-digest` staging session
(`03-c-monthly-thread.md`) — this spec is about **productionizing** it: generating
the desired doc from the snapshot series and converging it every scan.

## Backend: `thrds`, always

`thrds` is the single posting backend (no parallel hand-rolled `chat.*`/image
plumbing). The job renders the *desired* thread doc and runs `slck push` to
converge Slack; the gist mirror is the only optional layer.

- **Auth**: the bot token the job already carries (`SLACK_BOT_TOKEN`). Shape C's
  headline-as-sender-name + custom avatar need a bot token — that's fine, and
  `thrds` errors clearly if a user token is used with sender overrides (see the
  thrds spec).
- **Gist mirror**: pushed when GitHub creds are present; `THRDS_NO_PUSH=1`
  otherwise. When on, the gist is a free version-history of the digest (the same
  audit-trail nicety as the GSheet sync), and the Slack↔gist metadata link
  (`event_type:"thrds"`) is built in.
- **Image block**: depends on the thrds **editable-image-blocks** feature
  (`~/c/thrds/specs/editable-image-blocks.md`). Until it lands, the OP carries a
  text placeholder where the card goes (graceful degradation).

## New work in this repo

### 1. `gcs-usage digest-doc` (new CLI command)

Emits/updates the Shape-C month thread doc from the snapshot series
(`snapshots/series.json` + per-scan meta), for the current month:

- OP: `:calendar: **GCS usage — <Month Year>**`, per-ISO-week rollup bullets
  (`wk of M/D: **±<ΔTB>** → <TB> · <$/mo> (Δ$)`, current week marked _(partial)_),
  dashboard link, month-to-date ΔTB, and an `![usage card](<card-url>)` image line.
- One reply block per scan in the month: `:arrow_degNN: [M/D](<dashboard>) —
  <TB> (Δ, Δ%) · <$/mo> (Δ$)` + `:rotating_light:` on breach. `NN` = the
  Δ%-per-scan mapped to the nearest `arrow_degNN` glyph (the `gen-delta-arrows.py`
  sweep; full-scale ±0.7%/scan).
- Writes into the `slck/gcs-digest` session (or a dedicated prod session) as the
  month's `NN-<YYYY-MM>.md`; converging is `slck push`.
- Idempotent: re-running for the same scan reproduces the same doc (thrds then
  no-ops). Month rollover starts a new thread doc; prior months stay as history.

### 2. Card render + host

- `job/gen-digest-card.py` already renders the per-day table + TB sparkline PNG
  (GitHub-dark, Menlo, 2× supersample). Feed it the month's per-scan series.
- **Host**: publish to the `gcs-usage-icons` Cloudflare Pages project
  (`gcs-usage-icons.pages.dev`, CORS-open via `job/icons/_headers`). The
  `wrangler pages deploy job/icons` step is currently **manual/out-of-band** —
  wire it into the job so the card refreshes each scan.
- **URL**: versioned `card-<YYYYMM>.png?v=<scan-ts>` so the image block swaps on
  each converge (thrds diffs the URL). Cache-bust owned here (caller-supplied
  version), matching the thrds spec's case-1.

### 3. Wire into `job/run.sh`

Replace the terminal `gcs-usage alert …` one-liner with: render card → deploy
Pages → `gcs-usage digest-doc` → `slck push`. Keep the same gating (only when a
Slack transport is configured; skipped on `REPROC`; never fails the snapshot).
The `fail_alert` ERR-trap path (hard-failure Slack ping) stays as-is — it's the
safety net for failures that happen *before* the digest step. The daily job image
already ships `gcs-usage`; add `thrds` (+ its Slack/gist deps) to the image, and
provide the bot token (have it) and, optionally, gist push creds.

## One-time setup

- **Custom emojis**: upload the `arrow_degNN` PNGs (`job/icons/arrows/`) to the
  workspace so the reply arrows render. (Automate via Slack admin API, or a
  documented manual step.)
- **Promote** the `slck/gcs-digest` session's Shape C from staging
  (`gcs-usage-staging`) to prod (`#gcs-usage`); retire the A/B drafts.
- Confirm the Pages deploy creds available to the job.

## Open questions

- **cw digest**: does the CloudWatch/S3 usage digest share the gcs monthly OP, get
  its own monthly thread, or a separate channel? (Leaning: its own thread/section
  — different fleet, different cadence.)
- **Gist creds in Batch**: SSH deploy key vs. token for `gist.github.com`; or run
  the digest/`slck push` from a lighter always-on runner (like the hourly
  sheet-sync Cloud Run job) rather than the heavy snapshot job, decoupling digest
  cadence from the snapshot.
- **Backfill**: seed the current month's thread from existing `series.json` on
  first run, or start fresh from the switchover scan.

## Dependencies

- `thrds` editable image blocks + bot-token sender-override guard
  (`~/c/thrds/specs/editable-image-blocks.md`) — land first, or ship with the
  text-placeholder OP until then.

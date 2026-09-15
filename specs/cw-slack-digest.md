# `#cw-s3-usage`: the Shape-C monthly digest for CoreWeave

## Goal

Mirror `#gcs-usage` for the CoreWeave bucket: a new Slack channel `#cw-s3-usage` carrying gcs's **Shape C** digest (`specs/done/slack-digest-shape-c.md` on the `gcs` branch) — one thread per calendar month whose OP (month-to-date headline, per-week rollup bullets, dashboard link, daily-updating plot) is edited in place, plus one reply per scan (headline as the sender name, colour-coded trend-arrow avatar). `thrds`'s `SlackClient` is the only posting backend, exactly as on gcs.

This is a **port**, not a redesign: `gcs-usage/src/gcs_usage/{digest,digest_plot}.py` + `tests/test_digest.py` come over to `marin/` with only the CoreWeave deltas below, so `/cp` parity stays cheap.

## Deltas vs gcs (each one forced by the data or the deployment)

1. **Data source.** gcs reads `snapshots/<YYYY-MM-DD>/meta.json` (daily). cw publishes `snapshots/cw/<YYYY-MM-DDTHHMM>/meta.json` (12-hourly, `0 */12 * * *`; earlier scans were ad hoc). There is no `series.json` on either — `load_month` walks the `meta.json`s; the scan-id regex admits the sub-daily suffix and the lead-in scan (for the first delta) is the last scan before the month.
2. **No dollars.** cw's `meta.json` has `class_bytes: {}` *by design* (`job/cw-webdata.py`: pricing CoreWeave with GCS list rates "would invent a number"; the site's store descriptor hides the cost panel). So the digest carries **no $/mo** — the second metric is **object count** (`total_objects`, which cw does publish): reply body `38.43M objects (+0.02M) [↗](…)`, weekly bullets `… · 38.43M objects (+0.12M)`. Same headline shape as gcs (`M/D — <TB> (Δ, Δ%)`), with the scan's UTC time appended (`9/15 00:01Z`) because two scans share a date. Sizes keep gcs's convention (bytes / 2^40 labelled "TB") so the two channels read alike.
3. **Cadence-agnostic time normalisation.** gcs hard-codes one scan/day (`deg(…, 7)` for a reply, `7/len(rows)` for month-to-date, `len(week) < 7` for *partial*). cw normalises by the actual clock: a reply's arrow projects its Δ% over the hours since the previous scan to a weekly rate (`168 / hours`), month-to-date uses elapsed days, and a week is *partial* while its Sunday has no scan yet. A daily feed gets the same numbers as gcs's arithmetic.
4. **Plot.** No storage tiers to stack, so the mosaic is total TiB (top; month frame + month-start reference line, as gcs) over object count (bottom). Title `CoreWeave usage — <Month Year>`, corner `cw-s3.oa.dev`.
5. **Hosting without touching gcs's alias.** gcs deploys `job/icons/` to the `gcs-usage-icons` Pages project's **production** branch every run — that root alias serves the trend-arrow avatars *both* digests use. cw deploys its plot dir (`job/icons-cw/`: the CORS `_headers` + the month's PNG) with `--branch cw`, a preview branch: the OP's image uses the deployment-specific URL (as gcs does), the production alias never changes, and the avatars stay `https://gcs-usage-icons.pages.dev/arrows/av_deg<N>.png?v=4`. Same Pages project, same Slack app (`GCS Usage Bot` — per-message sender overrides hide the app name), same bot-token secret; nothing new to provision.
6. **Converge state keyed by channel.** gcs keeps one prod thread's state at `gs://<bucket>/digest/<YYYY-MM>.json`. cw's lives at `gs://<bucket>/digest/cw/<channel>/<YYYY-MM>.json` — namespaced so it can't collide with gcs's, and keyed by channel so a staging converge (`gcs-usage-staging`) never masquerades as the prod thread.
7. **Deep links.** `https://cw-s3.oa.dev/?d=<yymmdd>-<hhmm>#diff` (the site's compact scan-prefix form, `site/src/scan.ts`); weekly bullets pin the week's end scan with a `-<N>d<M>h` look-back to the previous bullet's end.
8. **Discord.** gcs's module has a Discord twin. cw is Slack-only: the pure helpers (`emoji_name`, `discordify`) come over with their tests, the Discord converge/shell and `-P discord` do not (there's no `discord_api` module on this branch and no cw Discord destination was asked for).
9. **Wording.** "CoreWeave usage", bucket `marin-us-east-02a`, dashboard `https://cw-s3.oa.dev`.

The legacy one-liner `gcs-usage alert` stays (gcs kept it too); `job/cw-run.sh` never called it.

## Wiring

- `marin/src/gcs_usage/digest.py`, `digest_plot.py` (+ `[plot]` extra = matplotlib), `tests/test_digest.py` (exact-equality specs on a hand-built 12-hourly month).
- `gcs-usage digest` (cli.py): `-c/--channel` (`$SLACK_CHANNEL`), `-t/--token` (`$SLACK_BOT_TOKEN`), `-r/--root` (default `gs://$DATA_BUCKET/snapshots/cw`), `-m/--month`, `-u/--url`, `-D/--reply-delay`, `-n/--dry-run`.
- `marin/pyproject.toml`: `thrds` pinned to the same GitHub tarball SHA gcs runs in prod (`70548988…`, `py` branch); `plot = ["matplotlib>=3.8"]`; hatch `allow-direct-references`.
- `Dockerfile`: `pip install "./marin[plot]"` (wrangler is already in the image).
- `job/cw-run.sh`: after publish — `gcs-usage digest -r gs://$DATA/snapshots/cw`, only when `SLACK_BOT_TOKEN` + `SLACK_CHANNEL` are set, never fails the scan; `fail_alert` prefers `SLACK_ALERT_CHANNEL` (→ `#gcs-usage-alerts`, as gcs) over the digest channel.
- `job/cw-batch-submit.sh`: vars `SLACK_CHANNEL` (= `#cw-s3-usage`), `SLACK_ALERT_CHANNEL` (`C0BTUNT3B5Z`), `CLOUDFLARE_ACCOUNT_ID`; secretVariables `SLACK_BOT_TOKEN` ← `gcs-alert-slack-bot-token`, `CLOUDFLARE_API_TOKEN` ← `cf-pages-token` (both already readable by `gcs-usage-job`).
- `job/icons-cw/_headers`: the CORS header, as `job/icons/_headers` on gcs.

## One-time setup + gated steps (in order)

1. Slack: create `#cw-s3-usage` (public), invite `GCS Usage Bot`. *(done in this pass; see the closed spec for ids)*
2. Stage: converge the current month into `gcs-usage-staging` (`C0BSV6ETHT4`) from real cw scans; eyeball it. *(done in this pass)*
3. **Rebuild `IMAGE:cw`** (`job/build.sh`) so the image carries `thrds` + matplotlib + the `digest` command.
4. **Edit the `cw-usage-snapshot` Cloud Scheduler body** (user-owned; not done by the agent) — add the vars + secretVariables from *Wiring* (`DRY=1 job/cw-batch-submit.sh` prints the full spec).
5. **First prod converge** — backfills the month into `#cw-s3-usage` with spaced replies: `SLACK_CHANNEL=<id> gcs-usage digest -r gs://oa-gcs-usage-dvx/snapshots/cw -D 305`.

## Open

- Backfill depth: the first converge seeds the *current* month from every scan already published (the spec's default); prior months are not posted.
- Reply spacing on backfill: Slack collapses consecutive same-sender chrome inside ~5 min, hence `-D 305` for prod; the staging preview used a short delay and accepts the collapsed look.

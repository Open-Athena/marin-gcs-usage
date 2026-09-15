# `#cw-s3-usage`: a monthly digest thread for CoreWeave

## Goal

A Slack channel `#cw-s3-usage` (created 2026-09-15: `C0C1YR7D0KU`) mirroring `#gcs-usage`'s **Shape C** shape (`specs/done/slack-digest-shape-c.md` on the `gcs` branch): one thread per calendar month whose OP is edited in place as scans land, plus one reply per scan under its own sender name + trend-arrow avatar, converged by `thrds`'s `SlackClient`.

This lands in two parts:

1. **Mechanism** (this pass, done): port gcs's converge machinery to the cw-s3 branch against the CoreWeave data — monthly-thread state, plot render + host, the job wiring — with the message text as a clean seam.
2. **Content** (workshop, open): what the headline and replies should *say*. CoreWeave has no storage classes and no dollar figures, so gcs's text doesn't transfer; the raw material and three candidate framings are in *Content — TBD* below. Only `op_body` / `reply` in `marin/src/gcs_usage/digest.py` change when it's decided.

## A separate Slack app

Per-message sender overrides (`username` / `icon_url`) don't hide the underlying **app** name in Slack's Unread / Threads views, so a GCS-named app posting CoreWeave numbers would be actively misleading. `#cw-s3-usage` gets its own app. Apps can't be created through the Web API without a config token, so this is a one-time manual step:

- Manifest: `job/slack/cw-usage-bot.manifest.json` — display name **CoreWeave Usage Bot** (spelled out rather than "CW", since the app name is exactly what's visible in those views), a bot user, and the scopes thrds's `SlackClient` needs: `chat:write` + `chat:write.customize` (the per-message sender/avatar), `channels:history` / `groups:history` / `channels:read` / `groups:read` / `users:read` / `emoji:read` (its `conversations.history|replies|info|list`, `users.info`, `emoji.list` calls), plus `chat:write.public` so it can post to a public channel without an `/invite`.
- Setup: api.slack.com/apps → *Create New App* → *From a manifest* → paste → install to the workspace → copy the Bot User OAuth Token (`xoxb-…`) → `printf %s "$TOKEN" | gcloud secrets create cw-s3-slack-bot-token --data-file=-` (named like the existing `cw-s3-access-key-id` / `cw-s3-secret-access-key` pair; grant `gcs-usage-job@…` `secretmanager.secretAccessor` on it) → optionally `/invite @CoreWeave Usage Bot` in `#cw-s3-usage` (not required with `chat:write.public`) → upload an app icon by hand (*Basic Information → App icon*; no API for it).

## Mechanism (done)

Ported from gcs's `digest.py` with these CoreWeave deltas (each forced by the data or the deployment):

1. **Data source.** gcs reads `snapshots/<YYYY-MM-DD>/meta.json` (daily). cw publishes `snapshots/cw/<YYYY-MM-DDTHHMM>/meta.json` (12-hourly since 9/2; see the inventory). No `series.json` on either — `load_month` walks the `meta.json`s; the scan-id regex admits the sub-daily suffix, ids sort chronologically, and the lead-in scan (for the first delta) is the last scan before the month.
2. **No dollars.** cw's `meta.json` has `class_bytes: {}` *by design* (`job/cw-webdata.py`: pricing CoreWeave with GCS list rates "would invent a number"; `App.tsx` sets `pricing = null` whenever `class_bytes` is empty — verified on this branch). `Scan` rows carry TiB + `total_objects` (+ deltas + hours since the prior scan); nothing is priced.
3. **Clock-normalised arrows.** gcs hard-codes one scan/day (`deg(…, 7)`). cw projects a scan's Δ% over the actual hours since the previous scan to a weekly rate (`168 / hours` — 14 for the 12-hourly feed, 7 for a daily one, so a daily feed reproduces gcs's numbers exactly).
4. **Plot.** No tiers to stack, so the 2-panel mosaic is total TiB (top; month frame + month-start reference line, as gcs) over object count (bottom). Title `CoreWeave usage — <Month Year>`, corner `cw-s3.oa.dev`. (Framing A below adds a quota line.)
5. **Hosting without touching gcs's alias.** gcs deploys `job/icons/` to the `gcs-usage-icons` Pages project's **production** branch every run — that root alias serves the trend-arrow avatars *both* digests use. cw deploys its own dir (`job/icons-cw/`: the CORS `_headers` + the month's PNG) with `--branch cw`, a preview branch: the OP's image uses the deployment-specific URL (as gcs does), the production alias never changes, and the avatars stay `https://gcs-usage-icons.pages.dev/arrows/av_deg<N>.png?v=4`. Same Pages project, nothing new to provision.
6. **Converge state keyed by channel.** gcs keeps one prod thread's state at `gs://<bucket>/digest/<YYYY-MM>.json`. cw's lives at `gs://<bucket>/digest/cw/<channel>/<YYYY-MM>.json` — namespaced so it can't collide with gcs's, keyed by channel so a staging converge never masquerades as the prod thread. `posted` is keyed by scan id.
7. **Deep links.** `https://cw-s3.oa.dev/?d=<yymmdd>-<hhmm>#diff` — the site's compact scan-prefix token (`site/src/scan.ts`); `_span` renders its `-<N>d<M>h` look-back for scan-pair links.
8. **Slack only.** gcs's Discord twin (and its `discordify`/emoji helpers) isn't ported — no cw Discord destination was asked for, and there's no `discord_api` module on this branch.
9. **Retired** the pre-Shape-C one-liner `gcs-usage alert` (+ its `_snapshot_dates` helper) from `cli.py`; `job/cw-run.sh` never called it.

Wiring:

- `marin/src/gcs_usage/digest.py` (mechanism + placeholder content), `digest_plot.py` (`[plot]` extra = matplotlib), `tests/test_digest.py` (exact-equality specs on the helpers + the converge against a fake client over a local snapshot tree).
- `gcs-usage digest` (cli.py): `-c/--channel` (`$SLACK_CHANNEL`), `-t/--token` (`$SLACK_BOT_TOKEN`), `-r/--root` (default `gs://$DATA_BUCKET/snapshots/cw`), `-m/--month`, `-u/--url`, `-D/--reply-delay`, `-n/--dry-run` (renders the plot + prints the OP/replies, posts nothing).
- `marin/pyproject.toml`: `thrds` pinned to the GitHub tarball SHA gcs runs in prod (`70548988…`, `py` branch); `plot = ["matplotlib>=3.8"]`; hatch `allow-direct-references`.
- `Dockerfile`: `pip install "./marin[plot]"` (wrangler is already in the image).
- `job/cw-run.sh`: after publish — `gcs-usage digest -r gs://$DATA/snapshots/cw`, only when `SLACK_BOT_TOKEN` + `SLACK_CHANNEL` are set, never fails the scan; `fail_alert` prefers `SLACK_ALERT_CHANNEL` (→ `#gcs-usage-alerts`, as gcs) over the digest channel.
- `job/cw-batch-submit.sh`: vars `SLACK_CHANNEL` = `C0C1YR7D0KU`, `SLACK_ALERT_CHANNEL` = `C0BTUNT3B5Z`, `CLOUDFLARE_ACCOUNT_ID`; secretVariables `SLACK_BOT_TOKEN` ← **`cw-s3-slack-bot-token`** (the new app), `CLOUDFLARE_API_TOKEN` ← `cf-pages-token`.
- `job/icons-cw/_headers`: the CORS header, as `job/icons/_headers` on gcs.

## Content — TBD (workshop)

Nothing has been posted anywhere (no staging preview either) — the text is decided first. The placeholders today: OP = `**<TB> TB** · <N>M objects · [dashboard]` + plot; reply sender = `M/D HH:MMZ — <TB> TB (Δ, Δ%)`, body = `<N>M objects (Δ) [↗](diff link)`, avatar = the clock-normalised trend arrow.

### Ryan's candidate framings (verbatim)

- **(A) quota-headroom headline** — `TiB (Δ, Δ%) · NN% of quota · X TiB free` + month plot with the quota line
- **(B) movers body** — top ±3 top-level prefixes by Δ, link to the dashboard diff view for that scan pair
- **(C) sweep ledger** — Δ split into sweeps-reclaimed vs organic growth + versioning-pending bytes with their expiry date, month-to-date reclaimed in the OP

### What a cw scan gives us (real September snapshots, `gs://oa-gcs-usage-dvx/snapshots/cw/`)

Per scan, four files (`job/cw-webdata.py` + `job/cw-diff.py`):

- `meta.json` — `{asof, generated, total_bytes, total_objects, class_bytes: {}}`. The two totals are the only per-scan scalars; `class_bytes` is always empty.
- `tree.json` — the size tree, `{n, b, o, d, c}` per node (name, bytes, objects, bytes-weighted mean mtime in epoch days, children), pruned at 0.02 % of total bytes → **8 top-level prefixes** under the bucket node. Per-prefix bytes/objects (and Δ vs any other scan's tree) come from here.
- `age.json` — 342 rows `{d, d1, b, o}`: bytes + objects per (created day, top-level dir). Age/mtime buckets ("older than N days", per prefix) are derivable per scan.
- `diff.json` — the **precomputed Δ vs the previous scan**: `{prev, curr, total_a, total_b, objects_a, objects_b, rows[500] {p, d, k, s, a, b, oa, ob, x}, expansions, truncated}` — per-directory before/after bytes + objects, status (`changed`/`added`/`removed`), depth. Framing B's "top movers for that scan pair" already exists here (500 rows, truncated — the top-level Δs are always present).

Across scans (derivable): Δ per prefix between any two trees; month-to-date and weekly rollups from the `meta.json` series; per-scan movers from `diff.json`. **Not** in the scans: sweep attribution (which Δ was a deletion run vs organic churn — that's the `deletion_runs` D1 table + run summaries in `gs://…/cw-sweep/`, joinable by scan id), and anything about object versions.

**Quota.** `910 TiB` appears only in *comments* (`site/src/types.ts:112`, `site/src/units.tsx:7`) and in session memory (`cw-zones-and-buckets`: the whole quota sits in `us-east-02a`) — there is **no constant** anywhere the digest could read. Framing A needs one; the honest home is a `QUOTA_TIB` in `digest.py` (or `meta.json` from the job) with the number confirmed against CoreWeave's console/contract.

**Versioning (enabled 2026-09-15).** Deletes now leave a delete marker + a noncurrent version; those bytes are *pending reclaim* until the lifecycle rule expires them (acceptance test in flight — `specs/cw-sweep.md`). None of this is in the scan (`bulk-list` lists current objects): `list_object_versions` per swept prefix could supply noncurrent bytes + marker counts, and with `NoncurrentDays=N` the expiry date is `noncurrent-since + N`. That's framing C's second half; it's a new data path, not a reshaping of existing JSON.

### Inventory tables (generated 2026-09-15 by `tmp/cw-inv/inventory.py`)

#### Cadence + totals (September 2026, with the last August scan as lead-in)

| scan (UTC) | TiB | % of 910 TiB | objects | Δ TiB | Δ objects | h since prior |
|---|---:|---:|---:|---:|---:|---:|
| 2026-08-31T1201 | 761.5 | 83.7% | 25,143,866 | — | — | — |
| 2026-09-01T1201 | 773.4 | 85.0% | 27,026,681 | +11.9 | +1,882,815 | 24.0 |
| 2026-09-02T0002 | 779.0 | 85.6% | 27,766,363 | +5.6 | +739,682 | 12.0 |
| 2026-09-02T1202 | 778.1 | 85.5% | 28,655,303 | -0.9 | +888,940 | 12.0 |
| 2026-09-03T0007 | 799.5 | 87.9% | 30,872,597 | +21.4 | +2,217,294 | 12.1 |
| 2026-09-03T1201 | 802.1 | 88.1% | 31,229,123 | +2.6 | +356,526 | 11.9 |
| 2026-09-04T0002 | 742.2 | 81.6% | 31,662,867 | -59.8 | +433,744 | 12.0 |
| 2026-09-04T1201 | 745.9 | 82.0% | 31,584,338 | +3.7 | -78,529 | 12.0 |
| 2026-09-05T0001 | 756.2 | 83.1% | 32,373,622 | +10.3 | +789,284 | 12.0 |
| 2026-09-05T1201 | 756.0 | 83.1% | 32,367,069 | -0.2 | -6,553 | 12.0 |
| 2026-09-06T0001 | 767.3 | 84.3% | 33,164,516 | +11.2 | +797,447 | 12.0 |
| 2026-09-06T1201 | 773.3 | 85.0% | 33,761,969 | +6.0 | +597,453 | 12.0 |
| 2026-09-07T0001 | 777.1 | 85.4% | 34,288,679 | +3.8 | +526,710 | 12.0 |
| 2026-09-07T1201 | 777.1 | 85.4% | 34,142,374 | +0.0 | -146,305 | 12.0 |
| 2026-09-08T0001 | 777.2 | 85.4% | 34,147,442 | +0.0 | +5,068 | 12.0 |
| 2026-09-08T1201 | 777.1 | 85.4% | 33,900,483 | -0.1 | -246,959 | 12.0 |
| 2026-09-09T0000 | 782.0 | 85.9% | 34,012,596 | +5.0 | +112,113 | 12.0 |
| 2026-09-09T1201 | 782.7 | 86.0% | 33,917,054 | +0.7 | -95,542 | 12.0 |
| 2026-09-10T0001 | 783.5 | 86.1% | 34,257,933 | +0.8 | +340,879 | 12.0 |
| 2026-09-10T1201 | 787.6 | 86.6% | 34,211,470 | +4.1 | -46,463 | 12.0 |
| 2026-09-11T0001 | 788.4 | 86.6% | 34,416,488 | +0.8 | +205,018 | 12.0 |
| 2026-09-11T1201 | 786.5 | 86.4% | 34,525,397 | -1.9 | +108,909 | 12.0 |
| 2026-09-12T0001 | 792.7 | 87.1% | 34,904,647 | +6.2 | +379,250 | 12.0 |
| 2026-09-12T1201 | 803.2 | 88.3% | 35,052,396 | +10.5 | +147,749 | 12.0 |
| 2026-09-13T0001 | 808.5 | 88.8% | 35,500,844 | +5.3 | +448,448 | 12.0 |
| 2026-09-13T1201 | 812.8 | 89.3% | 35,938,142 | +4.3 | +437,298 | 12.0 |
| 2026-09-14T0001 | 814.2 | 89.5% | 36,512,382 | +1.3 | +574,240 | 12.0 |
| 2026-09-14T1201 | 845.9 | 93.0% | 37,500,686 | +31.8 | +988,304 | 12.0 |
| 2026-09-15T0001 | 839.5 | 92.2% | 38,428,773 | -6.5 | +928,087 | 12.0 |

month-to-date: +78.0 TiB, +13,284,907 objects (2026-08-31T1201 → 2026-09-15T0001); latest = 839.5 TiB = 92.2% of quota, 70.5 TiB free
meta.json keys: ['asof', 'class_bytes', 'generated', 'total_bytes', 'total_objects']; class_bytes = {} (empty by design)

#### Top-level prefixes — 2026-09-15T0001 vs 2026-09-14T1201 (tree.json: bucket `marin-us-east-02a`, 8 children ≥ 0.02% of bytes; `Marin CoreWeave` root)

| prefix | TiB | % of bucket | objects | Δ TiB vs prior | Δ objects |
|---|---:|---:|---:|---:|---:|
| `marin` | 678.3 | 80.8% | 13,813,912 | -7.29 | +107,666 |
| `tmp` | 110.9 | 13.2% | 16,077,789 | +0.52 | +546,293 |
| `iris` | 34.7 | 4.1% | 6,966,725 | +0.01 | +582 |
| `MarinFold` | 5.7 | 0.7% | 1,271,583 | +0.10 | +207,070 |
| `protein-structure` | 5.0 | 0.6% | 48,466 | +0.09 | +6,676 |
| `MarinDNA` | 2.4 | 0.3% | 28,552 | +0.00 | +0 |
| `users` | 1.5 | 0.2% | 195,882 | +0.00 | +59,297 |
| `models` | 0.8 | 0.1% | 331 | +0.12 | +51 |

prefixes in prior but not latest: []
node keys: ['b', 'c', 'd', 'n', 'o'] (n=name, b=bytes, o=objects, d=bytes-weighted mean mtime in epoch days, c=children)
biggest ± movers vs prior scan: ['marin -7.29 TiB', 'MarinDNA +0.00 TiB', 'users +0.00 TiB'] ['MarinFold +0.10 TiB', 'models +0.12 TiB', 'tmp +0.52 TiB']

#### age.json (2026-09-15T0001)

list[342], first = {"d": 20631, "d1": "marin", "b": 17330764, "o": 12}

## Gated follow-ups (in order)

1. **Slack app** (Ryan): create from the manifest, install, `cw-s3-slack-bot-token` in Secret Manager + accessor grant to `gcs-usage-job`, app icon.
2. **Content decision** (workshop) → implement `op_body`/`reply` + tests; then a staging preview into `gcs-usage-staging` (`C0BSV6ETHT4`) with `-c C0BSV6ETHT4` (its own state file), before prod.
3. **Rebuild `IMAGE:cw`** (`job/build.sh`) — the image needs `thrds` + matplotlib + the `digest` command.
4. **Edit the `cw-usage-snapshot` Cloud Scheduler body** (user-owned): add the vars + secretVariables from *Wiring* (`DRY=1 job/cw-batch-submit.sh` prints the full spec).
5. **First prod converge** — backfills the month into `#cw-s3-usage` with spaced replies: `gcs-usage digest -r gs://oa-gcs-usage-dvx/snapshots/cw -D 305` (Slack collapses consecutive same-sender chrome inside ~5 min).

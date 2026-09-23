# `#cw-s3-usage`: a monthly digest thread for CoreWeave

## Goal

A Slack channel `#cw-s3-usage` (created 2026-09-15: `C0C1YR7D0KU`) mirroring `#gcs-usage`'s **Shape C** shape (`specs/done/slack-digest-shape-c.md` on the `gcs` branch): one thread per calendar month whose OP is edited in place as scans land, plus one reply per scan under its own sender name + trend-arrow avatar, converged by `thrds`'s `SlackClient`.

This landed in two parts:

1. **Mechanism** (done): port gcs's converge machinery to the cw-s3 branch against the CoreWeave data — monthly-thread state, plot render + host, the job wiring.
2. **Content** (decided 2026-09-15, framing A — *quota headroom*; implemented, A/B-staged): CoreWeave has no storage classes and no dollar figures, so gcs's text doesn't transfer. The chosen text, the one open tradeoff (two reply variants, staged side by side), and the data inventory that fed the decision are under *Content* below.

## A separate Slack app

Per-message sender overrides (`username` / `icon_url`) don't hide the underlying **app** name in Slack's Unread / Threads views, so a GCS-named app posting CoreWeave numbers would be actively misleading. `#cw-s3-usage` gets its own app. Apps can't be created through the Web API without a config token, so this is a one-time manual step:

- Manifest: `job/slack/cw-usage-bot.manifest.json` — display name **CoreWeave Usage Bot** (spelled out rather than "CW", since the app name is exactly what's visible in those views), a bot user, and the scopes thrds's `SlackClient` needs: `chat:write` + `chat:write.customize` (the per-message sender/avatar), `channels:history` / `groups:history` / `channels:read` / `groups:read` / `users:read` / `emoji:read` (its `conversations.history|replies|info|list`, `users.info`, `emoji.list` calls), plus `chat:write.public` so it can post to a public channel without an `/invite`.
- Setup: api.slack.com/apps → *Create New App* → *From a manifest* → paste → install to the workspace → copy the Bot User OAuth Token (`xoxb-…`) → `printf %s "$TOKEN" | gcloud secrets create cw-s3-slack-bot-token --data-file=-` (named like the existing `cw-s3-access-key-id` / `cw-s3-secret-access-key` pair; grant `gcs-usage-job@…` `secretmanager.secretAccessor` on it) → optionally `/invite @CoreWeave Usage Bot` in `#cw-s3-usage` (not required with `chat:write.public`) → upload an app icon by hand (*Basic Information → App icon*; no API for it).

## Mechanism (done)

Ported from gcs's `digest.py` with these CoreWeave deltas (each forced by the data or the deployment):

1. **Data source.** gcs reads `snapshots/<YYYY-MM-DD>/meta.json` (daily). cw publishes `snapshots/cw/<YYYY-MM-DDTHHMM>/meta.json` (12-hourly since 9/2; see the inventory). No `series.json` on either — `load_month` walks the `meta.json`s; the scan-id regex admits the sub-daily suffix, ids sort chronologically, and the lead-in scan (for the first delta) is the last scan before the month.
2. **No dollars.** cw's `meta.json` has `class_bytes: {}` *by design* (`job/cw-webdata.py`: pricing CoreWeave with GCS list rates "would invent a number"; `App.tsx` sets `pricing = null` whenever `class_bytes` is empty — verified on this branch). `Scan` rows carry TiB + `total_objects` (+ deltas + hours since the prior scan); nothing is priced. Headroom is against the quota instead (see *Content*).
3. **Clock-normalised arrows.** gcs hard-codes one scan/day (`deg(…, 7)`). cw projects a delta's % over the actual hours it spans to a weekly rate (`168 / hours` — 7 for a clean 24 h day, 14 for a 12 h half-day), so the arrows mean the same rate whatever the interval.
4. **Plot.** The month's sparkline of every scan (12-hourly points), single panel, y-axis pinned to the quota: the 1 PB line, the used fill under the line, a hatched headroom band above it, a dashed month-start reference. Title `CoreWeave usage — <Month Year>`, corner `cw-s3.oa.dev · TiB`.
4a. **Days, not scans.** gcs replies once per scan (= per day). cw scans twice a day, so replies are keyed by UTC **day** (`day_rows`), with the day's representative scan chosen per variant (below); the OP and plot still re-converge on every scan.
5. **Hosting without touching gcs's alias.** gcs deploys `job/icons/` to the `gcs-usage-icons` Pages project's **production** branch every run — that root alias serves the trend-arrow avatars *both* digests use. cw deploys its own dir (`job/icons-cw/`: the CORS `_headers` + the month's PNG) with `--branch cw`, a preview branch: the OP's image uses the deployment-specific URL (as gcs does), the production alias never changes, and the avatars stay `https://gcs-usage-icons.pages.dev/arrows/av_deg<N>.png?v=4`. Same Pages project, nothing new to provision.
6. **Converge state keyed by channel + variant.** gcs keeps one prod thread's state at `gs://<bucket>/digest/<YYYY-MM>.json`. cw's lives at `gs://<bucket>/digest/cw/<channel>/<variant>/<YYYY-MM>.json` — namespaced so it can't collide with gcs's, keyed by channel so a staging converge never masquerades as the prod thread, and by variant so the A/B threads coexist. `posted` is keyed by UTC day → `{ts, scan}` (the scan the reply currently reflects).
7. **Deep links.** `https://cw-s3.oa.dev/?d=<yymmdd>-<hhmm>#over-time` — the site's compact scan-prefix token (`site/src/scan.ts`); `_span` renders its `-<N>d<M>h` look-back for scan-pair links.
8. **Slack only.** gcs's Discord twin (and its `discordify`/emoji helpers) isn't ported — no cw Discord destination was asked for, and there's no `discord_api` module on this branch.
9. **Retired** the pre-Shape-C one-liner `gcs-usage alert` (+ its `_snapshot_dates` helper) from `cli.py`; `job/cw-run.sh` never called it.

Wiring:

- `marin/src/gcs_usage/digest.py` (mechanism + framing-A content), `digest_plot.py` (`[plot]` extra = matplotlib), `tests/test_digest.py` (exact-equality specs on the helpers, the rendered OP and both reply variants, the daily keying rule, and the converge against a fake client over a local snapshot tree).
- `gcs-usage digest` (cli.py): `-c/--channel` (`$SLACK_CHANNEL`), `-t/--token` (`$SLACK_BOT_TOKEN`), `-r/--root` (default `gs://$DATA_BUCKET/snapshots/cw`), `-m/--month`, `-u/--url`, `-V/--variant` (`sender`|`body`, default `sender`), `-i/--icons-dir`, `-D/--reply-delay`, `-n/--dry-run` (renders the plot + prints the OP/replies, posts nothing).
- `marin/pyproject.toml`: `thrds` pinned to the GitHub tarball SHA gcs runs in prod (`70548988…`, `py` branch); `plot = ["matplotlib>=3.8"]`; hatch `allow-direct-references`.
- `Dockerfile`: `pip install "./marin[plot]"` (wrangler is already in the image).
- `job/cw-run.sh`: after publish — `gcs-usage digest -r gs://$DATA/snapshots/cw`, only when `SLACK_BOT_TOKEN` + `SLACK_CHANNEL` are set, never fails the scan; `fail_alert` prefers `SLACK_ALERT_CHANNEL` (→ `#gcs-usage-alerts`, as gcs) over the digest channel.
- `job/cw-batch-submit.sh`: vars `SLACK_CHANNEL` = `C0C1YR7D0KU`, `SLACK_ALERT_CHANNEL` = `C0BTUNT3B5Z`, `CLOUDFLARE_ACCOUNT_ID`; secretVariables `SLACK_BOT_TOKEN` ← **`cw-s3-slack-bot-token`** (the new app), `CLOUDFLARE_API_TOKEN` ← `cf-pages-token`.
- `job/icons-cw/_headers`: the CORS header, as `job/icons/_headers` on gcs.

## Content

### Decision (2026-09-15): framing A — quota headroom

Of the three candidates —

- **(A) quota-headroom headline** — `TiB (Δ, Δ%) · NN% of quota · X TiB free` + month plot with the quota line
- **(B) movers body** — top ±3 top-level prefixes by Δ, link to the dashboard diff view for that scan pair
- **(C) sweep ledger** — Δ split into sweeps-reclaimed vs organic growth + versioning-pending bytes with their expiry date, month-to-date reclaimed in the OP

— **A** is the digest: the sparkline relative to the quota, daily replies, no burn-rate line ("days until full" was rejected: a single day is too random to extrapolate). B's movers already exist per scan in `diff.json` and C needs the sweep + versioning data paths; both stay available as later additions.

**Quota.** 1 PB *decimal* = `10^15` bytes = 909.49 TiB (the "910 TiB" in the site's comments and the zones memo is this rounded). Owned once: `QUOTA_BYTES = 10**15` in `digest.py`; headroom renders as `NN.N% of 1 PB` and `<free> TiB free`. Sizes are TiB throughout (the site's default unit; quotas are binary).

**OP** — sender `CoreWeave usage — <Month YYYY>` with the `:calendar:` icon; body:

```
:arrow_degNN: **±Δ TiB** [month-to-date](https://cw-s3.oa.dev/?d=<latest>-<span since lead-in>#over-time) · <TiB> TiB · NN.N% of 1 PB · [dashboard](https://cw-s3.oa.dev/)

*Weekly summaries*
:arrow_degNN: [wk of M/D](diff link over the week): **±Δ TiB** → <TiB> TiB · NN.N% of 1 PB
:arrow_degNN: [wk of M/D](…) _(partial)_: …          ← the current week, until its Sunday has a scan

![CoreWeave usage — <Month YYYY>](<sparkline>)
```

Weeks are ISO (Monday-keyed); a bullet's Δ is its last scan vs the previous bullet's last scan (the first vs the month's baseline = the last pre-month scan), and its link opens the dashboard's Diff section over exactly that span (`?d=<end>-<N>d<M>h#over-time`). Month-to-date is the latest scan vs the same baseline; its arrow is the month's rate projected to a week. The image is the quota sparkline described under *Mechanism* §4.

**Replies — one per UTC day**, `M/D — <TiB> TiB (Δ, Δ%) · NN.N% of 1 PB · <free> TiB free`, Δ over the prior day's reply scan (so a clean 24 h in steady state), the arrow normalised by that interval, the day linked to its 24 h diff on the dashboard. The one real tradeoff — Slack fixes a message's `username` + icon at post time, `chat.update` can't change them ([[gcs-usage-slack-alerting]]) — gives two variants, both implemented (`-V`) and staged side by side:

| | **`sender`** (gcs-style) | **`body`** |
|---|---|---|
| headline | the sender name: `9/15 — 840 TiB (+25.3, 3.1%)` | bold in the body under a static `CoreWeave usage` / `:calendar:` sender |
| arrow | the avatar (`av_deg<N>.png`) | the leading `:arrow_degN:` emoji |
| body | `92.3% of 1 PB · 70.0 TiB free [↗](diff)` | `:arrow_deg60: [9/15](diff) — **840 TiB (+25.3, 3.1%)** · 92.3% of 1 PB · 70.0 TiB free` |
| day's scan | the **morning** scan — the first at/after `REPLY_HOUR_UTC` = 12 (12:01Z = 8:01 am ET, same calendar date on both coasts; `-H` overrides) — posted once, ~24 h Δ to the prior day's reply scan; the 00:01Z scan only re-converges the OP + plot. If the morning scan is missed, the day's last scan stands in once the next day has started, so it's still exactly one reply per day. (Rule changed 2026-09-16: 00:01Z posts landed at 8 pm ET the evening before, dated tomorrow.) | the **latest** — the reply is **edited** as the day's scans land, so text and sparkline agree intra-day (the 00:01Z post's Δ spans 12 h until the 12:01Z edit makes it 24 h) |
| per-reply chrome | distinct sender per day → every reply carries its own header | same sender → consecutive replies within ~5 min collapse into one block (a backfill; not a concern at one reply per day) |

**Rule changes** re-thread with `gcs-usage digest --redo-replies` (dry-run plan by default, `-F/--for-real` applies): the new replies post first under the current rule, then the old ones are `chat.delete`d — no empty-thread window, and no strike/edit step since the headline is in the sender name (`chat.update` can't change it). The old ts are stashed as `stale` in the state until deleted; a failed post stops before any delete, a failed delete is left for a re-run.

Staging (2026-09-15): both variants converged from the real September scans into `gcs-usage-staging` (`C0BSV6ETHT4`) with the GCS bot token — fine for staging, the app-name concern only bites in prod. Permalinks in the session report; state under `digest/cw/C0BSV6ETHT4/{sender,body}/2026-09.json`, plots on the `cw` branch of the icons project.

### What a cw scan gives us (real September snapshots, `gs://oa-gcs-usage-dvx/snapshots/cw/`)

Per scan, four files (`job/cw-webdata.py` + `job/cw-diff.py`):

- `meta.json` — `{asof, generated, total_bytes, total_objects, class_bytes: {}}`. The two totals are the only per-scan scalars; `class_bytes` is always empty.
- `tree.json` — the size tree, `{n, b, o, d, c}` per node (name, bytes, objects, bytes-weighted mean mtime in epoch days, children), pruned at 0.02 % of total bytes → **8 top-level prefixes** under the bucket node. Per-prefix bytes/objects (and Δ vs any other scan's tree) come from here.
- `age.json` — 342 rows `{d, d1, b, o}`: bytes + objects per (created day, top-level dir). Age/mtime buckets ("older than N days", per prefix) are derivable per scan.
- `diff.json` — the **precomputed Δ vs the previous scan**: `{prev, curr, total_a, total_b, objects_a, objects_b, rows[500] {p, d, k, s, a, b, oa, ob, x}, expansions, truncated}` — per-directory before/after bytes + objects, status (`changed`/`added`/`removed`), depth. Framing B's "top movers for that scan pair" already exists here (500 rows, truncated — the top-level Δs are always present).

Across scans (derivable): Δ per prefix between any two trees; month-to-date and weekly rollups from the `meta.json` series; per-scan movers from `diff.json`. **Not** in the scans: sweep attribution (which Δ was a deletion run vs organic churn — that's the `deletion_runs` D1 table + run summaries in `gs://…/cw-sweep/`, joinable by scan id), and anything about object versions.

**Quota.** `910 TiB` appeared only in *comments* (`site/src/types.ts:112`, `site/src/units.tsx:7`) and in session memory (`cw-zones-and-buckets`: the whole quota sits in `us-east-02a`) — there was **no constant** anywhere the digest could read. Resolved: it is and always has been 1 PB decimal; `QUOTA_BYTES = 10**15` in `digest.py` owns it now.

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

## Go-live record (2026-09-15 → 16)

All five gates cleared, in order:

1. **Slack app** — "CoreWeave Usage Bot" created from the manifest (bot `B0C2YS423KJ`, user `coreweave_usage_bot`); the manifest's top-level `_comment` had to go first (Slack rejects unknown top-level keys — notes now in `job/slack/README.md`). Token stored as Secret Manager `cw-s3-slack-bot-token` with the accessor grant to `gcs-usage-job@`; locally in `wt/cw-s3/.envrc` as `CW_USAGE_SLACK_BOT_TOKEN`.
2. **Reply variant** — v1 `sender` chosen from the two staged threads (`gcs-usage-staging`: `p1789511503660909` sender vs `p1789511536796279` body); it was already the `-V` default. The `body` variant stays in the code, dormant.
3. **`IMAGE:cw` rebuilt** with thrds + matplotlib + `gcs-usage digest` (`sha256:2b286760…`).
4. **Scheduler body** updated (`userUpdateTime 2026-09-16T01:19Z`): `SLACK_CHANNEL=C0C1YR7D0KU`, `SLACK_ALERT_CHANNEL=C0BTUNT3B5Z`, `CLOUDFLARE_ACCOUNT_ID`, secretVariables `SLACK_BOT_TOKEN` ← `cw-s3-slack-bot-token`, `CLOUDFLARE_API_TOKEN` ← `cf-pages-token`. First unattended converge = the 2026-09-16 12:00Z run.
5. **Backfill posted** to `#cw-s3-usage`: August (OP `p1789516492354009`, 15 daily replies from 8/15 — the first CW scan ever) and September (OP `p1789516512015929`, 15 replies). `-D` was unnecessary: distinct per-reply senders don't collapse. The OP's `month-to-date` was then linked to the month's Diff view (lead-in → latest) and both OPs re-converged in place.

Left by hand: a channel topic for `#cw-s3-usage` (the bot deliberately lacks `channels:manage`).

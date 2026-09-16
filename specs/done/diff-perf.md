# Diff section latency: cache tiers, batched lookups, warm-up (2026-09-15)

A deep link into the home page's Diff section (`?d=<scan>-7d#diff`) took 20–25 s to draw and threw a burst of 503s on the way. Measured against prod with the job's read token before any change:

| request | time | point lookups |
| --- | --- | --- |
| 7-day root diff (8/31 → 9/6) | 24 s | 149 |
| 7-day root diff (9/6 → 9/13) | 20 s | 118 |
| 1-day root diff | 9 s | 15 |
| the same 7-day diff, repeated | 21 s | |
| diff `summary=1` | 6.5 s | |
| one root subtree (`/api/subtree`) | 4 s | |

## Three causes, three fixes

1. **The colo cache never stored anything** (`279acff`). `/api/diff` and `/api/subtree` answered with `Cache-Control: private` and handed that same response to `cache.put`; the Workers Cache API refuses a response whose Cache-Control says not to share it (it reports a 413, which the code never checked). Every diff was recomputed for every viewer. `site/functions/_lib/edgeCache.ts` now stores a `public, s-maxage=86400` copy and hands the client the `private` one (`x-cache: hit|miss`). The scope gate runs before `match`, so a hit still only reaches an authorized viewer. Repeat: 17.6 s → 0.18 s.
2. **Point lookups ran eight at a time, two D1 queries and a ~280 KB range read each** (`7a5e338`). `buildDiff` walks level by level; for each name one side lacks it looked the path up on the fine 7.4 GB index (26,467 row groups), and 118–149 of those at 8-wide was ~20 rounds. Each level's asks now go through `readAsks` as one batch per side (a few span queries + one round of group reads); lens views keep the per-ask path (the user-keyed index's group stats only hold inside a single-user group), and a batch past the reader's group cap falls back to it. 7-day root diff: 20–24 s → 4–5 s locally, 5–7 s cold on prod, rows byte-identical.
3. **The 503s were the isolate, not the endpoint.** All three landed at one timestamp, with Cloudflare's own 5 kB error page rather than our JSON, and three concurrent heavy diffs from curl didn't reproduce them: the Worker hit its 128 MB memory limit while a diff's two tier reads, its fine lookups and a subtree shared an isolate, and every in-flight request died together; the client's retries then succeeded. Fixes 1 and 2 shrink both the work and the overlap; if it recurs, `wrangler pages deployment tail` during a cold load is the confirmation.

Also: the diff slot reserved 380 px while the map renders at 0.6 × canvas width, so the map landing shoved everything below it by ~230 px after the hash pursuit had parked. The slot now reserves the map's real height.

## Global tier + daily warm-up (`714be65`)

The colo cache is per data-center, so the first viewer in each region still paid the compute. Both endpoints now sit behind colo cache → **KV** (`CACHE_KV`, namespace `oa-gcs-usage-cache`, key = SHA-256 of the cache-key URL, 30-day TTL, a KV hit back-fills the colo cache, `x-cache: kv`). Precomputing is then just warming: `gcs-usage warm-cache -d <scan>` replays the home page's default requests — the subtree, and for each diff pair (the previous scan plus the 1d/3d/7d/14d/30d chips resolved to the nearest earlier scan, as the client does) the `summary=1` twin and the full rows — at the canvas widths common windows quantize to (`ceil(innerWidth/128)*128`: 512, 1280, 1536, 1792, 1920). `run.sh` runs it after the healthcheck, non-fatal. First run from the laptop for 9/15: 55 requests, 410 s of request time, ~100 s wall at 4-wide; every warmed key is a 0.2 s hit afterwards. Scope-dependent views (marks, owner lens, class filter, drilled paths) are keyed on the ledger head or path and stay live; they're the fast cases now.

## Where the cold time really goes (measured on the edge, later on 2026-09-15)

`Server-Timing` lied by omission: Workers freeze the clock between I/O events, so decode and aggregation showed as zero. The deployment log tail (`wrangler pages deployment tail <deployment-id> --format json`, `cpuTime`) and an admin-free benchmark endpoint (`/api/bench`, `gcs` scope: today's group reader, gzip tier blobs, point lookups) gave the real split for a cold root subtree: **2.7 s CPU of 4.2 s wall** — of which the tier decode (all 29 coarse20 groups) is ~0.8 s and the view's aggregation over ~240k rows ~2 s. Findings that ruled options out: outbound fetches cap at ~6 in flight per request (`GROUP_READS` beyond that changes nothing); a binary-columnar tier blob decodes no faster than parquet (0.6 s vs 0.8 s CPU) and JSON blobs of the same rows kill the isolate at its 128 MB limit (the same 503 the page showed); page-index point lookups via hyparquet's filter path fetched 200 MB for 24 lookups; hysnappy can't run on Workers (compiles WASM from bytes); an in-isolate decoded-group LRU (kept, 24 MB) rarely hits across requests because a colo runs several isolates.

The fix that paid (`readView` fast path, this commit's parent): for the unscoped view the scoped aggregate is the row sum, so the threshold is applied to plain per-path byte totals before any aggregate object is built — ~240k rows → ~1k kept, output byte-identical. Cold root subtree 4.2 → 2.1–2.9 s wall (1.1–1.4 s CPU), bucket drill 3.0 → 1.9 s, 14-day root diff 14 → 4.8 s (2.7 s CPU), bucket diff 4.8 → 3.3 s.

## Not done

- What's left of a cold root view is ~0.8 s of parquet decode (29 groups, ~28 ms each on the edge) plus ~1 s of fetch rounds at 6-wide; the root threshold at laptop widths (~42 GiB) sits between the coarse16 (64 GiB) and coarse20 (4 GiB) floors, so a 16 GiB tier (`viz.COARSE_EXPS` + site `COARSE_EXPS`, backfill) would cut both by ~4× at the cost of folding deep sub-16 GiB cells earlier. The diff walk's point lookups still decode a whole 8k-row fine-index group per hit; smaller row groups for the fine index (4× the D1 footer rows) or a real page-index reader are the remaining levers there.
- `depth=1` first render: with the views at ~1 s each the walk is now most of a diff, so drawing the bucket level first would land ~2 s early on a cold diff. Cheap; do it if cold diffs still matter after warming.
- Artifact Registry holds 112 untagged job images with no cleanup policy (layers dedupe, so a few GB, but unbounded); a policy scoped to the `gcs-usage-snapshot` package (keep tagged, drop untagged > 7 days) is one command away.

Tooling: `tmp/diff-timing.sh` (prod timings with the job token), `tmp/warm-prod.sh` (warm prod from the laptop); local `wrangler pages dev` on :3254 reads real D1/GCS behind the dev identity stub and persists both cache tiers under `site/.wrangler/state`, so `rm -rf site/.wrangler/state/v3/cache` exercises the KV path.

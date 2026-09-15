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

## Not done

- `depth=1` first render (draw the bucket-level rows before the full walk): after the batching, the two tier reads (~2.6 s) dominate and the full walk adds ~2 s, so the progressive version would land ~2.5 s earlier. Worth it only if the tier reads don't get faster first.
- Tier reads themselves (`readView`: the coarse tiers at 8-wide group reads) are the remaining floor for every subtree and diff — the next lever if 4 s cold views still matter after warming.
- Artifact Registry holds 112 untagged job images with no cleanup policy (layers dedupe, so a few GB, but unbounded); a policy scoped to the `gcs-usage-snapshot` package (keep tagged, drop untagged > 7 days) is one command away.

Tooling: `tmp/diff-timing.sh` (prod timings with the job token), `tmp/warm-prod.sh` (warm prod from the laptop); local `wrangler pages dev` on :3254 reads real D1/GCS behind the dev identity stub and persists both cache tiers under `site/.wrangler/state`, so `rm -rf site/.wrangler/state/v3/cache` exercises the KV path.

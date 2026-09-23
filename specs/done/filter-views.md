# Filter views: a path filter selects subtrees, paints fast, and scopes every section

**From:** the union preview review (cw-s3, 2026-09-16). With `?f=ttl` at the root, the map drew `tmp/ttl=14d` as one 107 Ti slab with no children, the input fired a request per keystroke (37 requests, `subtree` 3–8 s and `diff` 10–13 s each, nothing cancelled), the held view was cleared to "0 bytes · 0 objects" while the next one loaded, and *Size over time* ignored the filter while *Diff* honoured it. Applies to both deployments (gcs has the same filter path; cw's 100 Ti single matches just made it obvious).

## 1. Where the filter runs today (server-side, post-read)

`?f=` → `q=` on `/api/subtree` and `/api/diff`; the client never filters map rows (it only re-filters the small marks feed). In `view.ts` the read is planned as usual — tier by the unscoped root's pixel threshold, one depth-band read of every row above the attenuated threshold — and `nameFilter` (`_lib/scope.ts`) then runs over *those rows*: the predicate is a case-insensitive substring of the whole index path (so `ttl` matches `tmp/ttl=14d` and everything under it), the **outermost** matches are the match roots, and each root's aggregate is summed into its ancestors. By design it does not visit the roots' descendants ("its descendants are not visited"), so a matched directory always arrives childless. Two consequences: the slab, and matches below the read's threshold are invisible (the read was budgeted for the unfiltered view, not for finding names).

## 2. Semantics: a match selects its subtree, found adaptively

- **Phase 1 — resolve the match roots on the coarsest tier**, independent of the pixel budget: the coarse tiers hold every path whose subtree clears an absolute floor (`coarse16` = 64 KiB), thousands of rows, one small read. Match roots = outermost matching paths (as `nameFilter` computes now). Nothing matched above the floor → walk down the tiers (`coarse20/24` are supersets in the other direction, so: `coarse16` → fine only if it found nothing), i.e. the traversal adapts its depth to the filter results rather than to the canvas.
- **Phase 2 — read each root's subtree as a region** with the normal pixel-budget fold, the way the lens path already reads claimed regions (`readRects` over per-root rects), with the threshold re-based on the root's own depth and the budget split across roots by bytes. Tier for phase 2 is chosen per root from its bytes (a 107 Ti root reads `coarse24`; a 2 Gi root reads fine) — the existing planner rule, applied per region.
- Response: the forest (roots linked under their ancestors as today, now with children) + `matched: [{path, b, o}]`. `/api/diff?f=` composes the same way over both scans' match roots (union of the two sets).
- Children table / drills unchanged: rows drill into the filtered subtree with `?f=` kept in the URL.

### As built (2026-09-16, `readView` in `_lib/view.ts`, pure parts in `_lib/filter.ts`)

- Phase 1 reads `coarse16` under the drilled path with no threshold (every path over its absolute floor — ~8k rows on cw); if nothing matches it walks `coarse20` → `coarse24` → fine **at the plain view's pixel threshold** (an unthresholded `coarse24` is ~1.2M rows on cw, past the reader's 700k-row guard), so a match below both the coarsest floor and the pixel threshold stays invisible, as before. `matchRoots` is the outermost-match rule scoped to the drilled root; a matching root itself means the plain view.
- Phase 2 uses **one forest threshold** = matched bytes × min cell area / canvas — splitting the budget by bytes gives every root the same bytes-per-pixel, so per-root thresholds collapse to one number, attenuated from each root's own depth (`rebasedThreshold`); the tier is the plain planner's rule over that number (`pickTier`), `partial=true` forces the coarsest. The 24 largest roots are read as regions in one `readRects`; further roots stay leaves. Verified on cw's synced scans: the `tmp/ttl=14d` root's children (and grandchildren) are name- and byte-identical to drilling into it.
- `/api/diff?q=`: each side plans its forest by its own matched bytes; the larger threshold is the shared floor and the other side is re-read at it. `matched` in the response is the union of both sides' roots. Under a user lens the claims fold owns the read, so the old post-read filter remains for that combination.

### Fast first paint

- Phase 1 + a coarse-tier phase 2 is milliseconds; return that with `partial: true`, then the client re-requests with `full=1` for the per-root-planned tiers and swaps the view when it lands.
- No name index is needed: the coarse tier is the index. Substring matching cannot use row-group path pruning, but phase 1 reads the whole (small) tier and phase 2 prunes by the roots' prefixes.

## 3. Client behaviour

- Debounce the input (~250 ms), and **abort** the in-flight `subtree`/`diff` requests when the filter changes (AbortController through react-query's `signal`).
- **Keep the previous view** under "loading view…" until the replacement arrives — the map already does this on scan switches (`shownTree = tree ?? lastTree`); the filter path must use the same held state. No "0 bytes · 0 objects" frame.
- The lifecycle fold only fetches `lifecycle.json` for scans on/after the recorded-from date (no expected 404s in the console).

## 4. Every section scopes to the filter

- **Size over time**: `/api/series?paths=<matched roots>` — sum per scan over the matched set (one index-row read per root per scan; a handful of roots × ~70 scans). Subtitle: "Stored bytes under “ttl” (3 prefixes) per scan".
- **Diff**: already scoped; keep the header form "the whole bucket · “ttl” — …".
- **Bytes by creation date**: scoped to the matched set when the age data can be (per-prefix age rows exist at the top level only; otherwise say so in the subtitle).

### As built, §3–§4 (2026-09-16)

- Client: the filter box edits a local draft, the URL follows after 250 ms; every subtree/diff `queryFn` passes react-query's abort signal to `fetch`; with a filter the companion query fetches the whole forest from the coarsest tier (`partial`) and the main query `full=1`; the map holds the previous tree (dimmed) as it already did across scan/drill changes; `lifecycle.json` is fetched only for scans ≥ the recorded-from date.
- `/api/series?paths=a,b` (≤ 24 roots, `parsePaths`): each scan's point is Σ one root read per root; `SizeOverTime` takes the deepest subtree response's `matched` roots and subtitles "Stored bytes under “ttl” (3 prefixes) per scan", and waits for the roots rather than showing the whole-store series first. The Diff header already read "the whole bucket · “ttl” — …". Age data is per top-level dir: `scopeAgeRows` scopes the chart exactly when every match root is a top-level dir, else the chart stays whole and its subtitle says so.

## Tests and verification

- Pure: matched-set resolution (nested matches collapse to the outer one; an object match is a leaf; case rules as today), forest budget split, series summation — exact-equality vitest/pytest as applicable.
- Functions: on the harness with cw's synced scans, `?f=ttl` returns a forest whose `ttl=14d` root has the same children as drilling into it; `partial` then full; `diff` consistent.
- CIC: type `ttl` — one debounced request pair, previous map stays dimmed, coarse paint within ~300 ms, full within a few seconds, the slab is gone, series and diff both show the filtered totals; clear the filter → whole-bucket view and series.

## Non-goals

Fuzzy/regex beyond today's `text, a|b`; a global name index (the coarse tier is the index); the KV warm tier (gcs's `diff-perf` track, independent).

## Verified (2026-09-16, harness on cw's synced scans)

- Functions: `?q=ttl` → 3 match roots (`tmp/ttl=14d` 145.8 T, `marin/tmp/ttl=14d` 12.9 T, `tmp/ttl=30d` 7.2 T); the `tmp/ttl=14d` root's children and grandchildren are name/byte/object-identical to drilling into it; partial (coarse16+coarse16, 942 nodes) 0.6 s, full (coarse16+coarse20, 1221 nodes) 0.9 s; `/api/diff?q=ttl` summary + depth=1 compose; `/api/series?paths=` sums the three roots per scan (148.7 → 165.9 Ti).
- Client: typing `t`,`tt`,`ttl` within the pause issued exactly `q=ttl` (subtree ×2, diff ×3); the previous map stayed held and dimmed; "151 Ti matched"; the `ttl=14d` tile drew its children (skyrl 130 Ti …); the series subtitle names the 3 prefixes and the chart shows their sum; the age chart notes it is per top-level dir; the Diff header reads "the whole bucket · “ttl” — 135 Ti − 109 Gi + 16 Ti = 151 Ti".

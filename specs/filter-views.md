# Filter views: a path filter selects subtrees, paints fast, and scopes every section

**From:** the union preview review (cw-s3, 2026-09-16). With `?f=ttl` at the root, the map drew `tmp/ttl=14d` as one 107 Ti slab with no children, the input fired a request per keystroke (37 requests, `subtree` 3–8 s and `diff` 10–13 s each, nothing cancelled), the held view was cleared to "0 bytes · 0 objects" while the next one loaded, and *Size over time* ignored the filter while *Diff* honoured it. Applies to both deployments (gcs has the same filter path; cw's 100 Ti single matches just made it obvious).

## 1. Semantics: a match selects its subtree

Today `?f=` returns only rows whose **own segment** matches, so a matched directory is a leaf (its children — `skyrl`, `users`, `checkpoints-temp` — don't match and are cut). The bulk bar already computes the intended thing: the **matched-prefix set** (matches minus those nested under another match).

- `/api/subtree?f=`: resolve the matched-prefix set first, then compose the view as a **forest** of those roots — each root's subtree rendered with the normal pixel-budget fold, the budget split across roots by bytes. A match with no children (an object) is a leaf as before. Response carries `matched: [{path, b, o}]` so the client doesn't recompute it.
- `/api/diff?f=`: same forest composition on the diff tree (it already scopes totals to the matches; make the map consistent).
- The children table under a filter lists the matched roots' parents as today, but rows drill into the *filtered* subtree (the filter stays in the URL across drills, as it does now).

## 2. Fast first paint

- Resolve the matched set on the **coarse tier** (thousands of rows — milliseconds) and return that view immediately with `partial: true`; then the client requests the refinement from the floor-free tier (same request with `full=1`), which replaces the view when it lands. Same two-step for `/api/diff`.
- Segment-name matching can't use the row-group path-range pruning; on the full tier, prune by the **matched set's prefixes** instead — once the coarse pass has named them, the full-tier read is a handful of ranges, not a scan.

## 3. Client behaviour

- Debounce the input (~250 ms), and **abort** the in-flight `subtree`/`diff` requests when the filter changes (AbortController through react-query's `signal`).
- **Keep the previous view** under "loading view…" until the replacement arrives — the map already does this on scan switches (`shownTree = tree ?? lastTree`); the filter path must use the same held state. No "0 bytes · 0 objects" frame.
- The lifecycle fold only fetches `lifecycle.json` for scans on/after the recorded-from date (no expected 404s in the console).

## 4. Every section scopes to the filter

- **Size over time**: `/api/series?paths=<matched roots>` — sum per scan over the matched set (one index-row read per root per scan; a handful of roots × ~70 scans). Subtitle: "Stored bytes under “ttl” (3 prefixes) per scan".
- **Diff**: already scoped; keep the header form "the whole bucket · “ttl” — …".
- **Bytes by creation date**: scoped to the matched set when the age data can be (per-prefix age rows exist at the top level only; otherwise say so in the subtitle).

## Tests and verification

- Pure: matched-set resolution (nested matches collapse to the outer one; an object match is a leaf; case rules as today), forest budget split, series summation — exact-equality vitest/pytest as applicable.
- Functions: on the harness with cw's synced scans, `?f=ttl` returns a forest whose `ttl=14d` root has the same children as drilling into it; `partial` then full; `diff` consistent.
- CIC: type `ttl` — one debounced request pair, previous map stays dimmed, coarse paint within ~300 ms, full within a few seconds, the slab is gone, series and diff both show the filtered totals; clear the filter → whole-bucket view and series.

## Non-goals

Fuzzy/regex beyond today's `text, a|b`; a global name index (the coarse tier is the index); the KV warm tier (gcs's `diff-perf` track, independent).

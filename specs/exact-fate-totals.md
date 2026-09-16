# Exact keep / sweep / undecided totals (floor-free)

> Superseded 2026-08-29 by `path-agnostic-serving.md` §2.3 — same algorithm, surfaced as `GET /api/marks/totals` (not `/api/fates`; the ledger vocabulary is *marks*), with the server-side WAL fold it needs.

## Symptom (2026-08-28)

Same scan, same ledger, three different "sweep" numbers:

| view | keep | last ckpt | sweep | undecided |
|---|---|---|---|---|
| homepage, fresh load | 183 Ti | 175 Ti | 446 Ti | 2550 Ti |
| homepage after Communal → All | 207 Ti | 65 Ti | 554 Ti | 2528 Ti |
| `/users` Total row | — | — | 460 Ti | — |

## Cause

The fate rollup (`subtreeFateTotals`) and the per-user fates (`allUserFates`) are computed **client-side against whatever tree is loaded**, and mark prefixes that fall below that tree's fold can't be resolved — their bytes settle under the nearest loaded ancestor's fate (usually *undecided*), and `keep_last_ckpt` marks whose step dirs aren't in view can't be decomposed (they stay amber "last ckpt" instead of splitting into keep/sweep).

- Fresh load (no lens): the map's base is the **pixel-budget `/api/subtree`** at the root (lazy drill, `specs/done/path-index-lazy-drill.md`) — a few hundred nodes. Most of the ~7k mark prefixes are folded away → sweep understated by ~100 Ti, last-ckpt overstated by ~110 Ti.
- Any lens (`?l=`) or filter (`?f=`) pulls **`tree.json`** (size-floored, `MIN_FRAC` of parent; ~29 MB) → far more prefixes resolve. React Query keeps that data after the lens is cleared, so "All" afterwards stays on the deeper tree → the numbers stay different until a reload.
- Drilling grafts floor-free subtrees at the drilled path into the same tree → the root totals can shift again slightly (monotonically toward exact) after a drill.
- `/users` walks `tree.json` (so ≈ row 2) but only credits bytes that have an owner (`us` shares or a claim) — bytes with no user land in no row, so its Total is a different quantity, not a third estimate of the same one. (Now labelled as such in its tooltip.)

## Shipped mitigation

Mark mode always loads `tree.json` (`needFullTree` includes `markMode`), and the fate rollup waits for it (`fateReady`) instead of showing the coarse tree's numbers. Every view of the homepage now computes on the same tree; row 2 above is what everyone sees. Cost: the lazy-drill bandwidth saving only applies outside mark mode (i.e. to nobody, today). Still floor-dependent: marks below `MIN_FRAC` remain unresolved, and drills still nudge the totals.

## Exact version (this spec)

Totals should come from the **floor-free path index** (`listing/<date>/path-index.parquet`, one row per prefix at every depth, sorted `(depth, path)`), computed once per `(scan, ledger head)` on the server, and consumed by the map's rollup, `/users`, the digest bot, and the sweep executor — which needs exactly this primitive (bytes and object counts per live mark prefix, net of deeper overriding marks) to plan and report a sweep.

### Algorithm

Live marks `M` (from the ledger fold: newest row per prefix, clears included). For each `m ∈ M`:

1. `bytes(m)`, `objects(m)` — one row lookup at `(depth(m), path(m))`.
2. `net(m) = bytes(m) − Σ bytes(m′)` over `m′ ∈ M` whose nearest marked ancestor is `m` (a trie over `M` gives that in one pass).
3. `fate(m)` = its keep value; `keep_last_ckpt` decomposes by listing `m`'s subtree rows at depths `depth(m)+1…` until a step-numbered sibling set is found (same `CKPT_NUM_RE` as `klcSplits`), kept = max-step child's bytes.
4. Totals: `keep = Σ net(m)·[fate=keep]` (+ KLC kept), `sweep` likewise, `undecided = total − keep − sweep`. Per-user: multiply each mark's net bytes by the `usr` attribution slices of its rows (the index carries `usr` per path), or the claimant when an owner row covers it.

Row lookups: the reader in `functions/api/subtree.ts` (`openIndex` / `readRows`, row-group spans by `(depth, path)` min/max) already does ranged reads; ~7k marks cluster into a small number of row groups (siblings share them), so one request is tens of range GETs, not thousands. Cache the result in D1 keyed by `(scan, max(action_id))` — the digest bot and `/users` then read it for free; a new action invalidates (or the client applies the delta locally until the next recompute).

### Surface

- `GET /api/fates?date=<scan>` → `{ total: {keep, sweep, keep_last_ckpt, unmarked}, users: { <uid>: {…} }, marks: [{prefix, fate, bytes, objects, net_bytes, net_objects}], asof: {scan, action_id} }`.
- Map rollup at the root reads `total`; drilled nodes keep the client walk (it's exact once the drilled subtree is grafted, and the drilled node is what the graft covers).
- `/users` reads `users`; the Total row then equals the map's numbers minus ownerless bytes, and says so.
- Sweep executor consumes `marks` — it's the manifest.

### Not in scope

Re-deriving attribution or the lens filters server-side; they stay on `tree.json`.

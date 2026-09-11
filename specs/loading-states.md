# Loading states: audit (2026-09-11) and what's left

Ryan demoed the site and hit two silent loads: toggling **not** on the owner picker showed the previous scope's map unchanged (no in-flight signal), and picking a diff start scan days back left a blank 380 px hole for 10–20 s. An audit of every fetch in `site/src` followed.

## Shipped

- **Map**: the held previous tree (`mapTree`) now dims under a centered "loading view…" marker while the new scope's tree is in flight (`mapStale`), and shows a corner "filling in…" pill while the full tree lands behind the depth-1 one or a refresh runs (`mapBusy`). Everything derived for the drawn map (`klcIdx`, `dateRange`, `catOrder`, the rules section) follows `mapTree`, so a hold no longer empties decorations under a still-showing map or unmounts the Ownership section. The deliberate no-`keepPreviousData` on the per-path subtree queries stays (a held sibling subtree under a new name breaks the graft — see the comment at `dataFor`).
- **Diff**: `placeholderData: keepPreviousData`; the section keeps its height (`.diff-slot`), the last diff dims under an "aligning A → B…" marker, the subtitle shows "aligning" instead of the previous pair's numbers; first load gets a skeleton of the same height.
- `Busy` (`src/Busy.tsx`) + `.busy-host` / `.busy-overlay` styles are the shared affordance for any widget that holds content across a load.
- `ChildrenTable`'s `segs` prop is memoized (a fresh array per render defeated its memos); the Treemap rollup's client state walk is cached by inputs (it re-walked the drilled subtree on every hover/outline render).

## Findings not yet acted on

TSQ coverage is near-total. Outside it: `OgPage` (serial DIY `fetch`es in a `useEffect`, no cache/loading/error), `TokenModal` (own phase state machine, has loading UI), `FilesPage` (file-tree's `HttpStore`, own caching).

1. **`isFetching` is read nowhere else.** Background polls (`actions` 30 s, `mark-totals` 30 s incl. its ~10 s cold recompute, `deletion-runs`/`sweep-jobs` 30 s, `scans` 5 min, every refetch-on-focus) are invisible. Cheap: a corner `Busy` on the relevant section when `isFetching && !isPending`.
2. **No pending UI at all**: `/user/:id` map query; all five `/sweep` queries (console is blank until candidates land); `DbTable` (`return null` while pending); the age chart section (heading over nothing); `OgPage`.
3. **Text-only `loading…` that collapses its section**: `SizeOverTime` (220 px), `/user/:id` estate, `/users` meta, `/assignments`, `/marks`. Reserve each widget's height with a skeleton (`.tm-skel` pattern).
4. **Ledger poll invalidates the world**: `useMarkIndex` keys on the `['actions']` data identity; a byte-identical 30 s poll should keep identity via structural sharing — verify with the render spy; if not, gate on max `action_id`. Today a no-op poll rebuilds `MarkIndex` → `klcSplits` over the tree → `markOutlines` → a full treemap re-layout.
5. **Progressive diff**: `/api/diff` returns totals and rows together; a `summary=1` companion (like the map's `depth=1`) would paint the +X/Δobjects line in under a second while the 10–20 s alignment finishes.
6. **Duplicate query definitions** with drifting options: `['scans', store.key]` (`scan.ts` vs `UserPage.tsx`), `['user-emails']` (twice in `sweep.ts`), `['rules']` (`App.tsx` vs `MarksPage.tsx`). Whichever mounts first wins the options.
7. **Deep-link scroll loop** (`App.tsx`, 500 ms interval for up to 60 s, re-armed on `[hash, tree, meta, scans]`) — with the map and diff no longer reflowing the page it can shrink to a couple of `requestAnimationFrame` passes.
8. Web workers: not worth it before 4–5; the trees are pixel-budgeted and the ledger is ~7 k rows. Measure with `?spy=1` first.

# FT/DT consolidation — one tree, two lenses

mgu sits between two converging siblings: **file-tree** ("FT", `@rdub/file-tree`
— the storage-agnostic browser: stores, viewers, and now a `TreeSource` seam +
`renderers/treemap` wrapping `@disk-tree/react`) and **disk-tree** ("DT" — the
scan engine + the `@disk-tree/react` treemap/timeseries widget lib, which mgu
vendors at `packages/react`). FT's own `specs/tree-sources-and-treemap.md` names
mgu as the reference consumer; DT's demo `ui/` is adopting `<FileTree>`. mgu is
the third node riding the same rails — and conceptually the site already *is*
"browser + treemap of one tree, with domain chrome" (marks, attribution, $).
This spec makes that literal, in phases.

## Phases

1. **Re-pin FT to a current dist SHA** — ✅ done (this branch): `5709d4e4`
   (`dist.3418f69`) → `49c02fab` (`dist.412a456`). Unlocks `TreeSource`,
   `renderers/treemap`, `stores/gcs` (bearer-OAuth), the newer table/viewer
   subpaths, and the omnibar/OG work as it lands. `/files` unchanged.
2. **CP DT's pending layout fixes into `packages/react`** — ✅ done (this
   branch): `foldThin` + `minCellSide` (folds tall/thin sliver cells into a
   hoverable `(+n)` tile) and `squarifyRemainder` (side-by-side long-tail
   layout; FT's OG-card path uses it). Sync state advanced in
   `specs/dt-core-upstreaming.md`.
3. **"Any path links to `/files/<path>`"** — the treemap tooltip's `open ↗` /
   `copy` (shipped) is the down-payment; extend hrefs into the `/files`
   browser from: dashboard treemap cells (secondary action), `/user/:id`
   prefix rows, `/users` drill targets, and breadcrumbs. Reciprocally, FT's
   treemap adapter supports `cellHref` so `/files`' map view can link back
   into the dashboard.
4. **Unify `/api/subtree` behind an FT `TreeSource`** — the payoff. Implement a
   marin `TreeSource` (either a `snapshotTreeSource` over
   `snapshots/<date>/tree.parquet`, or a thin `diskTreeTreeSource`-style
   adapter over the existing `/api/subtree` — FT's spec sketches both), then
   pass `treeSource` + `treemapRenderer` to `<FileTree>` on `/files`. The
   dashboard treemap and the `/files` browser become two views of one tree —
   recursive dir sizes and a list↔map toggle in the browser, free cross-links
   both ways, and the `/files`-vs-`/` split reduces to two lenses on one
   source (the route split stays as URL naming, nothing more).

## Architecture decisions (recorded 2026-08-31)

- **DT stays vendored** (`packages/react` full copy + manual CP treadmill per
  `specs/dt-core-upstreaming.md`). mgu is the widget's forcing workload —
  surgery on accessors/layout/folding originates here and upstreams to DT.
  The treadmill is real toil (three commits of drift accrued in days), but
  in-tree hackability is worth it while the widget is hot.
- **FT is a dist-SHA dep, not a vendor/subtree.** mgu consumes FT through its
  extension surface (stores, renderers, `treeSource`, hooks) — exactly what a
  library boundary handles well — and FT upstream already treats mgu as its
  reference consumer, so upstreaming needs is cheap. A **subtree-merge of FT
  into this repo** (git-vendoring both deps with bidirectional CP — the
  `git subtree` / Copybara pattern) was considered and **deferred**: it would
  double the CP treadmill for a dep we don't do surgery on. Revisit only if
  mgu repeatedly needs FT-*internal* patches faster than upstreaming allows;
  the halfway house is a long-lived mgu-specialized FT branch, dist-built and
  SHA-pinned.
- **The lib-level dep arrow points FT → DT-widget** and must stay acyclic:
  `@disk-tree/react` is the leaf (pure presentational, FT-free), FT is the
  composition layer (its `renderers/treemap` subpath takes DT-widget as an
  optional peer), and apps (mgu `site/`, DT's demo `ui/`, FT's demo) consume
  both. DT's demo adopting `<FileTree>` is app-layer, not lib-layer — no
  cycle.

## Stray input

- `specs/favicon-marks.md` (untracked, dropped in by the DT session) writes
  its output paths as `ui/public/…` — mgu's frontend is `site/`, so read those
  as `site/public/…` when implementing (fix the file in place; it isn't in
  this branch's checkout because it's untracked).

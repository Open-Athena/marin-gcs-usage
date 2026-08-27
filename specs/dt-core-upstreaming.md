# DT-core changes pending cherry-pick upstream

Core (`packages/react`, `src/disk_tree`) evolves here first (marin is the
forcing workload), then CPs onto the main disk-tree branch(es). Same-shape
conflicts are expected to resolve in one direction or the other. Convention:
tag core-touching commits `[CP→disk-tree upstream]` going forward; this file
tracks the backlog until each lands.

## Landed upstream (disk-tree session, 2026-08-27)

All `packages/react` items are on disk-tree `main` (each with new tests there):

| fork sha | upstream sha | what |
|---|---|---|
| 34d3d04 + f17c666 + c982c51 | b76a448 | `initialPath` + controlled `path` (gesture routing, input-guard) |
| f17c666 + d8b73a0 | f415b57 | flex label layout + size on 2nd line for tall leaves |
| a8924d6 | 23746c3 | anchored + interactive tooltips (grace timer, hover-into) |
| 6805541 | 3c0e45d | `CellStyle.segments` — makeup stripes |
| e73055f | 46e31ed | `TimeSeries.yTickValues` (unit-aligned y-ticks) |

`Treemap.tsx`/`TimeSeries.tsx` now differ from upstream **only** by upstream's
`fadeFloor` cumulative-fade scheme (upstream `d9ded7c`, not yet adopted here) —
future CPs in either direction should apply nearly clean. One deliberate drift:
upstream's controlled-path reporting includes our `c982c51` fix verbatim.

## Deferred (deliberately, not forgotten)

- `tree_build` floors (`6fd4b55`, `97a7814`): the module doesn't exist upstream,
  its fields/consumers (`cb`/`tm`/`ub`/`sh`, `viz.py`, `cw-webdata.py`) are
  fork-domain, and the pixel-budget redesign below aims to obsolete the floor
  tricks — upstreaming now would port code being redesigned away. Revisit when
  the path-index/pixel-budget core shape settles; CP that instead.

Planned (specs in this repo, implement as core features):
- pixel-budget subtree serving hooks (`specs/path-index-lazy-drill.md`)
- shared-edge tiling + depth-scaled borders (`specs/treemap-shared-edges.md`)

## New since last sync (pending CP)

- `Treemap.tsx`: `cellHref` accessor — leaf-rendered cells become real
  `<a href>`s (native cursor/cmd-click/link hints); nested-tile cells stay
  divs (anchors can't nest); plain clicks preventDefault into the normal
  onCellClick/drill/pin flow.

- `Treemap.tsx`: size-gate `branch`/`chain` chrome classes (`chromeOk` = min
  dim ≥ 28px; `branch` also whenever children actually render). Lazy-drill
  grafting turned dense fields of small tiles into drillable/chain cells, and
  the consumers' inset-ring treatments (drill affordance, chain doubled edge)
  read as dark inner rings at ≤~22px.

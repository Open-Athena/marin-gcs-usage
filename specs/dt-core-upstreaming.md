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
- hybrid canvas rendering (question raised 8/28): dense fields of tiny leaves
  (hundreds of sub-20px rects that today either fold into "(other)" or render
  as DOM cells) could paint to a `<canvas>` layer with hit-testing for hover,
  while cells above an interaction threshold stay DOM (anchors, badges, CSS
  borders, tooltips). Would let the fold floor drop without DOM cost. Note the
  8/28 sluggishness was NOT DOM count — it was an O(marks) per-cell lookup in
  the consumer (`marks.ts`), fixed to O(depth).

## Sync state (2026-08-28)

Both directions clean as of today: upstream `761602c`…`c128ff0` (paint-layer
fade, opaque container base, `--dt-treemap-cell-border`, `renderCellSubtitle`
dims, shared-edge tiling + `borderWidth`) cherry-picked here (manifest:
`specs/done/dt-core-cp-2026-08-28.md`); our `cellHref`, size-gated chrome, and
measured tip clamp landed upstream as `cb075ea`. `Treemap.tsx` should now
differ only by whatever lands next on either side. Marin uses
`tiling={(…, ctx) => ctx.medianChildArea < 100 ? 'shared' : 'gaps'}` and
themes `--dt-treemap-container-bg` / `--dt-treemap-edge` via `app.scss`.

## Python engine drift (`src/disk_tree`) — untracked until 2026-08-28

`gcs` ≈ `cw-s3` (only `tree_build.py` floors differed; synced 8/28), but both
vs upstream `main` diverge **in both directions** (22 files, +646/−1388 at
`cb075ea`):

- **upstream-only** (not here): diff index (`diff_index.py`, `cli/diff_index.py`),
  vocab sidecar + block index (`sidecar.py`, `cli/vocab.py`), compare perf
  (64K row groups, chunk map, single-flight), `touched` status, `recursive_diff`
  outer-join rewrite, `server.py` compare routes. Marin's `DiffTreemap` uses the
  older recursive diff — CP if/when the compare view matters here.
- **fork-only** (not upstream): `tree_build.py` (additive-field model, parent-
  relative floors, `ABS_FLOOR`/`TOP_K` — deferred by design, see above), access
  plane productionization (`access/{aggregate,parsers/gcs,read_sizes,schema}.py`,
  `cli/access.py`, `dt access sizes`), GCS usage-log dedup fix.

Needs a Python CP manifest from the disk-tree session (which side wants what);
the react lib is the only surface currently kept at parity on every sync.

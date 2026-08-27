# DT-core changes pending cherry-pick upstream

Core (`packages/react`, `src/disk_tree`) evolves here first (marin is the
forcing workload), then CPs onto the main disk-tree branch(es). Same-shape
conflicts are expected to resolve in one direction or the other. Convention:
tag core-touching commits `[CP→disk-tree upstream]` going forward; this file
tracks the backlog until each lands.

## Pending (2026-08-26/27 session)

| sha | what | files |
|---|---|---|
| (pre-compaction) | anchored + interactive tooltips (no mouse-follow; hover-into) | packages/react/Treemap |
| (pre-compaction) | size on 2nd line for tall leaves | packages/react/Treemap |
| (pre-compaction) | `yTickValues` override (unit-aligned y-ticks) | packages/react/TimeSeries |
| 6805541 | `CellStyle.segments` — makeup stripes for mixed leaf/fold cells | packages/react/Treemap |
| 6fd4b55 | `build_tree`: parent-relative fold floor | src/disk_tree/tree_build |
| 97a7814 | `build_tree`: `abs_floor` + `max_children` bounds | src/disk_tree/tree_build |

Planned (specs in this repo, implement as core features):
- pixel-budget subtree serving hooks (`specs/path-index-lazy-drill.md`)
- shared-edge tiling + depth-scaled borders (`specs/treemap-shared-edges.md`)

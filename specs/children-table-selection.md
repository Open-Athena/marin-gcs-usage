# Children table: row selection, bulk marking, pinnable owner histogram

The table under the treemap (`site/src/ChildrenTable.tsx`) lists the drilled directory's children: now paged (50/page, pager top and bottom), one dot per decision instead of three word-chips, and an `OwnerBar` in place of "top user". Three asks remain from the 2026-09-09 review.

## 1. Multi-select rows, one action for the selection — DONE 2026-09-09

Landed as `site/src/rowSelection.ts` (`useRowSelection` + `useRowSelectionKeys`), now the single model behind `/sweep` and the children table: checkbox column, click / shift / ⌘ selection, `j`/`k`/`⇧j`/`⇧k`/`x`/`⇧x`/Esc, and a bar above the children table that marks (keep / sweep / last ckpt / clear) or assigns the whole selection in one batched `POST /api/actions`. The App-level `x` (clear owner axis) moved to `alt+x`; the picker has a × button. Per the file-tree session's read (2026-09-09), the headless hook is slated to move to **use-kbd** (`useRowSelection`), with file-tree growing only thin row seams; the API here is shaped for that move.

Original ask:

The /sweep console already has the selection model wanted here (`SweepPage.tsx`: `selected: Set<prefix>`, a keyboard cursor, shift-click range extension, a select-page checkbox in the header, a selection bar that appears when ≥ 1 row is selected, `sel`/`cur` row classes). Factor it out and reuse it:

- `useRowSelection<T>(rows: T[], key: (r) => string)` → `{ selected, isSelected, toggle(keys, shiftFrom?), selectPage(), clear(), cursor, moveCursor }` plus the keyboard bindings (`j`/`k`/`x`/`⇧x`) wired through `use-kbd`, so both pages share one implementation and one set of hotkeys.
- A `<SelectionBar>` above the table when the selection is non-empty: `N selected · ≈ bytes` and the same decision dots as a row (keep / last ckpt / sweep / clear) applied to every selected prefix via one batched `POST /api/actions` (the API accepts an array), then refetch. "Assign to me" for the selection too.
- Column 0 becomes the checkbox column (`.col-sel`), as on /sweep.

## 2. Owner histogram: pin, expand, filter

`OwnerBar`'s tooltip lists the top 8 owners. Clicking the bar should **pin** the tooltip (the treemap's pinned-tooltip pattern: stays until Esc / click elsewhere), "… and N more" expands the full list in place, and clicking a person applies the page's owner lens (`?o=<user>`) — the same thing the legend's user rows do. Needs `OwnerBar` to accept an `onPickUser` (the treemap already has `onPickUser`).

## 3. Checkpoint-shaped dirs, decided ahead of time

`looksCkpt` (client, `sweep.ts`) decides where "keep last ckpt" is offered from the loaded tree: the dir's own name, a `checkpoints/` child, or ≥ 2 step-numbered children. Below the pixel budget the children aren't loaded, so a run dir deep in a big listing gets no KLC offer even when it has one. The index can settle this at build time: one boolean per `path-index` row, `ckpt` (the same three rules over the dir's *full* child list, at listing time), served in the subtree payload as `TreeNode.k`. The client then reads the flag and keeps the heuristic only as a fallback for older scans without it. Cost: one column in the tier parquet + the `index_schema` bump.

## 4. Later

- Sort by owner share / state.
- Persist page size (`?n=`), if anyone asks.

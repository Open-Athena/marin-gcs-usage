# Children table: row selection, bulk marking, pinnable owner histogram

The table under the treemap (`site/src/ChildrenTable.tsx`) lists the drilled directory's children: now paged (50/page, pager top and bottom), one dot per decision instead of three word-chips, and an `OwnerBar` in place of "top user". Three asks remain from the 2026-09-09 review.

## 1. Multi-select rows, one action for the selection — DONE 2026-09-09

Landed as `site/src/rowSelection.ts`, now the single model behind `/sweep` and the children table: checkbox column, click / shift / ⌘ selection, `j`/`k`/`⇧j`/`⇧k`/`⇧x`/Esc, and a bar above the children table that marks (keep / sweep / last ckpt / clear) or assigns the whole selection in one batched `POST /api/actions`. The App-level `x` (clear owner axis) moved to `alt+x`; the picker has a × button. Since 2026-09-09 the headless hook is **use-kbd**'s `useRowSelection` / `useRowSelectionKeys` (`use-kbd@0.13.0`, whose `commitOnRowsChange` freezes the active range into pinned keys when the page's rows change — the paging gap mgu asked for in use-kbd's `specs/done/use-row-selection-paging.md`); `rowSelection.ts` is a thin adapter adding `.sel`/`.cur` class names, the link/button/input click guard, cursor scroll-into-view, and a `⇧x` page toggle that preserves other pages' pins (use-kbd's `⌃a` `selectPage` replaces them, so it's disabled). Semantics follow use-kbd's file-manager model: the cursor row is always selected, `j`/`k` move a single selection, `⇧j`/`⇧k` extend, ⌘-click and checkboxes add or drop rows — so the old `x` (toggle the cursor row without moving) has no equivalent and was dropped.

2026-09-10: the selection summary + bulk controls sit in the pager row (right side), so a selection appearing never shifts the table under the pointer; a click on the section's dead space deselects. Esc clears only while something is selected — the action is registered only then (use-kbd preventDefaults a matched key before checking `enabled`, so a disabled action would still swallow it), and `@rdub/treemap`'s Backspace/Esc drill-up now listens on `window` and skips a `defaultPrevented` key (CP → dt). The column is labeled `marks`, the site's word; the internal `fate` names are gone too — `mark` is a ledger row / the `?k=` axis (`MarkAxis`, `markAxes`), `state` the effective keep / last-ckpt / sweep / undecided of bytes (`MarkState`, `stateOf`, `subtreeStateTotals`); the color mode is `'marks'` (`?c=fate` still decodes).

Render audit (same day): a row click cost ~2 s of React work in dev — the table re-walked every row's subtree for the state bar on each render, and registering the Esc action only while selected bumped use-kbd's registry version, which re-renders every `useActions` consumer (the whole app). Now the per-row derivations are memoized per page × ledger, Esc is a capture-phase listener outside the registry, and one selection change is one table-only commit (~100 ms dev, hidden tab) plus the selection bar's tooltips positioning themselves. `site/src/dev/renderSpy.ts` (dev / `?spy=1`, `window.__renderSpy`) records per commit which components rendered, their self time and who scheduled it; `e2e/renders.spec.ts` asserts those exact shapes. use-kbd took both asks the same day (disabled bindings fall through; registering no longer re-renders every registrant) — pinned as a dist SHA of `78090d5` via `pds gh`; the spy shapes are unchanged with it. Later the same day use-kbd also gated its built-in Esc `clear` on a non-empty selection and stopped a keystroke re-rendering every display consumer (dist `ef820a1`), so the adapter's capture-phase Esc listener is gone: Esc is use-kbd's `clear` again, a `j` is one table-only commit, and Esc with nothing selected drills up — all three are render specs now.

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

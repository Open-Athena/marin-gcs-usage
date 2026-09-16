# Page-scope bar: one sticky bar for nav, scope, and identity

**Status:** shipped 2026-09-05 (`gcs`).

## Problem

The home page had ten rows above the treemap, five of them controls (h1 + nav, scan line, two edu folds, lens tabs, lens note, typed-prefix input, color-by row, map title, legend). The scan picker scrolled off long before the sticky color-by row (which grew a scope strip only once stuck) took over, and the lens tabs, the `?u=` legend pin, and the Diff section's own scan pickers were three overlapping ways to say "which slice, which window".

## Design

One bar (`SiteNav`) on every page, `position: sticky` from the first pixel, so it looks the same parked at the top as mid-scroll:

- **☰ menu, far left** — Map / Scans / Users / Marks / Sweep, About (the two former edu folds, now a modal), page extras (home: "Mark a typed prefix…"), CoreWeave ↗, GitHub ↗. The brand link sits beside it.
- **Middle: the page's scope/controls.** On the home page, in reading order: drill-path crumbs (`all buckets` or `bucket/…/leaf`, each segment drills up) · scan picker (tooltip: publish time, fleet totals, list-price estimate) with the diff delta beside it (links to `#diff`) · **color** dropdown (marks / read / user / written / tree — group and user·group are hidden but still decode from `?c=`) · **marks** axis chips · **owner** axis chips + user select · path filter (+ bulk bar when it matches). Other pages put their own controls here (`/users`, `/user/:id`: the compact `ScanPicker`).
- **Avatar menu, far right** — identity card, units toggle (Ti/TiB ↔ T/TB, shift for the B), agent token, log out. Guests see "sign in".

The bar publishes its rendered height as `--topbar-h` on `<html>`; anchored sections use it for `scroll-margin-top`, and the deep-link pursuit parks at it, so wrapping onto two rows on a narrow viewport still lands sections correctly.

### The diff window

The scan picker is the window's **end**. Its **start** picker (`8/25 → 8/28`) only appears while a section that reads the window — the Diff, or the size chart with its shaded band — is on screen (IntersectionObserver over `#diff` and `#over-time`). The Diff section keeps the span presets but no longer has pickers of its own.

### Two orthogonal axes replace the lenses

| axis | param | values | scoping |
|---|---|---|---|
| marks | `?k=` ⊆ `ksu` | keep / sweep / unmarked, any subset (absent = all) | `applyFateFilter` (generalizes the old To-do prune; `keep_last_ckpt` counts as keep ∧ sweep); the children table filters the same way; size chart flips to mark progress when `series.json` carries `fate` |
| owner | `?o=` | `claimed`, `unclaimed`, `me`, or a user key (absent = everyone) | `unclaimed`/`claimed` slice bytes exactly (`unattrSlice` / `claimedSlice`); a user keeps ≥60%-owned subtrees (server user-lens when there's no name filter) and is the map's highlight |

Chip semantics: toggling the last "on" chip off rolls back to all (the mark-feed's type chips now match). Picking a user narrows "claimed" to that person; `me` resolves per reader (an unmapped email shows a note). The legend's user rows and the "unclaimed" row pin onto the owner axis; a group row still pins `?t=`.

Legacy params rewrite on load: `?l=todo` → `k=u`; `?l=unclaimed|communal`, `?t=unattributed|communal` → `o=unclaimed`; `?l=user[&lu=x]`, `?mt=`/`?mu=`, `?u=x` → `o=x|me`.

## Removed

`LensBar`, the edu folds (`useFold`), the scope chip, the stuck scope strip and its sentinel, the header `.sub` scan line, the dead store switcher, the `group`/`user·group` buttons.

# cw-s3 parity pass — Changes-section scan pickers + small backlog

**Status: done (2026-09-03).** CP cursor after this pass: **gcs `0cf6be6`** (every gcs commit through it has been considered; the next pass starts at `0cf6be6..gcs`).

## Context (2026-09-03)

CW storage is hot right now: a `CoreWeaveStorageQuotaExceeded` alert fired for US-EAST-02A (Grafana rule may trigger ~10% below the true limit), Mark freed a few TB and is hunting bigger targets, and Russell kicked off a cleanup pass in Slack pointing the team at **cw-s3.oa.dev** ("Ryan's nice viewer"). Live triage includes 3–4 datakit token stores (Mark plans to delete `store_0381a974` and `store_81e7e39a`), ~43 TB of rav's grug checkpoints, 61 TB SkyRL under a 14d TTL, and ~13 TB of small `grug/(other)` runs. The viewer has an audience today.

## Parity model

Branch-per-deployment is intentional (`specs/branch-parity-discipline.md`): `cw-s3` tracks `gcs` by adapted cherry-picks (patch-ids differ — neither `cw-s3..gcs` nor `--cherry-pick` counts the gap honestly). The CP cursor is the thing to track. Before this pass: cw-s3 `e018da3` = CP of gcs `8cac227` (gcs~22).

**Correction found during implementation:** the cursor is coarser than "everything before `8cac227` is here" — cw-s3's CP history is selective (e.g. `1e5ee40` squashed several gcs commits), and `9b8d237` (`useScan` + `<ScanPicker>`, 33 commits *before* the cursor) had never been ported. Treat the cursor as "last gcs SHA *considered*", and `git-didi`/`gdx` the tree when a feature seems missing rather than trusting ancestry.

## What landed (`8cac227..gcs`, classified)

**Ported (adapted for the leaner CW site — one page, no react-query, no marks/sweep/users):**

- `9b8d237` → `site/src/scan.ts`: `fmtScan`/`encodeScan`/`decodeScan` lifted out of `App.tsx` plus `useScan()` (prefix-match `?d=`, absent = latest, 5-min poll via a bare interval instead of react-query). `<ScanPicker>` itself is **not** ported — CW has no `SiteNav`/secondary scan-scoped pages to host it; the home map keeps its inline picker.
- `1690055` → Changes section with pickable endpoints: `?dp=` "before" select + "after" select that IS the page's scan (`?d=`, moves the whole page). Non-default "before" (or a scan with no baked `diff.json`) aligns client-side via `site/src/clientDiff.ts` (verbatim port). CW has no `/api/subtree`, so both sides fetch that scan's whole `tree.json` (module-level dedupe cache) and align from the **bucket** node, matching the baked rows' bucket-relative paths. `App.tsx` now keeps `trees` keyed by scan id (page scan + diff "before").
- `3296ccd` + `5c50fa3` → About paragraph is a `<details class="prose fold">` with the ⓘ summary line and the first-session-open / explicit-toggle-wins `useFold` (v2 keys `gcs-usage:fold2:*`); header is the designed cluster: `Scans` quiet-tab link + site-wide units button + identity chip, flush right.
- `3296ccd` + `9fe605b` (treemap) → per-cell adaptive `edge` (page-bg at depth 0, fill pulled toward `--surface` below), depth-emphasized `borderWidth` (6 / 2.5 / 1) **capped by cell size** (`min(base, max(1, min(w,h)/16))`); user coloring paints a ≥94% single owner instead of unattributed gray (CW has no attribution today, so moot until it does).
- `9fe605b` (units) → compact `Ti`/`T`(+`B`) toggle in the header (click = IEC↔SI, shift-click = trailing B); same `useUnits` as the treemap and Changes stats.
- `679dd8c` → tooltip `PathBar` with a copy-to-clipboard button (shown when the tip is pinned/hovered). The `open ↗` action is omitted: CW's map has no `?path=` URL state to land on.

**Already equivalent / not applicable:**

- `6c6374b` (omit `?c` at the lens default) — CW's `?c` codec already omits the `user` default.
- `0cf6be6` (fate-rollup denominator, lens labels), the `1690055` `/sweep` hunk, and the `9fe605b` `/sweep` band links, 404 route, and mark-copy removals — no marks/lenses/sweep console/drill routing on CW.

**Skipped (GCS-only: sweep executor/console, attribution gate, D1 records, GCS diff CLI):** `77d6755`, `a2f9401`, `7acafdf`, `9ab0a8e`, `64c3b29`, `86c8c5f` (CW's `job/cw-diff.py` pipeline already exists), `364702c`, `6b63e26`, `dc9e676`, `28a2c50`, `7a4de43`, `9b83dc3`, `f086811`, `2af9e34`, `33384f0`, `895a038`, `2cea7c3`.

## Branch hygiene fixed along the way

- `site/cf-status` was still pointed at the **gcs** Pages project (`oa-gcs-usage`), so `site/deploy --status` reported gcs's deployed SHA and listed every cw-s3 commit as undeployed. Now `oa-cw-s3-usage`.
- Dev ports moved to **3263 (vite) / 3264 (wrangler)** — this worktree runs alongside the gcs session's dev stack on 3253/3254 (`site/dev`, `vite.config.ts`, `package.json#devPort`, `scrns.config.ts`).

## Deploy

`site/deploy` from this branch → cw-s3.oa.dev (pushes `cw-s3` to `o` by default). Verify with `site/deploy --status`.

## Session conventions

This worktree (`wt/cw-s3`) is the cw-s3 session's base dir — the `gcs` session lives at the repo root; neither touches the other's checkout. Cross-session asks travel via `specs/` files + `/read`. Each parity pass records its new CP cursor (last gcs SHA considered) in the implementation commit message and in the spec it closes.

# cw-s3 parity pass — Changes-section scan pickers + small backlog

## Context (2026-09-03)

CW storage is hot right now: a `CoreWeaveStorageQuotaExceeded` alert fired for
US-EAST-02A (Grafana rule may trigger ~10% below the true limit), Mark freed a
few TB and is hunting bigger targets, and Russell kicked off a cleanup pass in
Slack pointing the team at **cw-s3.oa.dev** ("Ryan's nice viewer"). Live triage
includes 3–4 datakit token stores (Mark plans to delete `store_0381a974` and
`store_81e7e39a`), ~43 TB of rav's grug checkpoints, 61 TB SkyRL under a 14d
TTL, and ~13 TB of small `grug/(other)` runs. The viewer has an audience today.

## Parity model

Branch-per-deployment is intentional (`specs/branch-parity-discipline.md`):
`cw-s3` tracks `gcs` by in-order cherry-picks (adapted, so patch-ids differ —
neither `cw-s3..gcs` nor `--cherry-pick` counts the gap honestly). The CP
cursor is the thing to track: **cw-s3 HEAD `e018da3` = CP of gcs `8cac227`**
(gcs~22 as of today). The true backlog is `git log 8cac227..gcs`, minus
GCS-only work.

## The backlog (`8cac227..gcs`), classified

**CP (CW-relevant):**

- `1690055` — Changes section: pickable diff endpoints; "after" IS the page's
  scan. ← the user-visible gap: GCS has `9/2 ▾ → 9/3 ▾` pickers, CW has only
  the static "Changes since previous scan" line. (`9b8d237`'s `useScan` +
  `<ScanPicker>` are before the cursor — already here.)
- `3296ccd` — intro blocks fold into `<details>`; depth-emphasized adaptive
  treemap edges.
- `5c50fa3` — designed header nav + edu folds collapse after first session.
- `9fe605b` (partial) — treemap seam cap (`borderWidth` scaled by cell size:
  `min(base, min(w,h)/16)`), SiteNav units toggle, 94–98% single-user color
  fix. Skip the /sweep band-link + /sweep units parts.
- `0cf6be6` (partial) — fate-rollup denominator fix for client-scoped views;
  only if CW renders mark/fate rollups at all. Copy/lens renames are GCS-only.

**Skip (GCS-only: sweep executor/console, attribution, D1 records):**
`77d6755`, `a2f9401`, `7acafdf`, `9ab0a8e`, `64c3b29`, `86c8c5f` (GCS diff CLI
— CW's diff.json pipeline already exists), `364702c`, `6b63e26`, `dc9e676`,
`28a2c50`, `7a4de43`, `9b83dc3`, `f086811`, `2af9e34`, `33384f0`, `895a038`,
`2cea7c3`.

After the pass, note the new CP cursor (the last gcs SHA considered) in the
implementation commit message so the next pass starts from there.

## Deploy

`site/deploy` from this branch → cw-s3.oa.dev (pushes to `o` by default).
Verify with `site/deploy --status`.

## Session conventions

This worktree (`wt/cw-s3`) is the cw-s3 session's base dir — the `gcs` session
lives at the repo root; neither touches the other's checkout. Cross-session
asks travel via `specs/` files + `/read`.

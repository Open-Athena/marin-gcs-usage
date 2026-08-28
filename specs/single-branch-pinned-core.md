# One branch, pinned core: ending the 2×3 sync matrix

## The problem, measured (2026-08-28)

Two engines (`src/disk_tree` Python, `packages/react` TS) are vendored by copy
into two deploy branches (`gcs`, `cw-s3`) and also live upstream
(`runsascoded/disk-tree`). That is six copies of core kept "in sync" by
cherry-pick manifests and parity commits — and today's audit found every edge
drifting: `cw-s3` was 10 react commits behind, the Python engine diverged from
upstream in both directions (22 files, +646/−1388) with no ledger, and the CW
cron died because a per-branch script (`job/cw-*`) was pruned assuming the
other branch had it (it didn't).

Two independent fixes remove two of the three axes.

## 1. Collapse `cw-s3` into `gcs` (removes the branch axis)

`cw-s3` looks 160 commits / −16k lines apart, but that is history shape, not
content: `gcs` is a superset except **two** commits — `e210f93` (branding +
`s3://` scheme) and `20d9697` (users-only attribution, no group axis). `gcs`
already has the CW store (`STORES`, `/cw` route, `scheme` prop, `markMode`
gated to gcs). So:

- Fold the two deltas into `gcs` as per-store config (`STORES.cw`: title,
  scheme, `axes: ['user']`, no marks). Verify `/cw` renders the CW snapshot.
- `site/deploy <target>`: `gcs` (Pages project `oa-gcs-usage`) or `cw`
  (`cw-s3.oa.dev`'s project) — same build, two targets; `prod`/`prod-cw`
  pointers.
- Restore `job/cw-{run.sh,batch-submit.sh,diff.py,webdata.py}` from
  `053cc33^` onto `gcs`; one image, two entrypoints; point the CW cron back at
  `:latest` (currently pinned to an 8/23 digest as a stopgap).
- Retire the `cw-s3` branch (tag it `cw-s3-final`).

## 2. Stop vendoring core (removes the engine-copy axis)

Consume disk-tree as a dependency instead of a subtree:

- `@disk-tree/react` → npm-dist SHA pin (`pds gh @disk-tree/react`), like
  `@rdub/file-tree` already is. Delete `packages/react`, `ui/`, root `tests/`.
- `disk-tree` Python → git-SHA pin in `marin/pyproject.toml` (`disk-tree @
  git+https://github.com/runsascoded/disk-tree@<sha>`). Delete `src/`. The
  Docker image installs the pin (also fixes the unpinned `pip install ./marin`
  the audit flagged — install from `marin/uv.lock`).
- Core changes then land **upstream first** (the disk-tree session), and marin
  bumps the pin — the reverse of today's "core evolves in marin, CP later".
  `specs/dt-core-upstreaming.md` and its CP manifests go away.

**Precondition** — upstream must contain everything marin imports. Today marin
uses exactly: `disk_tree.tree_build.build_tree`/`DirRow`,
`disk_tree.access.{aggregate,read_sizes,parsers}`, and the `disk-tree bulk-list`
CLI. All four are **fork-side** (never upstreamed), while upstream's own recent
Python (diff index, vocab sidecar, compare perf, `server.py`) is server-side
compare-view work that marin's CF-Pages deployment never runs. So the useful
direction is fork → upstream only: manifest written for the disk-tree session
at `~/c/disk-tree/specs/marin-python-cp-2026-08-28.md`.

## Order

1. Python fork→upstream CP (dt session) — unblocks the pin.
2. Pin both engines on `gcs`; delete vendored dirs; CI runs `marin/tests` +
   `site` (the audit's CI fixes fall out naturally — the vendored suites were
   the only thing CI ran).
3. Fold `cw-s3` into `gcs`; two deploy targets; restore cw job scripts.

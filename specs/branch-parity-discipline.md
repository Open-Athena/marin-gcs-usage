# Branch parity discipline: audit + CP scrambles, verified with git-didi

## The model (decided 2026-08-24, reaffirmed 2026-08-28)

**One deployment = one long-lived branch**, cherry-picking between them, no
base branch: `gcs` (gcs.oa.dev), `cw-s3` (cw-s3.oa.dev), and future R2 / AWS-S3
deployments. Separate deployments have separate auth lists and significantly
customized FEs (cw-s3 has no team axis, different branding/scheme); baking
user-groups and mark+sweep into each one wants per-deployment customization
that config flags would only contort. Each branch owns its copy of the FE core
(`@disk-tree/react`) and the Python engine (`src/disk_tree`) and may do whatever
it wants with them. A large stream of CP-analogous commits across branches is
the *intended* workflow, not drift to be engineered away.

Upstream `runsascoded/disk-tree` (remote `dt`) is a sibling in this graph, not
a parent: it ships the **Flask × local/cloud-scan** reference www arch; the
marin branches ship the **Vite + CF Pages Functions × cloud-store** arch. The
shared thing is the Python scan/index core (`bulk-list`, `tree_build`, the
access plane, `webdata`-style aggregation). Each www arch answers its own
serving questions natively — no LCD abstractions across archs.

(A 2026-08-28 spec proposing to collapse cw-s3 into gcs and pin the engines
as dependencies re-litigated this and was withdrawn.)

## What today's audit actually showed

Not that branches are wrong — that the model's verification half was missing.
Nobody checked `gcs` vs `cw-s3` vs `dt/main` per surface between scrambles, so
`cw-s3` fell 10 react commits behind, the Python engine diverged both ways
with no ledger, and a per-branch script (`job/cw-*`) was pruned on the
assumption the other branch carried it. All fixable with routine, none with
architecture.

## The discipline

1. **Ledger of intended divergences** — per branch pair, per surface, a short
   list of the deltas that are *supposed* to exist (cw-s3: branding + `s3://`
   scheme, users-only axis, own auth list/Access app; vs upstream: Flask server
   + `ui/` vs `site/`; …). Kept in this file (below). Everything not on the
   list is a CP candidate or a mistake.
2. **[git-didi](https://github.com/runsascoded/git-didi)** as the checker —
   diff-of-diffs to verify long-lived forks keep only their intended deltas:
   `git-didi stat <base>..gcs <base>..cw-s3` (and `patch` per file) across the
   surfaces `packages/react/src`, `src/disk_tree`, `site/`, `job/`, `marin/`.
   Wrap as `scripts/branch-audit` (or a `/branch-audit` skill) that prints
   unexpected drift per surface; run it at the end of every CP scramble and
   any time a branch is touched for a while.
3. **Scheduled CP scrambles** — a recurring pass (post-milestone, or weekly
   during active work) that evaluates each branch against the others and
   moves the useful bits every direction, via CP manifests like
   `specs/done/dt-core-cp-2026-08-28.md`. The scramble ends with the audit
   green.

## Intended divergences (ledger)

| pair | surface | intended delta |
|---|---|---|
| gcs ↔ cw-s3 | `site/` | own Pages project (`oa-cw-s3-usage` ← cw-s3.oa.dev) + Access app `4c463052` (whole-host, OA-only, edge identity); branding, `s3://` scheme, no team/group axis, no mark & sweep (for now); no `/user/:id` pages |
| gcs ↔ cw-s3 | `job/` | `run.sh`+`batch-submit.sh` (GCS) vs `cw-*` (CW: S3-compat listing, precomputed `diff.json`); restore `cw-*` onto cw-s3 from `053cc33^` |
| gcs ↔ cw-s3 | `packages/react`, `src/disk_tree` | **none** — keep at parity (synced 8/28) |
| marin ↔ dt/main | `src/disk_tree` | upstream carries Flask serving (`server.py`, diff index, vocab sidecar, compare perf); marin carries nothing server-side. Shared core must be a superset upstream: fork→upstream manifest `~/c/disk-tree/specs/marin-python-cp-2026-08-28.md` |
| marin ↔ dt/main | `packages/react` | none — parity, both directions (8/28) |
| marin ↔ dt/main | www arch | `site/` (Vite+CFN) vs `ui/` (Vite+Flask) — intrinsically different |

## Diff mode for the CFN branches

Upstream's on-the-fly diff lives in its Python serving layer (Arrow/pandas/
DuckDB); that does not port to Workers. The CFN-native answers:

- **Interactive drill: align two subtrees.** Every date already has a
  `path-index.parquet` and `/api/subtree?date=&path=`. Diff(view) = fetch the
  subtree for both dates and align by name (client-side, or a thin
  `/api/diff-subtree` doing the join over range reads). On-the-fly, scales
  with the pixel budget, no engine port.
- **Heavy / global summaries: precompute in the job.** The established
  pattern — `job/cw-diff.py` runs upstream's `recursive_diff` to emit
  `diff.json` per scan pair; publish like snapshots, serve slices.
- Not: porting diff-index to TS.

## Filling out the arch matrix

A "vanilla" **Vite + CFN + cloud-store** demo app (no auth, no attribution, no
marks: `scans.json` + `/api/subtree` + treemap) as the reference impl for that
arch, alongside upstream's Flask × local reference. Natural home: the disk-tree
repo (owner of reference impls) — a spec for the dt session once we want it.

## Scramble log

### 2026-08-28 (cw-ward), triggered by the CW cron crash-loop

Landed on `cw-s3` (`7d348a0`, `18e4a10`, `f0d0b48`):
- `job/`: `cw-*` scripts restored from `053cc33^`; own `:cw` image (`job/build.sh`
  default tag, Dockerfile entrypoint `cw-run.sh`, `.[gcs,s3]` extras — the
  first build lacked `boto3`); GCS-only `run.sh`/`batch-submit.sh`/digest icons
  removed per the ledger; ERR-trap Slack alerting (dormant: no transport on the
  CW scheduler body yet — needs `SLACK_BOT_TOKEN` secret + a channel).
- `site/`: identity registry (Avatar/UserChip/UserCard + `identities.gen.ts`,
  `identities.yaml` synced) on rollup rows, tooltip user rows, ⌘K picker,
  whoami chip; IEC/SI units provider (`?si`/`?rb`, `i`/`b`). Intended delta
  recorded: no `/user/:id` link in the card.
- `packages/react`, `src/disk_tree`: parity (earlier today).

Deferred to the next cw-ward pass (each needs App.tsx wiring on cw-s3's older
App): `?f=` path filter (`filterTree.ts`), drill path in the URL path, shared
`SiteNav` chrome + `/files` bucket line, `series.json` size-over-time (needs a
`series` step in `cw-run.sh`), `/api/subtree` lazy drill (needs `-P` path index
from `cw-webdata.py`), CF-Access-cookie → app-session auth (cw-s3 still edge-
gated; that is an intended delta for now).

Verification: `scripts/branch-audit gcs cw-s3` — react/engine parity; `job`
now differs only by the ledger's intended files.

**Found while closing the site tier — cw-s3.oa.dev does not serve the `cw-s3`
branch.** It is a custom domain on the `oa-gcs-usage` Pages project (`wrangler
pages project list`), i.e. it serves the **`gcs` branch's build**, whose
host-aware store default (`ab88049`) shows the CW view on `cw-*` hosts. The
`cw-s3` branch's `site/` has never been deployed, and its `site/deploy` still
targets `--project-name oa-gcs-usage` — running it would overwrite gcs.oa.dev.
So today only the *job* half of the CW deployment is branch-owned. Closing the
model needs (Ryan's call — DNS + Access app): a second Pages project (e.g.
`oa-cw-usage`), the `cw-s3.oa.dev` custom domain moved onto it, the CW Access
app `4c463052` re-pointed if needed, `cw-s3`'s `site/deploy` retargeted, and
then gcs's host-aware CW branches (`/cw`, `isCwHost`) become dead code to prune.
**Resolved 2026-08-28 (same day):** `oa-cw-s3-usage` created, secrets copied, Access app extended to its pages.dev hosts, custom domain + CNAME moved, `cw-s3`'s `site/deploy` retargeted and run; gcs pruned of the host-keyed CW code (`f4e7025`). Both hosts verified gated with the right build.

### 2026-08-28 (cw-ward, pass 2) — after cw-s3.oa.dev moved onto the branch

The move exposed everything the CW *view* had only via the gcs build. Landed
on `cw-s3` (`245501f`, `1e5ee40`): `/data` rooted at `snapshots/cw/` with
sub-daily ids (the branch had never read CW data); `?d=` codecs + `fmtScan`;
diff.json "Changes since previous scan" (`DiffTreemap`); `initialPath` into
the lone bucket; latest scan = clean URL (no `?d` bake-in); `hasPrices` gates
every $ surface (CW has no class breakdown); auto age-chart granularity
(~30 bars); shared-edge tiling + core edge/container/cell-border vars.

Finding for the **gcs-ward** list: since today's core CP the core paints
sibling borders inline (`boxShadow` — `--dt-treemap-cell-border` ring in gaps
mode, half-stroke ring in shared mode), so the class-level inset box-shadow
rules in `app.scss` (`.dt-treemap-cell`, `.branch`, `.chain`, `.dust`,
`&.store-gcs …`) are dead on gcs too. gcs should drop them and tune
`--dt-treemap-cell-border` like cw-s3 did (`1e5ee40`).

### 2026-08-28/29 (cw-ward, pass 3) — age-chart axis, toggle placement, deploy parity

Landed on `cw-s3` (`fc90798`, `ec4ac9b`, `7d7dffb`): age chart gets its own
`?ac=` color axis with the `color by` control beside month|week|day (modes
`date|user|tree`; the caller passes `modes` so `user` is absent — CW has no
attribution — rather than a dead button); tiling toggle moves out of the
header into the treemap's crumbs bar via `renderLegend` (quiet style);
`site/deploy` now pushes the branch to `o` by default (`-P` to skip) like
gcs's does — before this, what ran on cw-s3.oa.dev could be ahead of GitHub.

Intended deltas (not ported): `read` axis and `fateReady` rollup gating (no
access logs / no marks on CW); `/users` fate $ pairs (no class pricing).

Queued for the next cw-ward pass (landed on `gcs` `974ee7c`, 2026-08-29):
`TimeSeries.yFrom` core prop + the size chart's `fit | from 0` toggle;
interactive legend rows (hover = solo, click = pin) — CW has user rows only
when attribution exists, so it's the group/tree-less subset; `SiteKbd`
(site-wide SpeedDial/omnibar) — CW is a single page, so only the omnibar's
page-link group matters there.

### 2026-09-04 (cw-ward, `/cp dt/main gcs`) — first `git cp` pass against upstream

Survey: `gcs` had nothing past the cursor (`8191de0` is its tip). `dt/main`
had no cursor (146 unmarked since the 2023 merge-base); triaged by surface
against this ledger. Landed on `cw-s3` (one commit, cursor `dt/main 25f3dc0`):

- `packages/react`: upstream `93e24b0..25f3dc0^` (14 commits) applied as one
  3-way patch, clean — `edgeContrast` (built-in luminance half-stroke for
  shared tiling, `parseColor`/`contrastEdge` in `colors.ts`), textured
  `(other)` dust tile (`DustHatch`), `foldControl`, `remainderTail`,
  `renderer="canvas"` (`layout.ts`/`cellStyle.ts`/`TreemapCanvas.tsx`,
  progressive paint, segments parity, bounded a11y/`cellHref` overlay),
  `onCellHover`, `edgeEmphasis`, click-through tooltip, `:focus-visible` +
  `.pinned` accent ring, `CellStyle.ring`. Tests 180 → 213 (upstream's count).
  Also re-widened `renderCellExtra`/`renderCellSubtitle` to upstream's
  `CellCtx` (was narrowed to `CellDims` here since the 8/28 sync).
- `src/disk_tree`: `588c4bf` (`index -q/--no-progress`, `progress` threaded
  through `backend.list`/`run_gfind`) — core-contract change, harmless here.
- Repo tooling: `scripts/branch-audit` + this ledger now live on `cw-s3` too
  (the `.claude/cp.yml` `audit:`/`ledger:` entries pointed at files only gcs
  carried).

Skipped, with reasons (so the cursor is trustworthy):
- **`25f3dc0` — extract the treemap core into `@rdub/treemap`.** Structural:
  moves `packages/react/src/{Treemap,…}` to `packages/treemap/`, changes CI
  + dist builds, and wants `site/package.json` re-pinned and imports renamed
  on *both* marin branches at once. Content parity is achieved via the
  pre-extraction diff; the split itself is Ryan's call, gcs-first.
- Upstream-only serving/local features (ledger: "upstream carries Flask
  serving"): `server.py` compare/library routes, diff index (`diff_index.py`),
  vocab sidecar, `storage/*` row-group + blob search path (depends on the
  library `config.py`), `cli/{du,reclaim,repos,overcount,snapshots,scans,
  vocab,diff_index}`, `extents.py`, `desktop.py`/macOS app, `library.py`.
- `ui/` and `specs/` commits — different www arch / upstream's own specs.
- The `⇒ disk-tree upstream`-tagged rows (stream engine, finalize, bulk-list
  adaptive) originated here and are already on both marin branches.

Queued:
- **gcs-ward**: this same react + `progress` patch (audit `gcs cw-s3` now
  shows `packages/react/*` and `src/disk_tree` differing until gcs CPs it).
- **dt-ward** (manifest `~/c/disk-tree/specs/marin-cp-2026-09-04.md`):
  `TimeSeries` `yFrom`/`annotations`/`onPickX` (gcs `974ee7c`, cw-s3
  `6121e6f`/`00dd02b`/`f3c5a03`), `S3BulkLister` adaptive retries (`e018da3`);
  the 8/28 Python manifest is still open upstream.

### 2026-09-04 (gcs-ward, `/cp dt/main cw-s3`) — mirror of the cw-ward 9/4 pass

Survey after backfilling the `cw-s3` cursor (`8191de0`'s prose marker wasn't
in trailer form; `git cp set cw-s3 5160a18`): 2 cw-s3 commits, 81 dt/main
(no prior gcs→dt cursor). Landed on `gcs` (one commit, cursors
`dt/main 25f3dc0` + `cw-s3 fceb717`):

- `packages/react` + `src/disk_tree`: `fceb717`'s exact patch (gcs was
  byte-identical to `fceb717^` on both surfaces, so it applied clean and the
  worktree is byte-identical to `fceb717` — i.e. to upstream's pre-extraction
  react core + the `progress` flag engine change). Tests 180 → 213 react,
  371 pytest, site `tsc` clean.
- Ledger: adopted the cw-ward 9/4 scramble-log entry (parity of this file).
- No `app.scss` adaptation needed: gcs dropped the dead cell box-shadows and
  themed the core edge vars back on 8/28–29 (see the comment citing
  `1e5ee40` at the `.dt-treemap-cell` block).

Skipped (mirroring the cw-ward triage, same reasons): `25f3dc0` (the
`@rdub/treemap` extraction — structural, gcs-first decision, still pending);
upstream Flask/local serving + CLI features; `ui/` + upstream `specs/`;
marin-originated rows upstream copied back (`DT_S3_ADDRESSING_STYLE` =
our `168308a`, stream engine, adaptive bulk-list). `f3c5a03` on cw-s3 is
their CP of our `8191de0` — nothing to port back.

Queued dt-ward: `~/c/disk-tree/specs/marin-cp-2026-09-04.md` (written by the
cw-s3 session) covers what both marin branches owe upstream; nothing new
from this pass.

### 2026-09-07 (cw-ward, `/cp gcs`) — gcs `8191de0..8ac1beb` (39 unmarked)

Landed on `cw-s3` (two commits; cursor `gcs 8ac1beb`):

- `f196e66` (ahead of the pass): Diff Δ-mode color by the node's own changed
  fraction, from `f2d0a8d`'s `colorForCell` — a wholly removed 1 Ti dir is
  full red beside a 25 Ti one (was normalised by the page's max delta).
- `packages/react`: `TimeSeries` `onBrush` + `window` (the core hunk of
  `f30230c`, applied verbatim) — parity restored.
- `src/disk_tree`: `tree_build.py` docstring path from the `marin/` →
  `gcs-usage/` rename (`f59162957`) — parity; the rename itself is not
  mirrored (cw-s3's `marin/` is only built into the job image).
- `site/src`, adapted: chart brush → `?d=<after>-<span>` via
  `useScan.setRange` (`f30230c`; cw-s3's points are scan instants, so the
  brush maps x back to scan ids and the window shades `[diffBefore, asof]`;
  no UTC-date fix needed — cw-s3 already renders instants in local time);
  deep links pursue their anchor until it parks and hold the scroll-spy off
  meanwhile (`8cf2b33` + `7f56d02`; cw-s3 has no sticky top bar, so the park
  margin is read from the anchor's computed `scroll-margin-top`, which the
  color-by bar sets only on attributed scans; and "parked" also waits for
  the map, since the baked `diff.json` mounts `#diff` before the tree lands).
- `site/dev`: wrangler relaunch loop (`e3bab91`) — memory pressure kills it
  here, not a D1 tunnel; the 500-vs-404 Diff note has no counterpart (cw-s3
  aligns client-side, no `/api/subtree`).
- Already here: `628635c` (gcs's CP of `fceb717`), `7f911c6` (`hasAttr`
  from meta — cw-s3 always did).

Skipped as gcs-only (ledger: index tiers / D1 / marks & sweep / lenses /
team axis / `job/` + `gcs-usage/` are intended deltas): `86b1b97`, `b14118b`,
`988103b`, `85a7592`, `888737b`, `528564c`, `43b4b33`, `ca6ab5c`, `f59162957`
(the rename; docstring taken), `79e6ea5`, `99c60a3`, `17e47f7`, `e4a43d7`,
`42c044c`, `7cfa124`, `2bae18c`, `2bd99af`, `42b3757`, `654eb00`, `f5d1868`,
`0f84f3d`, `e7135b2`, `53921c9`, `8a1b7f4`, `ca059a7`, `0841cd2`, `206c5e9`,
`c02a4b6`, `e9e1261`, `cbd5572`, `8ac1beb`; the rest of `f30230c`/`f2d0a8d`
(scope strip, lens-scoped Diff, mark-feed filters, server-side diff).

Deferred (portable, but a design decision for cw-s3): **`7ab2d25` +
`c24632a` — the sticky page bar** (☰ nav · crumbs · scan picker · avatar
menu; Diff header holds both scan endpoints). cw-s3 has one page and no
`SiteNav`; adopting the bar is a layout change to decide on, not a CP.

Queued gcs-ward (same-day follow-up on cw-s3): **the main map never showed
the core's adaptive edge** — `site/src/Treemap.tsx`'s `colorForCell` pinned a
per-cell `edge` (fill mixed toward the page bg) below depth 0, and the core's
`edgeContrast` default only applies when the consumer leaves `edge` unset;
gcs's wrapper has the identical lines. Also the Diff map now carries the
`gaps` tiling chip (`TilingToggle` + `tiling=`); gcs's `DiffTreemap` lacks it.

### 2026-09-07 (gcs-ward, `/cp dt/main`) — upstream's cloud-reduce / remote-target run

Survey `25f3dc0..41978ca`: 21 upstream commits. Landed on `gcs` (one
commit, cursor `dt/main 41978ca`):

- `packages/react/tests/TimeSeries.test.tsx`: upstream's tests for the
  `yFrom`/`annotations`/`onPickX` props (`3d48a20`, their CP of our
  `974ee7c`/`00dd02b`/`f3c5a03`) — the props were ours, the tests weren't.
  React suite 213 → 217.
- `src/disk_tree/backends/local.py`: `find` when `gfind` is absent
  (`3663e39`, clean `cherry-pick -x`).

Skipped: `5be454c` + `8c0628d` (their CPs of our `tree_build`/access-plane
and adaptive bulk-list work — already here, tests included); `bce974e`,
`642bd38`, `6a4a2e4`, `58a2845`, `fd509b9`, `268ddb7`, `5ec7f86`, `646e455`
(remote scan targets + capture/reduce + R2 serving: upstream's blob search
path, `capture`/`reduce`/`scans register`, `ui/cfn` Pages Functions,
manifests — a laptop→cloud pipeline; gcs lists buckets in Batch and serves
its own path index. The two engine touches inside them — `canonical()` on
`file` scan roots, 64K duckdb row groups — sit in `aggregate_*`, which
gcs-usage doesn't call); `0e14ae8`, `41978ca` (Pages auth for `ui/`; gcs has
its own); the specs commits. `268ddb7`'s `s3fs>=2024.10` pin (0.4.2 sends an
empty `x-amz-acl` on multipart, R2 rejects) is queued for the cw-s3 pass —
gcs writes through gcsfs / the `/gcs` mount.

Queued dt-ward: `~/c/disk-tree/specs/marin-cp-2026-09-07.md` — `TimeSeries`
`onBrush`/`window` (gcs `f30230c`), the one shared-surface change since
their last manifest.

Same day, second commit (`e9a3cce`): upstream's `25f3dc0` treemap-core
extraction landed — `packages/treemap` (`@rdub/treemap`) taken verbatim,
`packages/react` trimmed to the disk widgets + `export *`. `packages/` now
differs from upstream only by `TimeSeries`'s `onBrush`/`window`; the
`gcs ↔ dt/main` audit rows for `packages/react/src` should read `parity`
once that ports. The "structural gap" row above is closed.

### 2026-09-07 (gcs-ward, `/cp dt/main`, third pass) — the unification spec landed upstream

`7d9f7b9..01b5e19`: DT implemented `mgu-scale-unification.md` A–E in five
engine commits. Landed on `gcs` (one commit, cursor `dt/main 01b5e19`), as
upstream's final file versions (our pre-change divergence in
`aggregate_duckdb.py` / `import_listing.py` was only the `canonical()` /
`BLOB_ROW_GROUP_SIZE` / `--to` hunks skipped on the first pass — taken now):

- A `bcbe063` file-backed `--db`, `--partition-depth`; B `f746bce` `--label`
  slices as cascade group keys; C `bdf621f` `find/tiers.py` (`--tiers
  dirs,objects,coarse`, `--sort-variant`, KV floors) + `_SUCCESS.json`
  `started`/`finished`; E `01b5e19` `--size-hist`. Plus `blobfs.py` and
  upstream's `storage/base.py` (the writer imports `BLOB_ROW_GROUP_SIZE`;
  `storage/base` imports `blobfs`). `config.py` stays stripped, so
  `import --to <url>` (`config.set_write_target`) is not wired here — mgu
  writes tiers to the NVMe dir and uploads.
- **D held** (`16c621a`: hour-grained layer-2a, `--as-of`, `access state`,
  `--side/--max-col`): `access/aggregate.py` renames `day → hour` and the
  stats key `days → hours`; mgu's ingest (`access.py`, `reactive.py`
  compaction keyed `(bucket, path, day, op)`) and its retained 2a shards are
  day-grained. Adopting D is a migration (re-aggregate the retained raw
  shards at hour grain, repoint the compaction), scheduled with the A.3 gate.
  `aggregate_duckdb.py`'s `_Side` (the cascade half of D) is in, unused.

Tests: 104 in the ported files, 405 root, 144 gcs-usage. Next: the A.3
gate (DT's cascade on the 9/7 listing with `--db --partition-depth
--label usr --tiers --size-hist`, a2a against mgu's `path-index`) on Batch.

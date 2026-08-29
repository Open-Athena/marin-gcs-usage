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

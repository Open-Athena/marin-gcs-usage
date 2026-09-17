# Seams 1 + 2: the unified sweep model — implementation

The build plan for the two decided seams (`specs/gcs-toward-union.md` §5). Both
branches build to this so they converge by construction; DT upstreams the result
into `disk_tree` (its `specs/mgu-cp-2026-09-16.md` roadmap item 3) once it runs on
both. Written 2026-09-17.

## Target: one ledger, one plan object, one executor, per-deploy policy

- **Ledger (seam 2): gcs's `actions` WAL wins.** gcs migration 0010 (`actions` +
  `owner_prefixes` + `keep_prefixes`, most-recent-wins fold) is the reference.
  cw's `marks`/`mark_log` is its own documented subset-port (cw migration 0001
  header). Convergence is cw-side: cw adopts the `actions` ledger, its keep kinds
  (`keep`/`keep_last_ckpt`/`sweep`) become keep-axis actions. No gcs change.
- **Plan object (seam 1): cw's `plans`/`plan_items` wins.** A deletion plan is a
  first-class object (multiple concurrent drafts, each dry→real, a detail page).
  gcs adopts the tables (done: migrations 0024/0025) + the CRUD + a plan-aware
  executor.
- **Executor (seam 1): gcs's `sweep manifest`/`sweep execute` wins.** The
  exercised path (four real sweeps 2026-09-11; `cloud/src/dt_cloud/sweep_exec.py`
  → `execute_plan(plan_dir)` consuming a `plan-summary.json` with per-bucket
  manifests + `approved` bands). cw's `plan-sweep` executor was a port of it;
  drop it. The plan object *feeds* the manifest instead of the manifest being
  built inline from marks/approvals.
- **Policy is per-deploy config, not forked code:**
  - **Eligibility** `Store.sweepEligibility: 'owner-slice' | 'any'`. gcs =
    owner-slice (a non-admin's plan sweeps only dirs they own; `owner == author`
    enforced at manifest time, as `sweep_approvals mode='slice'` does today).
    cw = any (no owner data). The one executor honors whichever the store sets.
  - **Unmarked** `Store.unmarked: 'eligible' | 'untouched'`. gcs = sweep-eligible
    after the deadline (the mark-and-sweep premise). cw = untouched (only explicit
    `sweep` marks). Drives what the auto-seeded default plan pulls in.
  - **Undo** `Store.undo: 'soft-delete' | 'versioning' | 'none'`, constrained by
    the cloud adapter's offer: GCS offers soft-delete (default window) **and**
    versioning; S3/CAIOS offers versioning only. `undo`/`purge` endpoints exist
    only where a permanent-delete cloud needs them; GCS defaults to its
    soft-delete window and has neither.

## Checkpoints (each buildable: tsc + vitest + py suite green; destructive-path ones CIC'd)

1. **[done] Plan schema.** `plans`, `plan_items`, `deletion_runs.plan_id`
   (nullable), `deletion_runs.purge_state`. Migrations 0024/0025, cw's shape
   verbatim, `IF NOT EXISTS`. Apply to remote D1 is user-gated.
2. **`/api/plans` CRUD.** Port cw's `api/plans/[[path]].ts` + `_lib/plans.ts`,
   gcs-adapted: `gs://` prefixes, gcs auth helpers (`requireViewer`/`requireAdmin`
   already exist), `audit()` → gcs's `admin_edits`. **Multi-bucket difference:**
   cw refuses a plan whose items span buckets (`PlanSpansBuckets`) because its
   executor is one-bucket-per-run; gcs's executor already takes multiple `-b`
   buckets and `batchRegionFor` picks the region, so a gcs plan MAY span buckets —
   drop the refusal, keep `planBucket`'s grouping as "buckets in this plan".
   `snapshotPlan`'s keeps come from gcs's resolved `keep_prefixes` fold (the
   ledger), NOT a flat `marks` table (cw's source). Tests: CRUD + snapshot shape.
3. **Executor consumes a plan.** `sweep manifest` gains `--plan <id>` (reads
   `plan_items` for the sweep set) alongside today's ledger-sourced path; the
   dispatch bridge (`api/sweep/dispatch.ts`) takes `{ plan_id, mode, date, buckets? }`
   and records `deletion_runs.plan_id`. Eligibility gate reads
   `Store.sweepEligibility`. `sweep_approvals` stays the owner-slice evidence gate
   under 'owner-slice'; under 'any' it's bypassed. No behavior change for gcs's
   existing flow when a plan isn't supplied (keep the implicit path until the UI
   moves over). **Destructive path — dry-run only in tests; no real dispatch.**
4. **Auto-seeded default plan + /sweep redesign.** The page opens with a default
   plan materialized from the viewer's eligible slice (`Store.unmarked` +
   `sweepEligibility`), showing options immediately; "new plan" is the branch
   action, not the cold start. Per-plan detail page (items, runs, drift). **CIC
   the /sweep page** (empty→seeded, plan detail, dispatch dry-run) before done.
5. **Pluggable undo.** `undo`/`purge` behind `Store.undo`; the GCS adapter's
   soft-delete restore vs the CAIOS adapter's version-restore + purge. gcs keeps
   its soft-delete window as the net (no new endpoints); cw's `undo.ts`/`purge.ts`
   become the CAIOS adapter.

## Coordination

- **cw-s3 owns seam 2** (adopt the `actions` ledger, retire `marks`/`mark_log`)
  and **retiring its `plan-sweep` executor** in favor of gcs's. gcs owns the plan
  CRUD + executor rewire + auto-seed + eligibility/undo config. Both land on the
  union with the checkpoint protocol; the CP cursor advances per pair.
- **DT** upstreams nothing until checkpoints 1–3 run on both branches; then the
  unified ledger + plan model + executor is the `disk_tree` cloud engine (roadmap
  item 3). Seam 4 (digest profiles) is DT's comms item 1 and is independent.
- **D1 writes** (`migrations apply`) are gated to the user throughout.

## Non-goals

The branch/repo collapse, the D1 lineage merge (seam 3, collapse-time), the IdP
question, attribution changes.

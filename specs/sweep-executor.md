# Sweep executor — plan → review → execute (deleting what's marked `sweep`)

The marking phase is done ([mark-sweep-ui.md], [actions-ledger.md]); ~1,108 Ti is marked `sweep` and nothing can actually delete yet ([repo-hygiene-audit.md] ranks this spec #1). This spec designs the deletion system: **manifest-based, three-phase, dry-run-first**, with an admin review UI. We are deleting other people's data with (currently) **no undo** — soft delete is disabled on all six buckets (`retentionDurationSeconds=0`, no versioning, verified 2026-09-01) — so the design leans hard on: pinned inputs, an explicit reviewable manifest, independent cross-checks against the site's own fate totals, a temporary soft-delete safety net, and IAM that physically can't delete until the execute window.

## Principles

- **Nothing deletes except from an approved, pinned manifest.** The manifest is a parquet of exact object keys derived from one scan's listing + one ledger snapshot (`head = max(actions.id)`), both recorded in the plan. No "delete whatever is under prefix X at execute time".
- **Only objects present in the pinned listing die.** Anything written after the scan survives even inside a swept prefix (and overwrites are caught by generation matching, below).
- **Two independent implementations must agree before execute.** The plan's per-fate byte totals must reconcile with the site's `?marks=1` manifest / `mark_totals` (TS `computeTotals`) for the same scan+head — a full-estate cross-validation of the fate fold.
- **Reversibility window.** Enable soft delete (3–7d; window TBD with Percy) on the six buckets immediately before execute; disable after we're confident. Cost ≈ swept bytes × class rate × days/30.4 (computed exactly by `plan`; EM at 3d ≈ $0.7–1.1K, 7d ≈ $1.5–2.5K one-time vs ~$5–8K/mo saved).
- **IAM as a physical interlock.** The executor SA gets `storage.objects.delete` on `marin-*` only for the execute window (granted at go-time, revoked after). Until then every path through the code is incapable of deleting.

## Phase 1 — `gcs-usage sweep plan`

New CLI subcommand (runs on Batch for the full estate; laptop-OK for `-p` scoped runs).

**Inputs** (pinned + recorded in `plan.json`):
- `listing/<date>/<bucket>/*.parquet` — the per-object listing (`bucket, name, size_bytes, created, storage_class_id`).
- The live ledger via `GET /api/actions` (expanded `keeps` + `owners` rows joined to raw actions; regex patterns don't exist yet, so the expanded prefixes are exact). Record `head`.

**Fate resolution** (Python port, heavily tested):
1. Resolve each ledger prefix's *effective* fate: most-recent-wins across its ancestor-or-equal ledger rows (`ts DESC, id DESC`; NULL keep = unmark) — the exact `/api/resolve` rule.
2. An object's fate = the effective fate of its **deepest marked ancestor** (its covering set of ledger rows is identical to that ancestor's, so the winner is the same) — unmarked if none. Implemented as a longest-prefix match of each key against the ~7K ledger prefixes (sorted-array/trie; ~594M keys is minutes of work).
3. **KLC expansion** at object level, mirroring `klcSplits` exactly (`CKPT_NUM_RE = ^(?:step|checkpoint|ckpt|iter|epoch|global_?step)[-_]?(\d+)`): walking down from a `keep_last_ckpt` prefix, at the first level with step-numbered children the max-step child's subtree is kept and **everything else at that node sweeps** (non-step siblings included — that's what the UI painted red for reviewers); step-free levels recurse. A band where the walk finds no steps at all is **unresolved → keep + flagged** (`klc_ambiguous`; the UI's amber). Implemented + spec-tested in `gcs_usage.sweep_plan` (`klc_split`).

**Outputs** → `gs://oa-gcs-usage-dvx/sweep/<plan-id>/` (plan-id = `<scan>-h<head>`):
- `manifest/<bucket>.parquet` — fate=sweep keys: `name, size_bytes, storage_class_id, created, covering_prefix, action_id, actor, ts, klc` (sorted by name; row-grouped for ranged reads).
- `rollup.parquet` — dir-level rollup of the manifest (`dir, depth, bytes, objects, class bytes, actors`) at every ancestor depth — what the review UI drills.
- `plan.json` — scan date, ledger head, per-bucket/per-class/per-actor totals, monthly-savings estimate, **early-delete-fee bound** (per class: bytes younger than min duration × remaining term; `created`-based, so an upper bound — class-transition times aren't in the listing), **soft-delete window cost** at 3/7/14d, KLC-ambiguous totals, and the reconciliation block (plan totals vs `/api/marks/totals` for the same scan+head, with pass/fail).

`plan` is pure read — safe to run anytime, idempotent per (scan, head).

## Phase 2 — review: `/sweep` admin page + sign-off

- **New D1 table `sweep_plans`**: `plan_id, scan, head, created_by, created_ts, status ('planned'|'approved'|'executing'|'executed'|'abandoned'), approved_by, approved_ts, note`. Writes gated by the `admin` scope (new `/api/sweep` routes); reads `gcs`.
- **`/sweep` page** (admin nav): lists plans; a plan view shows `plan.json` headline (totals, costs, reconciliation status, KLC-ambiguous), a drillable rollup table (ranged reads of `rollup.parquet` via the existing `/v1/files` infra), per-actor grouping ("whose stuff dies"), and the KLC expansions spelled out (kept step vs swept steps per run). Deep-drill to raw manifest rows via the `/files` parquet browser.
- **Exclusions are ledger marks, not plan edits.** To pull something out of a plan: mark it `keep` (in the normal UI, from the `/sweep` drill's link-outs) and **re-plan** — a new head → new plan-id. One source of truth; no shadow exclusion list to reconcile.
- **Approval**: admin clicks approve (typed-confirmation of the plan-id + headline bytes) → D1 row. The runbook keeps the human step: post the plan summary to Slack (#gcs-usage) for Percy/David sign-off before approving. Approval is void if the ledger head moves — execute re-checks.
- **Users**: v1 read-only visibility — their existing lens views already show fate; the `/sweep` plan page is admin-only. Self-serve "delete my claimed data now" is explicitly **v2** (same manifest machinery scoped to `owner=me` + their own approval; not built until the admin path has survived a real sweep).

## Phase 3 — `gcs-usage sweep execute`

Batch job (same image/infra as the snapshot). **Dry-run is the default**; `--for-real` is required for deletion, and hard preconditions are re-verified in code at startup:

1. Plan status `approved` in D1, and ledger head: any post-approval `keep`/unmark action covering a manifest prefix → **abort** ("re-plan"). (Extra `sweep` marks are fine — they just aren't in this plan.)
2. Soft delete on (≥ the configured window, default 3d) on every target bucket (API check) — refuse `--for-real` otherwise (no override flag; enabling it is cheap and deliberate). Orthogonal to everything else here: it's bucket config the executor merely verifies, and `plan` prices the chosen window.
3. SA can delete (probe via a canary: create + delete one object under `tmp/ttl=1d/sweep-canary` in each bucket).

**Delete mechanics** (per bucket, per covering-prefix shard, resumable):
- **Fresh re-list of each swept prefix at execute time** capturing `name, generation, timeCreated` (listings lack generations — this closes the overwrite race): delete only keys in manifest ∩ fresh listing **with matching `timeCreated`**, via `DELETE ?ifGenerationMatch=<gen>`. Keys missing from the fresh list → already gone (log `skipped_gone` — a marked path that no longer exists is a graceful no-op, at the object or whole-prefix level); `timeCreated` moved → overwritten since scan (log `skipped_overwritten`, keep).
- **Drift detection — marks are as-of a past scan; reality moves.** The re-list also surfaces the reverse cases: (a) *new* keys under a swept prefix (written after the pinned scan — never in the manifest, so structurally undeletable here, but their presence means the prefix is live); (b) a KLC band that gained a **new max-step checkpoint** since planning (the planned "last" is no longer last — proceeding would delete older steps while keeping *both* the planned and the new max, i.e. strictly conservative, but the intent is stale). Both are **flagged and raise by default**: drifted prefixes are skipped, reported in the summary (bytes/objects of new data, per prefix), and the run continues on the clean ones. `--drift=proceed` deletes the manifest keys anyway (new keys always survive regardless — that's a structural guarantee, not a flag) for the common case where the drift is incidental; the cleaner path is re-planning against a newer scan, which folds the drift in properly.
- JSON-API batch requests (100 deletes/request), bounded concurrency w/ adaptive backoff (429/503 → EB, same philosophy as the S3 lister's adaptive mode). Object deletes are free-tier ops; the re-list LISTs are the main op cost (small).
- **Deletion log** → `sweep/<plan-id>/deleted/<bucket>/*.parquet`: `name, generation, size, class, created, deleted_ts, shard`. Checkpoint per shard (`_SHARD_DONE` markers) → kill-safe resume without re-listing completed shards.
- Dry-run mode runs the identical pipeline — re-list, intersect, generation match — writing `would-delete/` instead of deleting, plus the same summary. This is the "extensive dry run": it exercises everything but the DELETE verb.

**Post-run**: verification pass (re-list swept prefixes; manifest keys must be gone), summary to `#gcs-usage-alerts` + a human-readable report (bytes/objects deleted per bucket/actor, skips, duration), `sweep_plans.status = executed`. The next daily scan reflects reality; the ledger tombstone pass ([actions-ledger.md] §tombstones, still unbuilt) is follow-up work, not a blocker.

## Testing (before any `--for-real`)

1. **Resolver parity, property-tested**: Python fate of N sampled paths (marked, unmarked, nested-carve-out, KLC) == `/api/resolve` for each, exact equality. Plus unit fixtures for the tricky orderings (recency-beats-specificity, unmark, re-mark).
2. **KLC object-level vs tree-level**: synthetic layouts (HF `checkpoint-N`, `global_step_N`, `step-N`, mixed/none) — exact expected key sets.
3. **Full-estate reconciliation** (the big one): `plan` totals == site `mark_totals` for the same scan+head. Two codebases, one answer.
4. **End-to-end on a scratch bucket**: synthetic tree + synthetic ledger → plan → dry-run → `--for-real` → verify; includes overwrite-race and written-after-scan cases proving they survive.
5. **Prod dry-runs**: full plan + `execute` (dry) on the real estate; review the would-delete log; sit on it ≥ 1 day; re-plan to catch late marks; then the real thing, **one bucket first** (smallest: marin-us-west4), verify, then the rest.

## Non-goals (v1)

- User self-serve deletion (v2, above). — Regex mark patterns (don't exist). — Ledger tombstoning (follow-up). — CW/S3 sweep (separate estate, no marks yet).

[mark-sweep-ui.md]: mark-sweep-ui.md
[actions-ledger.md]: actions-ledger.md
[repo-hygiene-audit.md]: repo-hygiene-audit.md

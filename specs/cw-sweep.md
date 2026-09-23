# cw-sweep — mark & sweep (plan / dry-run / delete on Batch) for cw-s3

Grow the cw-s3.oa.dev app a `/sweep` page and a mark axis, so an operator can
mark CoreWeave S3 prefixes for deletion and **fire big deletes on GCP Batch from
the app** — the sweep subsystem gcs already has, converging onto cw-s3 per the
2026-09-08 direction in `specs/branch-parity-discipline.md` (mark & sweep to
every deployment; dogfood sweep on our own S3/R2 clouds).

gcs is the reference impl. This spec pins **what differs on cw-s3** and the work
to land it. Adapt from gcs; don't re-invent.

## Semantics vs gcs

| axis | gcs | cw-s3 |
|---|---|---|
| **owner** (who *wrote* it) | xref W&B + Iris (job scheduler) + **manual curation** (NOT access logs) | **manual curation only** for now; path-prefix rules already exist as a weak signal (`users/<name>/`, `tmp/ttl=…/<user>/`). W&B/Iris integration is optional/later. **Owner is not required for sweep** — drop the owner column/legend/chip. |
| **sweeper / actor** (who *marked / fired* it) | D1 user roles + admin scope | **CF Access identity** (`/cdn-cgi/access/get-identity`) — cw-s3 already has it. Record who marked and who dispatched; admin gate = a small allowlist (IaC'd), not a D1 role system. |
| **mark / fate axis** (keep / sweep a prefix) | absence = delete-eligible; kept safe by the owner-slice | **default = unmarked** (absence = neither swept nor kept); `keep`/`keep_last_ckpt` are affirmative protect signals; **nothing sweeps without an explicit `sweep` mark**. Safety comes from explicit-`sweep`-only + the curated plan, not attribution. The treemap outline capability already landed (`0a18858`); this wires the mark UI that consumes it. |

So the only genuinely-absent axis is *owner-from-integrations*. Everything the
delete flow needs (mark a prefix, who did it, plan, execute, undo) is available.

## Plan model — plan as a first-class object (decided 2026-09-11)

The flow is **mark → plan → dispatch → runs**:

1. **mark** — anyone marks a prefix `sweep` (or affirmatively `keep`). Raw signal
   only; a mark never deletes anything.
2. **plan** — an admin assembles marked prefixes into a **named, first-class
   plan** (`plans` + `plan_items`, 0002). This curation step is cw-s3's answer to
   gcs's owner==marker attribution slice: rather than auto-filtering marks to the
   marker's own owned dirs (an extra level gcs needed because its default is
   delete-eligible), a human explicitly picks what goes in. Cleaner, and the
   reference impl gcs can converge back toward.
3. **dispatch** — an admin fires a `deletion_run` (dry or real) against a plan;
   the executor snapshots the plan's items into a gs:// object-level manifest.
4. **runs** — a plan has **many** runs over its life (dry-runs, then a real run,
   re-runs after drift). `deletion_runs.plan_id` FKs the plan.

**Multiple independent plans: yes.** `plans` is a table, not a singleton — draft
plan A (old checkpoints) and plan B (`tmp/`) concurrently, each with its own
dry-run → real-run lifecycle and its own runs. This is barely more than FKs and
avoids conflating unrelated cleanups; it's the intended shape.

## Safety model — CAIOS versioning (researched 2026-09-11)

CoreWeave AI Object Storage (`cwobject.com`) is S3-compatible and **supports
bucket versioning**, but it is **disabled by default** and enabled per-bucket via
the **S3 API** (`aws s3api put-bucket-versioning --bucket <b>
--versioning-configuration Status=Enabled`; two-step — can't enable in
`CreateBucket`). Refs:
[about](https://docs.coreweave.com/docs/products/storage/object-storage/about),
[versioned buckets](https://docs.coreweave.com/docs/products/storage/object-storage/buckets/rclone-versioned-buckets).

On a **versioned** bucket:
- a delete writes a **delete marker**; the prior version is **retained and
  recoverable** (this is our soft-delete). Restore = delete the delete marker, or
  `copy-object`/`get-object` by `versionId`.
- list via `aws s3api list-object-versions` (`Versions[]` + `DeleteMarkers[]`).
- ~~no native lifecycle for expiring noncurrent versions was found~~ **Corrected
  2026-09-15 by probing the bucket**: CAIOS **does implement bucket lifecycle** —
  `get_bucket_lifecycle_configuration` on `marin-us-east-02a` returned 10 rules
  Marin already relies on (`marin-abort-incomplete-mpu` 7d + `marin-ttl-{1…7,14,30}d`
  `Expiration` rules on `tmp/ttl=<N>d/` prefixes). Whether
  `NoncurrentVersionExpiration` / `ExpiredObjectDeleteMarker` are *honored* is
  still untested (accept-test: PUT then observe). Until a noncurrent rule exists,
  cleanup is manual (`sweep purge`, or `list-object-versions` → delete by
  `versionId`).
- **Hazard once versioning is on**: under S3 semantics an `Expiration` rule on a
  versioned bucket writes a *delete marker* rather than freeing the object, so
  Marin's `tmp/ttl=<N>d/` cleanup now leaves expired data behind as noncurrent
  versions (quota not reclaimed) until a `NoncurrentVersionExpiration` rule is
  added — and `put_bucket_lifecycle_configuration` **replaces the whole config**,
  so any addition must be a read-modify-write preserving Marin's 10 rules (a
  shared-resource edit; Marin owns those rules).

Consequences for the design (mirrors GCS soft-delete's retention window):

1. **Guard.** The executor's preflight **refuses a real delete unless the target
   bucket has `Status=Enabled` versioning** (analogous to gcs's ≥7d soft-delete
   guard). Enabling versioning on `marin-us-east-02a` is an IaC prerequisite
   (Phase 0). *Ask CoreWeave for a native noncurrent-version lifecycle* — a
   nice-to-have that would remove the hand-rolled purge below.
2. **Delete ≠ space reclaimed.** On a versioned bucket a delete only writes a
   marker; noncurrent versions still occupy space. So "sweep" is two-stage:
   - **delete** (Phase 1) — write delete markers + record the run; instantly
     reversible.
   - **purge** (Phase 3) — after a hold period, permanently drop the noncurrent
     versions for the run's keys (space actually freed). No CAIOS lifecycle, so a
     GC job does it (list-object-versions → delete by versionId, or
     `cleanup-hidden`).
3. **Undo** (`sweep undo`, gcs already has it via `deleted/` manifest logs) =
   remove the delete markers for the logged keys, *before* purge.

## Architecture — maps onto existing cw-s3 infra

cw-s3's scan job **already runs on GCP Batch** (`job/cw-run.sh`, image tag
`IMAGE:cw`, submitted by `job/cw-batch-submit.sh`), listing `s3://marin-us-east-02a`
via `cwobject.com` with AWS creds from Secret Manager, output to `gs://…/snapshots/cw/`.
The sweep dispatch reuses this wholesale.

- **Plan API** — `site/functions/api/plans/*.ts` (admin scope): CRUD the `plans` +
  `plan_items` tables — create/close a plan, add/remove prefixes (typically pulled
  from the current `sweep`-marked set the FE offers). Reads are open to any
  authenticated viewer; writes gated on `admin_emails`.
- **Dispatch** — `site/functions/api/sweep/{dispatch,jobs,stop}.ts`, adapted from
  gcs. `dispatch` takes a **`plan_id`**, snapshots that plan's `plan_items`, and
  submits a **GCP Batch** job running `IMAGE:cw` (not gcs's snapshot image),
  command = `marin sweep manifest --plan <plan.json> -d <date> -o <manifest>` +
  `marin sweep execute [--for-real] <manifest>`. Manifest/log land in
  `gs://oa-gcs-usage-dvx/sweep/cw/runs/<id>` (part files, per gcs). Region: the CW
  bucket is single-region (us-east-02a) — one Batch region, no `batchRegionFor`
  fan-out.
- **Executor** — a new sweep module in cw-s3's **`marin/`** engine (cw-s3 has
  none today; gcs's lives in `gcs-usage/src/gcs_usage/{mark,sweep_plan,sweep_exec}.py`).
  Adapt those, swapping the **GCS client → boto3** (`delete_objects` in ≤1000-key
  batches, per-item retries, durable decision log). `sweep manifest` expands a
  **plan** (the dispatched `plan_items`, passed as JSON) — not the raw mark ledger
  — into the object-level manifest; reuse gcs's bisection / LPT-root-scheduling /
  preflight design. Preflight adds the **versioning-enabled check** above. `.[s3]`
  extras already ship in `IMAGE:cw`.
- **Marks + plans + runs store** — **D1** (per Ryan; a JSON blob is not a db). cw-s3's
  Functions have **no `site/wrangler.toml` and no `site/migrations/` today** —
  both are **CP'd from gcs, adapted** (see IaC below), not authored from scratch:
  - `site/wrangler.toml` ← gcs's, changing `name = oa-cw-s3-usage`, its own
    `database_id`, `ACCESS_AUD` → the cw Access app `4c463052`, `STAFF_DOMAIN`
    kept. Adds the `[[d1_databases]]` binding cw-s3 lacks.
  - `site/migrations/` — a **minimal mark+plan+sweep set** borrowed from gcs's
    *final* table shapes, authored as a fresh `0001..0004` — **not** a replay of
    gcs's 23-step history (which threads gcs's access-log/grants/index-footer/
    user-emails auth+attribution axis cw-s3 doesn't have). **Authored 2026-09-11:**
    - `0001_marks.sql` — `marks` (current resolved, PK prefix) + `mark_log`
      (append-only history), keep-axis only (gcs `0007`+`0010`, owner dropped).
      **Default state = unmarked** (absence of a row — neither swept nor
      affirmatively kept); `keep`/`keep_last_ckpt` are affirmative "protect"
      signals, not the default. Nothing is swept without an explicit `sweep` mark
      (deepest-mark-wins). Differs from gcs (absence = delete-eligible, kept safe
      by an owner-slice); cw-s3's safety is explicit-`sweep`-only + the curated
      plan. Plain prefixes (regex + the raw/expanded split later).
    - `0002_plans.sql` — **`plans` + `plan_items`**: the deletion plan as a
      first-class object (see *Plan model* below). An admin curates `sweep`-marked
      prefixes into a named, draftable plan; multiple plans coexist independently.
    - `0003_deletions.sql` — `deletion_runs` + `deletion_bands` (gcs `0015`+`0023`
      folded, `buckets` inline), **`plan_id` FK to `plans`** + `manifest` (the
      run's gs:// object-level snapshot; was gcs's `plan` dir). GCS→CAIOS:
      `undo_state`/`undo_deadline` (remove delete markers before the hold expires)
      **plus** `purge_state` (`none`/`pending`/`done`) for the two-stage
      versioned-bucket space reclaim.
    - `0004_admin.sql` — `admin_emails` (the sweep-dispatch gate) + `admin_edits`
      (audit), from gcs `0008`. **Not** carried: `sweep_approvals` (`0016`/`0017`)
      — its owner-verification raison d'être is absent without an owner axis; the
      `sweep` mark → plan curation → admin dispatch is already a multi-step gate.
      Add later only if a 4-eyes (marker ≠ dispatcher) control is wanted.
      `mark_totals` (`0012`/`0022`) also skipped — it caches totals priced against
      the path-index footer, which cw-s3 lacks (loads whole `tree.json`).
- **FE** — adapted from gcs, owner columns dropped:
  - mark axis: `MarkControls` (mark a prefix keep/sweep), a mark column in
    `ChildrenTable`, mark legend + treemap mark outlines (capability present).
  - **Plans view**: list plans (open/closed), open one to see + curate its items
    (add from the current `sweep`-marked set, remove), close it. First-class,
    admin-scoped writes.
  - `SweepPage` (per plan): dry-run / real-delete dispatch against the open plan +
    a runs table (paginated, per-bucket cut, live progress, failed-job "why" fold —
    all in gcs's version), scoped to that plan's `deletion_runs`.
  - sweeper identity chip from CF Access; the dispatch + plan-write buttons gated
    on the admin allowlist.

## IaC — CP the CF/D1 layer; the Pulumi program already has a design

**Correction to an earlier read:** this is *not* from-scratch. Two levels:

**1. In-repo IaC → CP from gcs (parity surface).** The CF/D1 config lives in the
repo: gcs has `site/wrangler.toml` + `site/migrations/*.sql`; cw-s3 has neither.
These CP-adapt as above (name / `database_id` / `ACCESS_AUD` / a mark+sweep
migrations subset). **Fold both into the `.claude/cp.yml` parity surfaces + the
ledger**, so `scripts/branch-audit gcs cw-s3` renders the delta as the intended
small patch (name/id/AUD/table-subset) rather than a whole-file absence — the
git-didi visibility Ryan asked for.

**2. Provisioning-as-code → gcs's `specs/cf-iac.md` (written 2026-08-28) already
covers it, for both branches.** It recommends *one* Pulumi program in
`~/c/oa/ops` (existing setup: `gs://oa-pulumi` backend, KMS secrets), **two
stacks parameterized by store — `gcs` and `cw-s3`** — importing today's live
resources so the first `pulumi up` is a no-op diff. Nothing is provisioned-as-code
for *either* branch yet; running that plan is the ops-session job. So no new
`ops/specs/` spec is needed — this spec just consumes `cf-iac.md`; the sweep-new
resources to add there:
- **CoreWeave**: enable **bucket versioning** on `marin-us-east-02a` (Phase 0
  prereq; `aws s3api put-bucket-versioning` against `cwobject.com` — a Pulumi
  `command`/local-exec, no CW provider).
- **Cloudflare** (`@pulumi/cloudflare`, per `cf-iac.md`): the **D1 database**
  (new; `PagesProject`/`D1Database`/`ZeroTrustAccessApplication` are all in the
  cw-s3 stack the spec already scopes) + admin allowlist on Access app `4c463052`.
- **GCP** — the genuinely net-new half (today imperative `job/*.sh` + hand-edited
  cron bodies on *both* branches, the drift `cf-iac.md` flags): the sweep Batch
  dispatch SA + IAM (`batch.jobs.create`, act-as `gcs-usage-job`), the Scheduler
  body-as-code, Secret Manager (CW AWS creds).

**IaC vs wrangler split (Ryan: prefer everything IaC, a mix is fine).** The clean
division, and what gcs already does: **Pulumi owns resource *lifecycle*** (the D1
database, Pages project, Access app + its admin-allowlist policy, GCP SAs/
Scheduler/Secret Manager, CW bucket versioning — all importable). **wrangler owns
the deploy/schema *layer*** that Pulumi models poorly: `site/wrangler.toml` (the
deploy-time bindings/vars, read by `wrangler pages deploy`) and `wrangler d1
migrations apply` (SQL schema evolution). So `wrangler.toml` + `site/migrations/`
staying wrangler-driven is not an IaC gap — it's the same tool boundary gcs uses;
Pulumi still declares the D1 database *exists*, wrangler fills its schema.

## Phases

- **Phase 0 (prereqs)**: CP `site/wrangler.toml` + author the mark+sweep
  `site/migrations/` (in-repo, cw-s3 session); add both to the CP parity surfaces.
  Provision the D1 database + enable `marin-us-east-02a` versioning (ops/ session
  per `cf-iac.md`, or a documented one-time op to unblock dev).
  - **In-repo authoring DONE 2026-09-11**: `site/wrangler.toml` (aud + name +
    db-name set; `database_id` blank pending create), `site/migrations/0001–0004`
    (marks, plans, deletions, admin), parity surfaces + ledger updated.
  - **Provisioning — D1 DONE 2026-09-12**: `oa-cw-s3-usage-db`
    (`7f1e1326-b879-4ecd-8621-846621c24f36`, region ENAM) created + migrations
    0001–0004 applied `--remote`; `database_id` committed to `wrangler.toml`.
  - **Provisioning — remaining three DONE 2026-09-15**:
    - **CoreWeave bucket versioning ENABLED** on `marin-us-east-02a`
      (`put_bucket_versioning Status=Enabled` via boto3 — virtual addressing,
      creds from Secret Manager; helper `tmp/cw-versioning.py`: status /
      `--enable` / `--lifecycle` probe). Real sweeps now pass the preflight.
      **Direction chosen 2026-09-15: time-boxed soft deletes** = versioning +
      `NoncurrentVersionExpiration` (old version dropped after N days) +
      `ExpiredObjectDeleteMarker` (leftover marker removed) — deleted objects
      evaporate on their own after the window; `sweep purge` becomes
      belt-and-braces. **Acceptance test in flight** (started 12:09Z): a scoped
      rule `cw-sweep-lifecycle-test` on `tmp/cw-sweep-lifecycle-test/` with
      `NoncurrentDays=1` (S3's minimum — no sub-day granularity) was accepted by
      CAIOS (read-modify-write; Marin's 10 rules verified unchanged, backup in
      `tmp/`), and 2 seeded objects were deleted into noncurrent-version +
      delete-marker pairs. `tmp/cw-versioning.py check` from ~2026-09-16 12:09Z
      (possibly +1 day for midnight-UTC rounding + scheduler slack): versions
      gone ⇒ expiry honored, markers gone ⇒ marker cleanup honored. If honored,
      promote to a bucket-wide rule sized to the undo window (needs Marin's
      go-ahead — it also restores reclaim for their `tmp/ttl=<N>d/` TTLs); if not,
      fall back to (B) suspend versioning + soften the preflight.
    - **`GCP_SA_KEY` Pages secret SET** on `oa-cw-s3-usage`: a fresh JSON key for
      the existing `gcs-usage-dispatch@oa-internal-450019` SA (already scoped
      `roles/batch.jobsEditor` + `serviceAccountUser` on `gcs-usage-job` — the same
      submit identity gcs uses). Key creation is classifier-blocked for the agent
      (Ryan ran it); the agent piped it into `wrangler pages secret put` and deleted
      the local material.
    - **`IMAGE:cw` REBUILT** (`job/build.sh`, Cloud Build 7m12s) — now carries the
      `gcs-usage sweep` CLI. The first attempt failed: the branch's `Dockerfile`
      never got the `COPY packages/treemap` line from the 9/7 `@rdub/treemap`
      split (gcs's did), so a clean install resolved a stale `@rdub/treemap` and
      the site's `tsc` failed on `Tiling`/`borderWidth`. Fixed (`3c7dc52`, now
      matches gcs); the image had not rebuilt successfully on this branch since the
      split.
- **Phase 1 (backend, cw-s3)**: `marin sweep {manifest,execute}` with the boto3
  delete path + versioning-guard preflight (`manifest` expands a plan JSON);
  `/api/plans/*` (CRUD) + `/api/sweep/{dispatch,jobs,stop}` (dispatch takes a
  `plan_id`); apply the D1 migrations. Dry-run end-to-end on Batch first.
  - **Slice 1 DONE 2026-09-11** (`marin/src/gcs_usage/sweep.py` + `sweep manifest`
    CLI, tests): the plan model (`Plan`/`load_plan`, deepest-mark-wins, keep
    carves out sweep), the CAIOS boto3 client + `versioning_enabled` guard, and
    the manifest builder (curated plan × the pinned layer-2 parquet → object-level
    `manifest/<bucket>.parquet` + `plan-summary.json`, via DuckDB). Overwrite guard
    keys off (size, mtime) — layer-2 has no ETag. gcs's `sweep_plan.py`
    classify/vote/owner brain is dropped wholesale (the curated plan is the
    eligibility decision).
  - **Slice 2 DONE 2026-09-11** (`execute_plan` + `sweep execute` CLI, tests): the
    executor. Lists each curated root (`list_objects_v2`), merge-checks every live
    object against the reviewed manifest, deletes only reviewed keys whose (size,
    mtime) still match — new keys since scan left alone (`drift_new`), changed ones
    `skipped_overwritten`, vanished `skipped_gone`. `delete_objects` ≤1000/batch on
    a thread pool; real delete writes a recoverable delete marker; **versioning
    preflight** refuses `--for-real` unless Status=Enabled. Writes a decision-log
    parquet + `<deleted|would-delete>-summary.json` (with per-band aggregates for
    `deletion_bands`). Does **not** write D1 — the Functions layer reflects the
    summary in (a cw-s3 simplification vs gcs's Batch-writes-D1-directly). Tests
    (5) cover dry decisions-without-delete, real delete, versioning refusal, and
    delete-failure recording via a fake S3.
  - **Slice 2 follow-ups (deferred)**: gcs's scale/robustness extras not yet
    ported — LPT root scheduling + the in-memory bisection index (manifest is
    dict-loaded; fine for curated subsets, revisit for giant plans), streamed
    multi-part logs, progress daemon + `STOP`-file. `undo`/`purge` are Phase 3.
  - **Slice 3 DONE 2026-09-11** (the API, `site/functions/`, typechecks clean):
    - `_lib/auth.ts` — lean identity from the edge Access JWT (`Cf-Access-Jwt-Assertion`,
      aud/exp checked) + `requireAdmin`/`requireViewer`; admin = `admin_emails` D1
      OR the staff domain (implicit, so there's a bootstrap admin without a PII
      seed row in this public repo). Much leaner than gcs's `@open-athena/auth`.
    - `_lib/gcp.ts` — `gcpToken` (SA-key JWT → OAuth, WebCrypto, memoized) + Batch/
      CW infra consts; ported from gcs minus the multi-region fan-out (CW is
      single-region us-central1).
    - `_lib/plans.ts` — prefix normalization, the admin-edit audit, and
      `snapshotPlan` (plan items + current keep marks → the executor's plan.json).
    - `api/plans/[[path]].ts` — CRUD (list/create/get/close/add-items/remove-items;
      reads viewer, writes admin, items editable only while open).
    - `api/sweep/dispatch.ts` — snapshot → write plan.json to GCS → submit the
      Batch job (mirrors `cw-batch-submit.sh`: `IMAGE:cw`, `gcs-usage-job` SA, CAIOS
      secrets, data-bucket FUSE mount) running `sweep manifest && execute` → insert
      the in-progress `deletion_runs` row.
    - `api/sweep/jobs.ts` — list `cw-sweep-*` jobs + **reflect** each terminal run's
      gs:// summary into `deletion_runs`/`deletion_bands` (idempotent; the D1-write
      side of the Batch-writes-gs://-only split).
    - `api/sweep/stop.ts` — cancel a running job (Batch `:cancel`; the clean
      STOP-drain awaits the executor's STOP-poll follow-up).
    - Tooling: added `site/functions/tsconfig.json` + `@cloudflare/workers-types`
      and `tsc -p functions` to the site `build` (CI now typechecks Functions —
      cw-s3 didn't before, a gap vs gcs).
  - `/api/marks` + `/api/whoami` DONE 2026-09-11 (mark-axis write path; the FE
    admin flag get-identity doesn't carry).
- **Phase 2 (FE, cw-s3)** — DONE 2026-09-11 (typechecks, CIC'd on localhost;
  data flows need D1):
  - **2a `SweepPage.tsx` + `/sweep` route** — the console: plans list + create,
    a plan panel (curate items add/remove while open, dry/armed-real dispatch
    against a chosen scan), and a runs table (live state polled from
    `/api/sweep/jobs`, deleted/gone/overwritten, failed "why" fold, undo/purge/
    stop actions gated on run state + admin). Owner axis absent — the curated
    plan is the gate. Plain fetch/useState.
  - **2b mark axis** — `marks.ts` (`MarkIndex` deepest-wins + `useMarks`) + a
    settable keep/klc/sweep dot column in `ChildrenTable` (own/inherited/available)
    + an "add sweep-marked" button on a plan. `/api/whoami` gates the controls.
  - **2c treemap mark outlines** DONE 2026-09-12: a `marks` prop on `Treemap`
    drives an `outlineGroups` overlay — union frames where a cell's mark differs
    from its parent's (uniform subtree = one frame) + an outline legend key.
    CIC'd (throwaway dev → local-D1 wrangler): a swept subtree renders one red
    union frame. **Still deferred**: the `MarkControls` tooltip widget (the
    children-table dots cover marking); not needed for the workflow.
- **Phase 3 (cw-s3)** — DONE 2026-09-11: `sweep undo` (remove delete markers) +
  `sweep purge` (drop noncurrent versions after the hold) engine + tests, and
  `/api/sweep/{undo,purge}` (Batch-dispatched, state-gated, reflected via
  `/api/sweep/jobs`). Sweeper identity + admin gate = `_lib/auth.ts` + `admin_emails`.
  Deferred: the executor's clean `STOP`-drain (stop.ts cancels the job today).

## Open questions

- Hold period before purge (gcs uses ≥7d soft-delete). Match, or configurable?
- ~~Admin allowlist source: reuse the CF Access app's identity list, or a
  dedicated D1 table?~~ **Resolved 2026-09-11 → D1** (`admin_emails`, gcs-proven,
  editable, removals bite per request). CF Access stays the sign-in gate + the
  sweeper-identity source (`get-identity`); D1 decides who among the authenticated
  may dispatch. Seed it from the Access identity list if convenient.
- Path-prefix owner rules on cw-s3: worth surfacing as a (non-authoritative)
  owner hint now, or defer until a real W&B/Iris integration?

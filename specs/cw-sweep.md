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
| **mark / fate axis** (keep / sweep a prefix) | full | **full, unchanged** — owner-independent. The treemap outline capability already landed (`0a18858`); this wires the mark UI that consumes it. |

So the only genuinely-absent axis is *owner-from-integrations*. Everything the
delete flow needs (mark a prefix, who did it, plan, execute, undo) is available.

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
- **no native lifecycle** for expiring noncurrent versions was found; cleanup is
  manual (`rclone backend cleanup-hidden`, or `list-object-versions` → delete by
  `versionId`).

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

- **Dispatch** — `site/functions/api/sweep/{dispatch,jobs,stop}.ts`, adapted from
  gcs. Submits a **GCP Batch** job running `IMAGE:cw` (not gcs's snapshot image),
  command = `marin sweep manifest -d <date> -S <buckets> -o <plan>` +
  `marin sweep execute <buckets> [--for-real] <plan>`. Plan/log land in
  `gs://oa-gcs-usage-dvx/sweep/cw/runs/<id>` (part files, per gcs). Region: the CW
  bucket is single-region (us-east-02a) — one Batch region, no `batchRegionFor`
  fan-out.
- **Executor** — a new sweep module in cw-s3's **`marin/`** engine (cw-s3 has
  none today; gcs's lives in `gcs-usage/src/gcs_usage/{mark,sweep_plan,sweep_exec}.py`).
  Adapt those, swapping the **GCS client → boto3** (`delete_objects` in ≤1000-key
  batches, per-item retries, durable decision log). Reuse gcs's manifest/bisection/
  LPT-root-scheduling/preflight design. Preflight adds the **versioning-enabled
  check** above. `.[s3]` extras already ship in `IMAGE:cw`.
- **Marks + runs store** — **D1** (per Ryan; a JSON blob is not a db). cw-s3's
  Functions have **no `site/wrangler.toml` and no `site/migrations/` today** —
  both are **CP'd from gcs, adapted** (see IaC below), not authored from scratch:
  - `site/wrangler.toml` ← gcs's, changing `name = oa-cw-s3-usage`, its own
    `database_id`, `ACCESS_AUD` → the cw Access app `4c463052`, `STAFF_DOMAIN`
    kept. Adds the `[[d1_databases]]` binding cw-s3 lacks.
  - `site/migrations/` — a **minimal mark+sweep set** borrowed from gcs's *final*
    table shapes (`marks` `0007`, `deletions` `0015`, `sweep_approvals`
    `0016`/`0017`, `deletion_runs_buckets` `0023`, `allowed_emails` `0008`,
    `actions_ledger` `0010`), authored as a fresh 0001..N — **not** a replay of
    gcs's 23-step history (which threads gcs's access-log/grants/index-footer/
    user-emails auth+attribution axis cw-s3 doesn't have).
- **FE** — adapted from gcs, owner columns dropped:
  - mark axis: `MarkControls` (mark a prefix keep/sweep), a mark column in
    `ChildrenTable`, mark legend + treemap mark outlines (capability present).
  - `SweepPage`: plan / dry-run / real-delete dispatch + a runs table (paginated,
    per-bucket cut, live progress, failed-job "why" fold — all in gcs's version).
  - sweeper identity chip from CF Access; the dispatch buttons gated on the admin
    allowlist.

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

## Phases

- **Phase 0 (prereqs)**: CP `site/wrangler.toml` + author the mark+sweep
  `site/migrations/` (in-repo, cw-s3 session); add both to the CP parity surfaces.
  Provision the D1 database + enable `marin-us-east-02a` versioning (ops/ session
  per `cf-iac.md`, or a documented one-time op to unblock dev).
- **Phase 1 (backend, cw-s3)**: `marin sweep {manifest,plan,execute}` with the
  boto3 delete path + versioning-guard preflight; `/api/sweep/{dispatch,jobs,stop}`;
  apply the D1 migrations. Dry-run end-to-end on Batch first.
- **Phase 2 (FE, cw-s3)**: mark axis (`MarkControls`, `ChildrenTable` mark column,
  mark legend/outlines) + `SweepPage`, adapted from gcs (no owner).
- **Phase 3 (cw-s3)**: sweeper identity + admin gate, `sweep undo` (remove delete
  markers), and the **purge/GC** job for noncurrent versions after the hold.

## Open questions

- Hold period before purge (gcs uses ≥7d soft-delete). Match, or configurable?
- Admin allowlist source: reuse the CF Access app's identity list, or a dedicated
  `allowed_emails` D1 table (gcs's approach)?
- Path-prefix owner rules on cw-s3: worth surfacing as a (non-authoritative)
  owner hint now, or defer until a real W&B/Iris integration?

// POST /api/sweep/dispatch — launch a sweep run on GCP Batch from the /sweep
// console (specs/cw-sweep.md). Admin only.
//
// Body: { plan_id, mode: 'dry' | 'real', date: <scan id> }. Snapshots the plan's
// items into plan.json (in the run dir on GCS), submits a Batch job running the
// cw image's `sweep manifest && sweep execute`, and records an in-progress
// `deletion_runs` row (finished_ts NULL) so the run shows in the console from
// the moment it's submitted. The executor writes only gs:// artifacts; the run's
// totals are reflected into D1 later by /api/sweep/jobs.
//
// Auth to GCP: `_lib/gcp.ts` (the `GCP_SA_KEY` Pages secret — Batch submit +
// actAs the job SA + GCS write for the plan.json drop).
import type { D1Database } from "@cloudflare/workers-types"
import { type Ctx, type Env as AuthEnv, json, requireAdmin } from "../../_lib/auth.js"
import {
  batchJobsUrl, BATCH_REGION, CW_BUCKET, CW_ENDPOINT, CW_IMAGE, DATA_BUCKET,
  gcpToken, JOB_SA, secretRef,
} from "../../_lib/gcp.js"
import { snapshotPlan } from "../../_lib/plans.js"

type Env = AuthEnv & { DB?: D1Database }

const DATE_RE = /^\d{4}-\d{2}-\d{2}(?:T\d{4})?$/

export const onRequestPost = async (ctx: Ctx & { env: Env }): Promise<Response> => {
  const gated = await requireAdmin(ctx)
  if (gated instanceof Response) return gated
  if (!ctx.env.DB) return json({ error: "plans store not configured (no D1 binding)" }, 503)
  if (!ctx.env.GCP_SA_KEY) return json({ error: "dispatch not configured (GCP_SA_KEY secret missing)" }, 503)
  const db = ctx.env.DB

  const body = (await ctx.request.json().catch(() => null)) as
    | { plan_id?: number; mode?: string; date?: string } | null
  const planId = body?.plan_id
  const mode = body?.mode
  const date = body?.date
  if (!Number.isInteger(planId)) return json({ error: "plan_id required" }, 400)
  if (mode !== "dry" && mode !== "real") return json({ error: "mode must be 'dry' or 'real'" }, 400)
  if (!date || !DATE_RE.test(date)) return json({ error: "date must be a scan id (YYYY-MM-DD[THHMM])" }, 400)

  const snapshot = await snapshotPlan(db, planId!)
  if (!snapshot) return json({ error: "no such plan" }, 404)
  if (!snapshot.sweep.length) return json({ error: "plan has no items to sweep" }, 400)

  const ts = new Date().toISOString().replace(/[-:]/g, "").replace("T", "-").slice(0, 15).toLowerCase()
  const jobId = `cw-sweep-${mode}-${ts}z`
  const runGs = `gs://${DATA_BUCKET}/sweep/cw/runs/${jobId}`
  const runMnt = `/gcs/${DATA_BUCKET}/sweep/cw/runs/${jobId}`

  const token = await gcpToken(ctx.env.GCP_SA_KEY)

  // Drop plan.json into the run dir (the executor reads it via the FUSE mount).
  const planObj = encodeURIComponent(`sweep/cw/runs/${jobId}/plan.json`)
  const up = await fetch(
    `https://storage.googleapis.com/upload/storage/v1/b/${DATA_BUCKET}/o?uploadType=media&name=${planObj}`,
    { method: "POST", headers: { authorization: `Bearer ${token}`, "content-type": "application/json" }, body: JSON.stringify(snapshot) },
  )
  if (!up.ok) return json({ error: "plan.json write failed", status: up.status, detail: (await up.text()).slice(0, 300) }, 500)

  const script = [
    "set -euo pipefail",
    `RUN="${runMnt}"`,
    `gcs-usage sweep manifest --plan "$RUN/plan.json" -d "$SWEEP_DATE" -o "$RUN"`,
    `gcs-usage sweep execute ${mode === "real" ? "--for-real " : ""}"$RUN"`,
  ].join("\n")

  const spec = {
    taskGroups: [{
      taskCount: 1,
      taskSpec: {
        runnables: [{
          container: {
            imageUri: CW_IMAGE,
            entrypoint: "/bin/bash",
            commands: ["-c", script],
            volumes: [`/mnt/disks/gcs/${DATA_BUCKET}:/gcs/${DATA_BUCKET}:rw`],
          },
        }],
        computeResource: { cpuMilli: 8000, memoryMib: 16000 },
        maxRetryCount: 0,
        // Deletes are network-bound round trips to the bucket; a big plan can run
        // for hours. 24 h ceiling (the scan itself is ~30 min).
        maxRunDuration: "86400s",
        volumes: [{
          gcs: { remotePath: DATA_BUCKET },
          mountPath: `/mnt/disks/gcs/${DATA_BUCKET}`,
          mountOptions: ["--implicit-dirs"],
        }],
        environment: {
          variables: {
            JOB_ID: jobId,
            SWEEP_DATE: date,
            DATA_BUCKET,
            CW_BUCKET,
            CW_ENDPOINT,
            AWS_DEFAULT_REGION: "us-east-1",
            AWS_EC2_METADATA_DISABLED: "true",
            DT_S3_ADDRESSING_STYLE: "virtual",
          },
          secretVariables: {
            AWS_ACCESS_KEY_ID: secretRef("cw-s3-access-key-id"),
            AWS_SECRET_ACCESS_KEY: secretRef("cw-s3-secret-access-key"),
          },
        },
      },
    }],
    allocationPolicy: {
      instances: [{ policy: { machineType: "n2-standard-8", bootDisk: { type: "pd-balanced", sizeGb: "100" } } }],
      serviceAccount: { email: JOB_SA },
      location: { allowedLocations: [`regions/${BATCH_REGION}`] },
    },
    logsPolicy: { destination: "CLOUD_LOGGING" },
  }

  const r = await fetch(`${batchJobsUrl()}?job_id=${jobId}`, {
    method: "POST",
    headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
    body: JSON.stringify(spec),
  })
  const text = await r.text()
  if (!r.ok) {
    console.error("batch submit failed", r.status, text.slice(0, 2000))
    let detail: unknown = { body: text.slice(0, 1000) }
    try { detail = JSON.parse(text) } catch { /* keep raw */ }
    return json({ error: `batch submit failed (${r.status})`, status: r.status, detail }, 500)
  }

  const head = (await db.prepare("SELECT coalesce(max(id), 0) AS h FROM mark_log").first<{ h: number }>())?.h ?? 0
  await db.prepare(`
    INSERT INTO deletion_runs (run_id, plan_id, manifest, scan, head, exec_head, actor, mode, started_ts, log_dir)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
  `).bind(jobId, planId, runGs, date, head, head, gated.email, mode, Math.floor(Date.now() / 1000), runGs).run()

  return json({ job_id: jobId, plan_id: planId, mode, date, run: runGs, by: gated.email })
}

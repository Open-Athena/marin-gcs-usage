// Plans store (specs/cw-sweep.md): the first-class deletion plan. An admin
// curates `sweep`-marked prefixes into a named plan; a dispatch snapshots the
// plan's items into the plan.json the Batch executor consumes. D1 CRUD +
// snapshot + the admin-edit audit trail live here; the HTTP surface is
// api/plans/[[path]].ts.
import type { D1Database } from "@cloudflare/workers-types"
import { CW_BUCKET } from "./gcp.js"

// A plan prefix stored as `s3://<bucket>/<path>/` (matching the marks convention);
// normalized to a relative key prefix only at snapshot time.
const PREFIX_RE = /^(?!\/)(?![.]{1,2}\/)[^\\]+\/$/

export interface PlanRow {
  id: number
  name: string
  note: string | null
  state: "open" | "closed"
  created_by: string
  created_ts: number
  closed_ts: number | null
}

/** `s3://bucket/a/b/` or `/a/b` or `a/b` -> `a/b/` (relative, trailing slash). */
export function relPrefix(raw: string, bucket: string = CW_BUCKET): string {
  let s = raw.trim().replace(/^s3:\/\//, "")
  if (s.startsWith(`${bucket}/`)) s = s.slice(bucket.length + 1)
  s = s.replace(/^\/+/, "")
  if (!s.endsWith("/")) s += "/"
  return s
}

/** Canonical stored form of a plan-item prefix: `s3://<bucket>/<path>/`. */
export function canonicalPrefix(raw: string, bucket: string = CW_BUCKET): string | null {
  const rel = relPrefix(raw, bucket)
  if (!PREFIX_RE.test(rel)) return null
  return `s3://${bucket}/${rel}`
}

export async function audit(
  db: D1Database,
  tbl: string,
  pk: string,
  action: "insert" | "update" | "delete",
  who: string,
  oldJson: unknown,
  newJson: unknown,
): Promise<void> {
  await db
    .prepare("INSERT INTO admin_edits (tbl, pk, action, who, ts, old_json, new_json) VALUES (?, ?, ?, ?, ?, ?, ?)")
    .bind(tbl, pk, action, who, Math.floor(Date.now() / 1000),
      oldJson == null ? null : JSON.stringify(oldJson),
      newJson == null ? null : JSON.stringify(newJson))
    .run()
}

/** Snapshot a plan into the executor's plan.json: relative sweep prefixes (the
 * plan's items) + relative keep prefixes (the current keep/keep_last_ckpt marks,
 * which carve out at manifest time). Returns null if the plan is missing. */
export async function snapshotPlan(db: D1Database, planId: number): Promise<
  { plan_id: number; name: string; bucket: string; sweep: string[]; keep: string[] } | null
> {
  const plan = await db.prepare("SELECT id, name FROM plans WHERE id = ?").bind(planId).first<{ id: number; name: string }>()
  if (!plan) return null
  const items = await db.prepare("SELECT prefix FROM plan_items WHERE plan_id = ? ORDER BY prefix").bind(planId).all<{ prefix: string }>()
  const keeps = await db.prepare("SELECT prefix FROM marks WHERE keep IN ('keep', 'keep_last_ckpt') ORDER BY prefix").all<{ prefix: string }>()
  return {
    plan_id: planId,
    name: plan.name,
    bucket: CW_BUCKET,
    sweep: items.results.map(r => relPrefix(r.prefix)),
    keep: keeps.results.map(r => relPrefix(r.prefix)),
  }
}

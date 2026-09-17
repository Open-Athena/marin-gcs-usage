// Plans store (specs/sweep-plan-union.md, seam 1): the first-class deletion
// plan. An admin (or, per the store's eligibility policy, any viewer over their
// own slice) curates prefixes into a named plan; a dispatch snapshots the
// plan's items into the executor's plan and runs gcs's `sweep manifest` /
// `sweep execute` against them. D1 CRUD + the snapshot + the admin-edit audit
// trail live here; the HTTP surface is api/plans/[[path]].ts.
//
// gcs adaptation of cw-s3's `_lib/plans.ts`: prefixes are stored fully
// qualified (`gs://marin-<bucket>/<path>/`, the marks convention here), so
// there is no relativize step, and a plan MAY span buckets — gcs's executor
// takes multiple `-b` buckets and `batchRegionFor` picks the region, unlike
// cw's one-bucket-per-run executor. Keep carve-outs are resolved from the
// actions ledger at manifest time (not a flat marks table), so the snapshot
// carries only the sweep set.
import type { D1Database } from '@cloudflare/workers-types'
import { PREFIX_RE } from './resolve.js'

export interface PlanRow {
  id: number
  name: string
  note: string | null
  state: 'open' | 'closed'
  created_by: string
  created_ts: number
  closed_ts: number | null
}

/** The marin bucket a stored prefix names (`gs://marin-<bucket>/…` → `marin-<bucket>`), or null. */
export function bucketOf(prefix: string): string | null {
  const m = /^gs:\/\/(marin-[a-z0-9-]+)\//.exec(prefix)
  return m ? m[1] : null
}

/** Canonical stored form of a plan-item prefix: `gs://marin-<bucket>/<path>/`.
 * Tolerates a missing trailing slash; rejects anything not a marin dir prefix. */
export function canonicalPrefix(raw: string): string | null {
  let s = raw.trim()
  if (!s.endsWith('/')) s += '/'
  return PREFIX_RE.test(s) && s.length <= 1024 ? s : null
}

/** The distinct buckets a plan's item prefixes live in, sorted — for the
 * dispatch region and the plan's display; gcs does not refuse a multi-bucket plan. */
export function planBuckets(prefixes: string[]): string[] {
  return [...new Set(prefixes.map(bucketOf).filter((b): b is string => b != null))].sort()
}

/** The executor's sweep set for a plan: the plan's item prefixes + the buckets
 * they span. Keep carve-outs are resolved from the ledger at manifest time, so
 * they are not part of the snapshot. Null when the plan is missing. */
export async function snapshotPlan(
  db: D1Database,
  planId: number,
): Promise<{ plan_id: number; name: string; buckets: string[]; sweep: string[] } | null> {
  const plan = await db.prepare('SELECT id, name FROM plans WHERE id = ?').bind(planId).first<{ id: number; name: string }>()
  if (!plan) return null
  const items = await db.prepare('SELECT prefix FROM plan_items WHERE plan_id = ? ORDER BY prefix').bind(planId).all<{ prefix: string }>()
  const sweep = items.results.map(r => r.prefix)
  return { plan_id: planId, name: plan.name, buckets: planBuckets(sweep), sweep }
}

/** Append an admin-edit audit row (`admin_edits`), mirroring the marks/allowlist trail. */
export async function audit(
  db: D1Database,
  tbl: string,
  pk: string,
  action: 'insert' | 'update' | 'delete',
  who: string,
  oldJson: unknown,
  newJson: unknown,
): Promise<void> {
  await db
    .prepare('INSERT INTO admin_edits (tbl, pk, action, who, ts, old_json, new_json) VALUES (?, ?, ?, ?, ?, ?, ?)')
    .bind(tbl, pk, action, who, Math.floor(Date.now() / 1000),
      oldJson == null ? null : JSON.stringify(oldJson),
      newJson == null ? null : JSON.stringify(newJson))
    .run()
}

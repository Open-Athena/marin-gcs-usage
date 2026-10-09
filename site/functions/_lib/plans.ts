// Plans store (specs/staged-delete.md): the first-class deletion plan. Trash
// gestures stage prefixes into the shared open plan (or an admin curates a
// named one); a dispatch snapshots the plan's items into the plan.json the
// Batch executor consumes. D1 CRUD + snapshot + the admin-edit audit trail
// live here; the HTTP surface is api/plans/[[path]].ts.
import type { D1Database } from "@cloudflare/workers-types"

// A plan prefix stored as `<scheme><bucket>/<path>/` (the deployment's
// `STORE_SCHEME`: `s3://`, `gs://`, `file:///`); normalized to a relative key
// prefix only at snapshot time.
const PREFIX_RE = /^(?!\/)(?![.]{1,2}\/)[^\\]+\/$/
const SCHEME_RE = /^[a-z0-9]+:\/\//

/** The deployment's prefix convention: the URI scheme its stored prefixes
 * carry and the buckets its scan covers (first = primary). From `[vars]`
 * (`STORE_SCHEME`, default `s3://`; `STORE_BUCKETS`). No buckets = no shape
 * (null): the plans and sweep routes refuse until the deployment names them. */
export interface PrefixShape {
  scheme: string
  buckets: readonly string[]
}
export function prefixShape(env: { STORE_SCHEME?: string; STORE_BUCKETS?: string }): PrefixShape | null {
  const buckets = (env.STORE_BUCKETS ?? '').split(',').map(s => s.trim()).filter(Boolean)
  return buckets.length ? { scheme: env.STORE_SCHEME ?? 's3://', buckets } : null
}
/** The 503 message when `prefixShape` is null. */
export const NO_SHAPE = 'not configured (STORE_BUCKETS unset)'

/** The bucket a raw prefix names — `<scheme><b>/…` or `<b>/…` for a scanned
 * bucket — else the primary. The treemap's paths start with the bucket, so
 * a plan item under a second bucket's `<b>/…` must not canonicalize under
 * the primary (specs/done/cw-multi-bucket.md §4). */
export function bucketOf(raw: string, buckets: readonly string[]): string {
  const s = raw.trim().replace(SCHEME_RE, "").replace(/^\/+/, "")
  // `*` (`STORE_BUCKETS = "*"`): every top-level segment is a root — a
  // filesystem-root capture's `Applications`, `Users`, … — so a prefix's
  // bucket is its first segment, never a fallback to the primary.
  if (buckets.includes("*")) return s.split("/")[0]
  return buckets.find(b => s === b || s.startsWith(`${b}/`)) ?? buckets[0]
}

export interface PlanRow {
  id: number
  name: string
  note: string | null
  state: "open" | "closed"
  created_by: string
  created_ts: number
  closed_ts: number | null
}

/** `s3://bucket/a/b/` (any scheme) or `/a/b` or `a/b` -> `a/b/` (relative, trailing slash). */
export function relPrefix(raw: string, bucket: string): string {
  // Leading slashes go first: a `file:///` URI leaves `/Users/…` after the
  // scheme, which must still match its bucket.
  let s = raw.trim().replace(SCHEME_RE, "").replace(/^\/+/, "")
  if (s.startsWith(`${bucket}/`)) s = s.slice(bucket.length + 1)
  if (!s.endsWith("/")) s += "/"
  return s
}

/** Canonical stored form of a plan-item prefix: `<scheme><bucket>/<path>/`
 * in the deployment's shape, the bucket resolved from the raw (`bucketOf`
 * over the shape's buckets) unless given. */
export function canonicalPrefix(raw: string, shape: PrefixShape, bucket: string = bucketOf(raw, shape.buckets)): string | null {
  const rel = relPrefix(raw, bucket)
  if (!PREFIX_RE.test(rel)) return null
  return `${shape.scheme}${bucket}/${rel}`
}

/** `a` covers `b`: the same prefix, or `b` lies under it. Canonical prefixes
 * end in `/`, so `s3://b/a/` covers `s3://b/a/x/` and not `s3://b/ab/`. */
export const covers = (a: string, b: string): boolean => b === a || b.startsWith(a.endsWith('/') ? a : `${a}/`)

/** Some member of `set` other than `p` itself covers `p` (`covers`). Every
 * such member is a cut of `p` at one of its `/`s (with or without the slash),
 * so this probes those cuts — O(|p|), not O(|set|). */
export function coveredBy(p: string, set: ReadonlySet<string>): boolean {
  for (let i = p.indexOf('/'); i >= 0; i = p.indexOf('/', i + 1)) {
    if (set.has(p.slice(0, i))) return true
    if (i + 1 < p.length && set.has(p.slice(0, i + 1))) return true
  }
  return false
}

/** The prefixes of `all` that no other member covers — the set a delete
 * actually acts on. A staged dir and a staged descendant of it would count
 * (and delete) the descendant twice; the no-nesting rule keeps one. */
export function uncovered(all: readonly string[]): string[] {
  const set = new Set(all)
  return all.filter(p => !coveredBy(p, set))
}

/** Stage prefixes for deletion — the opt-in trash model's proposal step
 * (`STAGING` deployments): append them to a shared open plan, creating one
 * ("Staged") if none is open. Any full viewer may stage; an admin approves +
 * dispatches later. One `stage_batches` row per gesture carries the memo
 * (a fact about the action), and every prefix in the call points at it — the
 * 1:many an admin reads back as "trashed together by X: <memo>". A re-staged
 * prefix keeps its first batch (`INSERT OR IGNORE`).
 *
 * `asOf` is the scan the gesture stages against (a scan date; the caller
 * validates it): every new item records it as `plan_items.as_of`, and the
 * executor deletes only objects present, unchanged, in both that scan and the
 * dispatch scan. A re-staged prefix keeps its first `as_of`. The gesture is
 * one D1 transaction: on any failure nothing of it is written.
 *
 * No nesting (`covers`): a prefix already under a staged ancestor is skipped
 * (`covered`), and staging an ancestor absorbs its staged descendants
 * (`absorbed` — removed from the plan; the ancestor now names them). Returns
 * the plan + batch ids and what happened, or an error for a malformed prefix. */
export async function stageItems(
  db: D1Database,
  rawPrefixes: string[],
  who: string,
  note: string | null,
  shape: PrefixShape,
  asOf: string | null,
): Promise<{ plan_id: number; batch_id: number; staged: string[]; covered: string[]; absorbed: string[]; as_of: string | null } | { error: string }> {
  const prefixes: string[] = []
  for (const r of rawPrefixes) {
    const c = canonicalPrefix(r, shape)
    if (!c) return { error: `bad prefix ${JSON.stringify(r)}` }
    if (!prefixes.includes(c)) prefixes.push(c)
  }
  if (!prefixes.length) return { error: 'prefixes required' }
  const ts = Math.floor(Date.now() / 1000)
  const open = await db.prepare("SELECT id FROM plans WHERE state = 'open' ORDER BY created_ts DESC LIMIT 1").first<{ id: number }>()
  const have = open
    ? (await db.prepare('SELECT prefix FROM plan_items WHERE plan_id = ?').bind(open.id).all<{ prefix: string }>()).results.map(r => r.prefix)
    : []
  const { staged, covered, absorbed } = planStaging(have, uncovered(prefixes))

  // Every write below is ONE `db.batch` — one D1 transaction, all or nothing
  // (a request cut off mid-gesture used to leave a partial batch). Ids minted
  // inside it are read back as `MAX(id)`: both tables are AUTOINCREMENT, so
  // the row just inserted holds the largest id, and D1 serializes
  // transactions, so no other writer interleaves. The item inserts and the
  // absorbed deletes are one statement each over `json_each` (no per-prefix
  // round trip, no bind-count limit).
  const planId = open ? String(Number(open.id)) : '(SELECT MAX(id) FROM plans)'
  const batchId = '(SELECT MAX(id) FROM stage_batches)'
  const edit = 'INSERT INTO admin_edits (tbl, pk, action, who, ts, old_json, new_json)'
  const stmts = []
  if (!open) {
    stmts.push(
      db.prepare("INSERT INTO plans (name, note, state, created_by, created_ts) VALUES ('Staged', NULL, 'open', ?, ?)").bind(who, ts),
      db.prepare(`${edit} SELECT 'plans', CAST(${planId} AS TEXT), 'insert', ?, ?, NULL, ?`)
        .bind(who, ts, JSON.stringify({ name: 'Staged', auto: true })),
    )
  }
  stmts.push(db.prepare(`INSERT INTO stage_batches (plan_id, note, created_by, created_ts) VALUES (${planId}, ?, ?, ?)`).bind(note, who, ts))
  if (absorbed.length) {
    stmts.push(db.prepare(`DELETE FROM plan_items WHERE plan_id = ${planId} AND prefix IN (SELECT value FROM json_each(?))`).bind(JSON.stringify(absorbed)))
  }
  // A re-staged prefix keeps its row — first batch, first `as_of` (`OR IGNORE`).
  stmts.push(db.prepare(
    `INSERT OR IGNORE INTO plan_items (plan_id, prefix, batch_id, added_by, added_ts, as_of)
     SELECT ${planId}, value, ${batchId}, ?, ?, ? FROM json_each(?)`,
  ).bind(who, ts, asOf, JSON.stringify(staged)))
  stmts.push(db.prepare(`${edit} SELECT 'plan_items', CAST(${planId} AS TEXT), 'insert', ?, ?, ?, json_set(?, '$.batch_id', ${batchId})`)
    .bind(who, ts, absorbed.length ? JSON.stringify({ absorbed }) : null, JSON.stringify({ staged, covered, batch_id: 0, note, as_of: asOf })))
  stmts.push(db.prepare(`SELECT ${planId} AS plan_id, ${batchId} AS batch_id`))
  const out = await db.batch<{ plan_id: number; batch_id: number }>(stmts)
  const ids = out[out.length - 1].results[0]
  return { plan_id: ids.plan_id, batch_id: ids.batch_id, staged, covered, absorbed, as_of: asOf }
}

/** Fold stage batches `ids` into batch `into`: one logical staging that went
 * in as several requests (chunked) reads, and reviews, as the one batch it is.
 * All of them must be in plan `planId` (open) and staged by the same person;
 * `into` takes their items, the earliest `created_ts`, and `note` if given
 * (else keeps its own); the folded batches are deleted. Items keep their own
 * `added_ts` / `as_of`. One D1 transaction, with an `admin_edits` row
 * (`update`, `merged_into`) that `emptiedBatches` replays so later fates land on
 * `into`. */
export async function mergeBatches(
  db: D1Database,
  planId: number,
  into: number,
  ids: number[],
  note: string | null,
  who: string,
): Promise<{ into: number; merged: number[]; items: number } | { error: string; status: number }> {
  const from = [...new Set(ids)].filter(i => i !== into)
  if (!from.length) return { error: 'ids: at least one batch other than `into`', status: 400 }
  const plan = await db.prepare('SELECT state FROM plans WHERE id = ?').bind(planId).first<{ state: string }>()
  if (!plan) return { error: 'no such plan', status: 404 }
  if (plan.state !== 'open') return { error: 'plan is closed', status: 409 }
  const all = [into, ...from]
  const rows = (await db.prepare('SELECT id, plan_id, created_by FROM stage_batches WHERE id IN (SELECT value FROM json_each(?))')
    .bind(JSON.stringify(all)).all<{ id: number; plan_id: number; created_by: string }>()).results
  const missing = all.filter(i => !rows.some(r => r.id === i && r.plan_id === planId))
  if (missing.length) return { error: `not batches of plan ${planId}: ${missing.join(', ')}`, status: 404 }
  const by = new Set(rows.map(r => r.created_by.toLowerCase()))
  if (by.size > 1) return { error: `batches were staged by different people (${[...by].join(', ')})`, status: 409 }
  const ts = Math.floor(Date.now() / 1000)
  const fromJson = JSON.stringify(from)
  const n = await db.prepare('SELECT COUNT(*) AS n FROM plan_items WHERE plan_id = ? AND batch_id IN (SELECT value FROM json_each(?))')
    .bind(planId, fromJson).first<{ n: number }>()
  await db.batch([
    db.prepare('UPDATE plan_items SET batch_id = ? WHERE plan_id = ? AND batch_id IN (SELECT value FROM json_each(?))').bind(into, planId, fromJson),
    db.prepare(`UPDATE stage_batches SET created_ts = (SELECT MIN(created_ts) FROM stage_batches WHERE id IN (SELECT value FROM json_each(?))),
      note = COALESCE(?, note) WHERE id = ?`).bind(JSON.stringify(all), note, into),
    db.prepare('DELETE FROM stage_batches WHERE id IN (SELECT value FROM json_each(?))').bind(fromJson),
    db.prepare("INSERT INTO admin_edits (tbl, pk, action, who, ts, old_json, new_json) VALUES ('plan_items', ?, 'update', ?, ?, ?, ?)")
      .bind(String(planId), who, ts, JSON.stringify({ batches: from }), JSON.stringify({ merged_into: into, note })),
  ])
  return { into, merged: from, items: n?.n ?? 0 }
}

/** The no-nesting rule for one gesture against a plan's current items (pure):
 * `staged` = the new prefixes no existing item covers, `covered` = the new
 * prefixes an existing item already names, `absorbed` = existing items a new
 * prefix covers (to remove). A prefix already in the plan stays as it is. */
export function planStaging(have: readonly string[], add: readonly string[]): { staged: string[]; covered: string[]; absorbed: string[] } {
  const haveSet = new Set(have)
  const staged: string[] = []
  const covered: string[] = []
  for (const p of add) {
    if (haveSet.has(p)) { staged.push(p); continue }
    if (coveredBy(p, haveSet)) covered.push(p)
    else staged.push(p)
  }
  const stagedSet = new Set(staged)
  const absorbed = have.filter(h => !stagedSet.has(h) && coveredBy(h, stagedSet))
  return { staged, covered, absorbed }
}

export interface StageBatchRow { id: number; plan_id: number; note: string | null; created_by: string; created_ts: number }
/** `as_of`: the scan the item was staged against (NULL = before `as_of` existed). */
export interface PlanItemRow { prefix: string; note: string | null; added_by: string; added_ts: number; batch_id: number | null; as_of: string | null }

/** A `plan_items` audit row (`admin_edits`, `tbl = 'plan_items'`, pk = the plan id). */
export interface PlanEdit { action: string; old_json: string | null; new_json: string | null }

/** A stage batch with no items left, and what became of what it staged
 * (specs/staged-runs.md): `absorbed` into later batches (a gesture that staged
 * an ancestor), `unstaged` (taken back), and `covered` — named by an already
 * staged ancestor when it was staged, so never added. `staged` = the prefixes
 * it added. */
export interface EmptiedBatch extends StageBatchRow {
  staged: number
  covered: number
  absorbed: { into: number; n: number }[]
  unstaged: number
  /** Taken out by the real run that deleted them (`unstageDeleted`). */
  deleted: number
}

/** The plan's emptied stage batches, by replaying its `plan_items` audit trail
 * (pure): who held each prefix, and which gesture took it away. */
export function emptiedBatches(batches: readonly StageBatchRow[], items: readonly PlanItemRow[], edits: readonly PlanEdit[]): EmptiedBatch[] {
  const live = new Set(items.map(i => i.batch_id))
  const parse = (s: string | null): Record<string, unknown> => { try { return (s ? JSON.parse(s) : {}) as Record<string, unknown> } catch { return {} } }
  const strs = (v: unknown): string[] => Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : []
  const owner = new Map<string, number | null>()
  const fate = new Map<number, { staged: number; covered: number; absorbed: Map<number, number>; unstaged: number; deleted: number }>()
  const of = (b: number) => { let f = fate.get(b); if (!f) fate.set(b, f = { staged: 0, covered: 0, absorbed: new Map(), unstaged: 0, deleted: 0 }); return f }
  for (const e of edits) {
    const o = parse(e.old_json)
    const n = parse(e.new_json)
    if (e.action === 'insert') {
      const batch = typeof n.batch_id === 'number' ? n.batch_id : null
      for (const p of strs(o.absorbed)) {
        const was = owner.get(p)
        if (was != null && batch != null) { const a = of(was).absorbed; a.set(batch, (a.get(batch) ?? 0) + 1) }
        owner.delete(p)
      }
      // A stage gesture's `staged` (a re-staged prefix keeps its first batch);
      // an admin's curation (`prefixes`, no batch).
      for (const p of [...strs(n.staged), ...strs(n.prefixes)]) {
        if (owner.has(p)) continue
        owner.set(p, batch)
        if (batch != null) of(batch).staged++
      }
      if (batch != null) of(batch).covered += strs(n.covered).length
    } else if (e.action === 'update' && typeof n.merged_into === 'number') {
      // `mergeBatches`: the folded batches' prefixes (and fates) now belong to `into`.
      const into = n.merged_into
      const folded = new Set(Array.isArray(o.batches) ? o.batches.filter((x): x is number => typeof x === 'number') : [])
      for (const [p, b] of owner) if (b != null && folded.has(b)) owner.set(p, into)
      for (const b of folded) {
        const f = fate.get(b)
        if (!f) continue
        const t = of(into)
        t.staged += f.staged; t.covered += f.covered; t.unstaged += f.unstaged; t.deleted += f.deleted
        for (const [k, v] of f.absorbed) t.absorbed.set(k, (t.absorbed.get(k) ?? 0) + v)
        fate.delete(b)
      }
      // An earlier batch absorbed into a folded one was absorbed into `into`.
      for (const f of fate.values()) {
        for (const [k, v] of [...f.absorbed]) {
          if (!folded.has(k)) continue
          f.absorbed.delete(k)
          f.absorbed.set(into, (f.absorbed.get(into) ?? 0) + v)
        }
      }
    } else if (e.action === 'delete') {
      const byRun = typeof o.deleted_by === 'string'
      for (const p of strs(o.prefixes)) {
        const was = owner.get(p)
        if (was != null) { if (byRun) of(was).deleted++; else of(was).unstaged++ }
        owner.delete(p)
      }
    }
  }
  return batches.filter(b => !live.has(b.id)).map(b => {
    const f = fate.get(b.id) ?? { staged: 0, covered: 0, absorbed: new Map<number, number>(), unstaged: 0, deleted: 0 }
    return { ...b, staged: f.staged, covered: f.covered, absorbed: [...f.absorbed].map(([into, n]) => ({ into, n })).sort((x, y) => x.into - y.into), unstaged: f.unstaged, deleted: f.deleted }
  })
}

/** A plan with its items (memo joined from the stage batch where the deployment
 * stages), its stage batches (`emptied`: those with no items left, with their
 * fate) and its runs — what `/staged` and `/api/plans/:id` render. `staging` =
 * the `stage_batches` table exists here. */
export async function planDetail(db: D1Database, id: number, staging: boolean): Promise<{ plan: PlanRow; items: PlanItemRow[]; batches: StageBatchRow[]; emptied: EmptiedBatch[]; runs: Record<string, unknown>[] } | null> {
  const plan = await db.prepare('SELECT * FROM plans WHERE id = ?').bind(id).first<PlanRow>()
  if (!plan) return null
  const items = staging
    ? await db.prepare(
      `SELECT i.prefix, COALESCE(i.note, b.note) AS note, i.added_by, i.added_ts, i.batch_id, i.as_of
       FROM plan_items i LEFT JOIN stage_batches b ON b.id = i.batch_id
       WHERE i.plan_id = ? ORDER BY i.added_ts DESC, i.prefix`,
    ).bind(id).all<PlanItemRow>()
    : await db.prepare('SELECT prefix, note, added_by, added_ts, NULL AS batch_id, as_of FROM plan_items WHERE plan_id = ? ORDER BY added_ts DESC, prefix').bind(id).all<PlanItemRow>()
  const batches = staging
    ? (await db.prepare('SELECT * FROM stage_batches WHERE plan_id = ? ORDER BY created_ts DESC').bind(id).all<StageBatchRow>()).results
    : []
  // Every run, not just this plan's: plans are bookkeeping, and /staged's runs
  // table is the deployment's run history.
  const runs = await db.prepare('SELECT * FROM deletion_runs ORDER BY started_ts DESC LIMIT 500').all<Record<string, unknown>>()
  const edits = staging
    ? (await db.prepare("SELECT action, old_json, new_json FROM admin_edits WHERE tbl = 'plan_items' AND pk = ? ORDER BY id").bind(String(id)).all<PlanEdit>()).results
    : []
  return { plan, items: items.results, batches, emptied: emptiedBatches(batches, items.results, edits), runs: runs.results }
}

/** One run's D1 record and its per-band rows (largest first) — the run detail
 * `/staged` expands a run row into (`GET /api/plans/run?id=`). */
export async function runDetail(db: D1Database, runId: string): Promise<{ run: Record<string, unknown>; bands: Record<string, unknown>[] } | null> {
  const run = await db.prepare('SELECT * FROM deletion_runs WHERE run_id = ?').bind(runId).first<Record<string, unknown>>()
  if (!run) return null
  const bands = await db.prepare('SELECT prefix, bytes, objects, gone, overwritten, drift_new_objects, undone_objects FROM deletion_bands WHERE run_id = ? ORDER BY bytes DESC, prefix').bind(runId).all<Record<string, unknown>>()
  return { run, bands: bands.results }
}

/** The shared open plan trash gestures land in (the newest open one), or null. */
export async function openPlanId(db: D1Database): Promise<number | null> {
  const row = await db.prepare("SELECT id FROM plans WHERE state = 'open' ORDER BY created_ts DESC LIMIT 1").first<{ id: number }>()
  return row?.id ?? null
}

/** A plan whose items name more than one bucket: the executor runs against one
 * bucket per run, so dispatch refuses it (400) rather than guessing. */
export class PlanSpansBuckets extends Error {
  constructor(public readonly buckets: string[]) {
    super(`plan spans buckets: ${buckets.join(", ")}`)
  }
}

/** The one bucket a plan's (canonical) item prefixes live in, and the items
 * relative to it; a plan with no items is the primary's (`buckets[0]`). */
export function planBucket(prefixes: string[], buckets: readonly string[]): { bucket: string; sweep: string[] } {
  const named = [...new Set(prefixes.map(p => bucketOf(p, buckets)))]
  if (named.length > 1) throw new PlanSpansBuckets(named)
  const bucket = named[0] ?? buckets[0]
  return { bucket, sweep: prefixes.map(p => relPrefix(p, bucket)) }
}

/** A plan's canonical items grouped by bucket: bucket -> the items relative
 * to it, buckets and items sorted. The gcs executor takes several `-b`, so a
 * plan there MAY span buckets — this is `planBucket` without the refusal
 * (cw keeps `planBucket`: one bucket per run). */
export function planBuckets(prefixes: string[], buckets: readonly string[]): Record<string, string[]> {
  const by: Record<string, string[]> = {}
  for (const p of prefixes) {
    const b = bucketOf(p, buckets)
    ;(by[b] ??= []).push(relPrefix(p, b))
  }
  return Object.fromEntries(Object.keys(by).sort().map(b => [b, [...new Set(by[b])].sort()]))
}

/** The plan.json a multi-bucket dispatch drops in the run dir for
 * `dt-cloud sweep manifest --plan`: the plan's items in canonical form
 * (`<scheme><bucket>/<path>/`, the executor groups them by bucket itself) and
 * the buckets they name (the run's `-b` cut). The plan is the whole intent:
 * nothing carves out. */
export interface PlanBucketsSnapshot {
  plan_id: number
  name: string
  sweep: string[]
  buckets: string[]
  /** Each item's `as_of` scan, keyed like `sweep` (items staged before
   * `as_of` existed are absent: the dispatch scan stands in). The executor
   * deletes only objects unchanged in both that scan and the dispatch scan. */
  as_of: Record<string, string>
}

/** A plan's items with their `as_of` scans, prefix-sorted. */
async function planItems(db: D1Database, planId: number): Promise<{ prefix: string; as_of: string | null }[]> {
  return (await db.prepare("SELECT prefix, as_of FROM plan_items WHERE plan_id = ? ORDER BY prefix").bind(planId).all<{ prefix: string; as_of: string | null }>()).results
}

export async function snapshotPlanBuckets(db: D1Database, planId: number, shape: PrefixShape): Promise<PlanBucketsSnapshot | null> {
  const plan = await db.prepare("SELECT id, name FROM plans WHERE id = ?").bind(planId).first<{ id: number; name: string }>()
  if (!plan) return null
  const items = await planItems(db, planId)
  const sweep = items.map(r => r.prefix)
  const as_of = Object.fromEntries(items.filter(r => r.as_of).map(r => [r.prefix, r.as_of!]))
  return { plan_id: planId, name: plan.name, sweep, buckets: Object.keys(planBuckets(sweep, shape.buckets)), as_of }
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

/** Take out of plan `planId` every item a finished real run of it fully
 * deleted since the item was staged: a band with no objects written or
 * rewritten during the run (`drift_new_objects`, `overwritten`), not undone.
 * A drifted or overwritten prefix stays queued (its new objects were never
 * reviewed; the next dry-run sees them). One `admin_edits` row per run
 * (`deleted_by`), so `/staged` shows those batches as deleted, not unstaged.
 * An undo deliberately does not re-stage: restoring means "keep these".
 * Idempotent; returns the prefixes removed. */
export async function unstageDeleted(db: D1Database, planId: number): Promise<string[]> {
  const { results } = await db.prepare(`
    SELECT i.prefix, min(r.run_id) AS run_id FROM plan_items i
    JOIN deletion_bands b ON b.prefix = i.prefix
    JOIN deletion_runs r ON r.run_id = b.run_id
    WHERE i.plan_id = ? AND r.plan_id = i.plan_id AND r.mode = 'real' AND r.finished_ts IS NOT NULL
      AND r.started_ts >= i.added_ts AND b.drift_new_objects = 0 AND b.overwritten = 0 AND b.undone_objects = 0
    GROUP BY i.prefix ORDER BY i.prefix
  `).bind(planId).all<{ prefix: string; run_id: string }>()
  const byRun = new Map<string, string[]>()
  for (const r of results) byRun.set(r.run_id, [...(byRun.get(r.run_id) ?? []), r.prefix])
  for (const [run, prefixes] of byRun) {
    await db.batch(prefixes.map(p => db.prepare('DELETE FROM plan_items WHERE plan_id = ? AND prefix = ?').bind(planId, p)))
    await audit(db, 'plan_items', String(planId), 'delete', `run:${run}`, { prefixes, deleted_by: run }, null)
  }
  return results.map(r => r.prefix)
}

/** `unstageDeleted` on the open plan, if any: the executors' job polls call
 * it every time, so a run's deletions settle even when its finish was
 * reflected before this existed (or by another reader). */
export async function settleOpenPlan(db: D1Database): Promise<string[]> {
  const id = await openPlanId(db)
  return id == null ? [] : unstageDeleted(db, id)
}

/** A run control (stop / undo / purge) into the audit log: who, when, and the
 *  job it launched (null for a stop, which writes a flag file). The run's own
 *  row keeps only state (`undo_state`, …), not who changed it. */
export const auditRunControl = (
  db: D1Database,
  control: 'stop' | 'undo' | 'purge',
  target: string,
  who: string,
  jobId: string | null,
): Promise<void> => audit(db, 'deletion_runs', target, 'update', who, null, { control, job_id: jobId })

/** Snapshot a plan into the executor's plan.json: the plan's one bucket
 * (`planBucket`; throws `PlanSpansBuckets`) and the relative sweep prefixes
 * (the plan's items — the whole intent; nothing carves out), with each
 * item's `as_of` scan (keyed by the relative prefix). Returns null if the plan
 * is missing. */
export async function snapshotPlan(db: D1Database, planId: number, shape: PrefixShape): Promise<
  { plan_id: number; name: string; bucket: string; sweep: string[]; as_of: Record<string, string> } | null
> {
  const plan = await db.prepare("SELECT id, name FROM plans WHERE id = ?").bind(planId).first<{ id: number; name: string }>()
  if (!plan) return null
  const items = await planItems(db, planId)
  const { bucket, sweep } = planBucket(items.map(r => r.prefix), shape.buckets)
  // keyed by the relative prefix, as `sweep` is (`planBucket` keeps order)
  const as_of = Object.fromEntries(items.flatMap((r, i) => r.as_of ? [[sweep[i], r.as_of]] : []))
  return { plan_id: planId, name: plan.name, bucket, sweep, as_of }
}

// ── The real-deletion gate (specs/done/staged-slack.md) ─────────────────────────

const hex = (buf: ArrayBuffer): string => [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, "0")).join("")

/** A run's item set as a short digest: sha-256 of the sorted canonical
 * prefixes joined by `\n`, first 16 hex; order-independent. Every executor
 * records it on the runs it starts (`deletion_runs.plan_digest`). */
export async function planDigest(prefixes: readonly string[]): Promise<string> {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode([...prefixes].sort().join("\n")))
  return hex(buf).slice(0, 16)
}

/** The `deletion_runs` columns the gate and the Slack thread read. */
export interface RunRow {
  run_id: string
  mode: "dry" | "real"
  scan: string
  actor: string
  started_ts: number
  finished_ts: number | null
  deleted_bytes: number
  deleted_objects: number
  skipped_gone: number
  skipped_overwritten: number
  plan_digest: string | null
  undo_deadline?: number | null
  log_dir?: string | null
}

export type Gate =
  | { ok: true; dry: RunRow }
  | { ok: false; reason: string }

/** May the item set with digest `digest` (`items` prefixes) be really deleted
 * now? Yes only after a *finished* dry-run of exactly this set, and with no
 * run of the plan in flight. `runs` are the plan's, any order. A run with no
 * digest (NULL: from before digests, or not yet reflected) or the empty one
 * (it ended without a result) never opens the gate. */
export function realGate(runs: readonly RunRow[], digest: string, items: number): Gate {
  if (!items) return { ok: false, reason: "the plan is empty" }
  const live = runs.find(r => r.finished_ts == null)
  if (live) return { ok: false, reason: `a ${live.mode} run is in progress (${live.run_id})` }
  const dry = [...runs].filter(r => r.mode === "dry" && !!r.plan_digest && r.plan_digest === digest).sort((a, b) => b.started_ts - a.started_ts)[0]
  if (!dry) {
    const any = runs.some(r => r.mode === "dry")
    return { ok: false, reason: any ? "the plan changed since the last dry-run; dry-run it again" : "no dry-run of this plan yet" }
  }
  return { ok: true, dry }
}

/** A run an executor's reflection just closed: `ok` = with its result
 * (totals); not ok = it ended without one (its Batch job stopped first). */
export interface FinishedRun { run_id: string; ok: boolean }

/** A plan's runs, as the gate reads them. */
export async function planRuns(db: D1Database, planId: number): Promise<RunRow[]> {
  return (await db.prepare("SELECT * FROM deletion_runs WHERE plan_id = ?").bind(planId).all<RunRow>()).results
}

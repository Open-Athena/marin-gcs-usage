/** One view = "path + scan + scope + pixel budget", answered from the index
 * tiers (specs/view-serving.md §1–2). Shared by `/api/subtree` (the map),
 * `/api/todo` (the review backlog) and anything else that wants a folded tree.
 *
 * Treemap area is proportional to bytes, so "everything this canvas can draw
 * under P" is one predicate: b ≥ P.b · minArea / (w·h), attenuated per level.
 * Every tier holds rows `(path, depth, usr, b, o, …)`, descendant-
 * inclusive, sorted `(depth, path)` (or by usr for a user lens), so a query
 * is row-group-pruned range reads, a threshold filter, and a nested-tree
 * assembly with `(other)` = parent − Σ kept children (exact by subtraction).
 *
 * Tier planning: the coarse tiers keep only paths whose subtree clears an
 * absolute floor F_E (the job writes E = 16, 20, 24). Descendant-inclusive
 * bytes are monotone, so when the query's threshold is ≥ F_E the tier's rows
 * are a superset of what the query keeps and the answer is identical to the
 * floor-free tier's — just read from a few row groups instead of the whole
 * file. The planner picks the coarsest such tier; a path below every floor
 * (or a scan without coarse tiers) reads the floor-free tier.
 */
import type { Env } from './auth.js'
import { type IndexHandle, type Lens, openIndex, readRows, type Row } from './index.js'

export const MIN_AREA_DEFAULT = 12 // px² of the smallest legible cell (~3×4)
// Each nesting level below the query root loses canvas to chrome (title bars,
// insets) — and deeper detail is one drill away regardless. The per-node
// threshold doubles per level: thr(depth) = thr · ATTEN^(depth − dP − 1).
export const ATTEN_DEFAULT = 2
export const QUANT = 128 // px quantization for w/h → cache-key stability
const HARD_CAP = 50_000 // response nodes; way above any real canvas budget
// Coarse tiers the job writes, coarsest first (viz.py COARSE_EXPS).
const COARSE_EXPS = [16, 20, 24]

export type ViewNode = { n: string; b: number; o: number; c?: ViewNode[]; f?: number } & Record<string, unknown>

export interface ViewOpts {
  date: string
  path: string
  w: number
  h: number
  minArea: number
  atten: number
  lens?: Lens
  /** Absolute byte threshold instead of the pixel-budget one (e.g. the to-do
   * backlog's min bytes). Still attenuated per level by `atten`. */
  threshold?: number
}

export interface View {
  tree: ViewNode
  /** Which tier answered: `coarse<E>` or `fine` (floor-free). */
  tier: string
  /** How that tier's metadata was read: `d1` (footer in D1) or `footer`. */
  index: string
  threshold: number
  nodes: number
  truncated: boolean
}

export class NotFound extends Error {}
/** The scan has no synced index for this lens (older scans) — a client-side
 * fallback question, not a server fault. */
export class LensUnavailable extends Error {}

interface Agg {
  b: number
  o: number
  wts: number
  wb: number
  a: number | null // subtree-max last-read epoch day (read lens); null = never read
  cb: Record<string, number>
  ub: Record<string, number>  // per-user bytes; unclaimed = b − Σ ub
}

const newAgg = (): Agg => ({ b: 0, o: 0, wts: 0, wb: 0, a: null, cb: {}, ub: {} })

function merge(a: Agg, r: Row): void {
  a.b += r.b
  a.o += r.o
  a.wts += r.wts
  a.wb += r.wb
  if (r.a != null) a.a = a.a == null ? r.a : Math.max(a.a, r.a)
  for (const [k, v] of [['2', r.c2], ['3', r.c3], ['4', r.c4]] as [string, number][]) {
    if (v) a.cb[k] = (a.cb[k] ?? 0) + v
  }
  if (r.usr) a.ub[r.usr] = (a.ub[r.usr] ?? 0) + r.b
}

function subtract(parent: Agg, kids: Agg[]): Agg {
  const out = newAgg()
  out.a = parent.a // max isn't subtractable; the residual's read day ≤ parent's
  const sum = (f: (a: Agg) => number) => kids.reduce((s, k) => s + f(k), 0)
  out.b = parent.b - sum(a => a.b)
  out.o = Math.max(0, parent.o - sum(a => a.o))
  out.wts = parent.wts - sum(a => a.wts)
  out.wb = Math.max(0, parent.wb - sum(a => a.wb))
  for (const key of ['cb', 'ub'] as const) {
    for (const [k, v] of Object.entries(parent[key])) {
      const r = v - kids.reduce((s, kid) => s + (kid[key][k] ?? 0), 0)
      if (r > 0) out[key][k] = r
    }
  }
  return out
}

function display(a: Agg): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  if (a.wb) out.d = Math.round(a.wts / a.wb / 86400)
  if (a.a != null) out.a = a.a
  const desc = (m: Record<string, number>) =>
    Object.fromEntries(Object.entries(m).sort((x, y) => y[1] - x[1]))
  if (Object.keys(a.cb).length) out.cb = desc(a.cb)
  if (Object.keys(a.ub).length) {
    out.us = Object.entries(a.ub).sort((x, y) => y[1] - x[1]).map(([u, b]) => [u, b])
  }
  return out
}

/** Open an index variant; `null` when the scan has no such tier synced. */
async function tryOpen(env: Env, date: string, variant: string): Promise<IndexHandle | null> {
  try {
    return await openIndex(env, date, variant)
  } catch (e) {
    if (String((e as Error).message).includes('not synced')) return null
    throw e
  }
}

const floorOf = (h: IndexHandle): number | null => (h.mode === 'd1' ? h.floor : null)

export async function buildView(env: Env, o: ViewOpts): Promise<View> {
  const { date, path, w, h, minArea, atten, lens } = o
  const dP = path === '' ? 0 : path.split('/').length
  const sort = lens ? 'user' : 'path'
  const readRoot = (idx: IndexHandle) =>
    path === '' ? readRows(idx, 1, 1, '', '￿', undefined, lens) : readRows(idx, dP, dP, path, path, undefined, lens)

  // Root aggregate P.b (and P's own us for the response root) from the
  // coarsest tier that has P at all — the same numbers in every tier.
  const tiers: { name: string; idx: IndexHandle }[] = []
  for (const e of COARSE_EXPS) {
    const idx = await tryOpen(env, date, `coarse${e}${sort === 'path' ? '' : `-${sort}`}`)
    if (idx && floorOf(idx) != null) tiers.push({ name: `coarse${e}`, idx })
  }
  let rootRows: Row[] = []
  let fine: IndexHandle | null = null
  for (const t of tiers) {
    rootRows = await readRoot(t.idx)
    if (rootRows.length) break
  }
  if (!rootRows.length) {
    fine = await openFine(env, date, sort, lens)
    rootRows = await readRoot(fine)
    if (!rootRows.length) throw new NotFound(path)
  }
  const rootAgg = newAgg()
  for (const r of rootRows) merge(rootAgg, r)
  const threshold = o.threshold ?? (rootAgg.b * minArea) / (w * h)
  const thrAt = (depth: number) => threshold * atten ** Math.max(0, depth - dP - 1)

  // The coarsest tier whose floor the query can't see below.
  let pick: { name: string; idx: IndexHandle } | null = null
  for (const t of tiers) {
    if (threshold >= floorOf(t.idx)!) { pick = t; break }
  }
  if (!pick) pick = { name: 'fine', idx: fine ?? await openFine(env, date, sort, lens) }
  const idx = pick.idx

  // Everything under P that this canvas can draw. Depth is unbounded — the
  // attenuated threshold bounds both row count and depth by construction.
  const pLo = path === '' ? '' : path + '/'
  const pHi = path === '' ? '￿' : path + '0' // '0' sorts just past '/'
  const rows = await readRows(idx, dP + 1, 1e9, pLo, pHi, thrAt, lens)
  const aggs = new Map<string, Agg>()
  const aggDepth = new Map<string, number>()
  for (const r of rows) {
    let a = aggs.get(r.path)
    if (!a) {
      aggs.set(r.path, (a = newAgg()))
      aggDepth.set(r.path, r.depth)
    }
    merge(a, r)
  }
  const kept = [...aggs.entries()].filter(([p, a]) => a.b >= thrAt(aggDepth.get(p)!))
  const truncated = kept.length > HARD_CAP
  // Descendant-inclusive bytes are monotone (ancestor ≥ descendant), so the
  // kept set — and even its top-N-by-bytes truncation — is ancestor-closed:
  // every kept path's parent is kept (or is P itself). Linking is trivial.
  const keptMap = new Map(
    (truncated ? kept.sort((a, b) => b[1].b - a[1].b).slice(0, HARD_CAP) : kept),
  )

  const nodeOf = (name: string, a: Agg): ViewNode => ({
    n: name,
    b: Math.round(a.b),
    o: Math.round(a.o),
    ...display(a),
  })
  const parentOf = (p: string): string => {
    const cut = p.lastIndexOf('/')
    return cut === -1 ? '' : p.slice(0, cut)
  }
  const kidsOf = new Map<string, string[]>()
  // Sub-threshold child count per parent. A coarse tier only holds paths
  // above its floor, so this counts the folded children it can see; the
  // (other) bytes themselves are exact regardless (parent − Σ kept).
  const foldedOf = new Map<string, number>()
  for (const [p] of keptMap) {
    const key = keptMap.has(parentOf(p)) ? parentOf(p) : path
    const arr = kidsOf.get(key)
    if (arr) arr.push(p)
    else kidsOf.set(key, [p])
  }
  for (const [p, a] of aggs) {
    if (keptMap.has(p)) continue
    if (a.b >= thrAt(aggDepth.get(p)!)) continue // truncation casualty, not a fold
    const par = parentOf(p)
    const key = keptMap.has(par) ? par : par === path || (path === '' && !p.includes('/')) ? path : null
    if (key !== null) foldedOf.set(key, (foldedOf.get(key) ?? 0) + 1)
  }
  const build = (p: string, a: Agg): ViewNode => {
    const node = nodeOf(p === path ? (path === '' ? 'marin GCS' : path.split('/').pop()!) : p.split('/').pop()!, a)
    const childPaths = kidsOf.get(p) ?? []
    if (!childPaths.length) return node
    const kids = childPaths
      .map(cp => build(cp, keptMap.get(cp)!))
      .sort((x, y) => y.b - x.b)
    const kidAggs = childPaths.map(cp => keptMap.get(cp)!)
    const rest = subtract(a, kidAggs)
    node.c = kids
    if (rest.b > thrAt((aggDepth.get(childPaths[0]!) ?? dP + 1))) {
      node.c.push({ ...nodeOf('(other)', rest), f: foldedOf.get(p) ?? 0 } as ViewNode)
    }
    return node
  }
  const tree = build(path, rootAgg)
  return { tree, tier: pick.name, index: idx.mode, threshold, nodes: keptMap.size, truncated }
}

async function openFine(env: Env, date: string, sort: string, lens?: Lens): Promise<IndexHandle> {
  try {
    return await openIndex(env, date, sort)
  } catch (e) {
    if (lens && String((e as Error).message).includes('not synced')) throw new LensUnavailable(sort)
    throw e
  }
}

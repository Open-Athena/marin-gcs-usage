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
import { type FateAxis, fateScope, type FateScope } from './fates.js'
import { type IndexHandle, type Lens, openIndex, readRects, readRows, type Rect, type Row } from './index.js'
import { ownerLens, type OwnerLens } from './owners.js'
import { nameFilter, type NamePred, ownerOk, type OwnerScope } from './scope.js'
import { markTotals } from './totals.js'

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
  // --- the page's scope axes (specs/view-serving.md §2) -------------------
  /** `o=claimed|unclaimed`: the owner axis's pools (a user is `lens`). */
  owner?: OwnerScope
  /** `k=` ⊆ keep/sweep/unmarked: bytes under an allowed fate, per node. */
  fates?: ReadonlySet<FateAxis>
  /** `q=`: name filter over the read rows' paths (see `scope.ts`). */
  query?: NamePred
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
  /** With `query`: the outermost matching paths (what a bulk action targets). */
  matches?: string[]
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

/** Scale an aggregate to `frac` of itself — a scope's share of a node: bytes
 * exactly, the rest (objects, per-user/class splits, written-time weights)
 * proportionally, which assumes the scope is spread like the node's mix. */
function scale(a: Agg, frac: number): Agg {
  if (frac === 1) return a
  const out = newAgg()
  out.b = a.b * frac
  out.o = a.o * frac
  out.wts = a.wts * frac
  out.wb = a.wb * frac
  out.a = a.a
  for (const key of ['cb', 'ub'] as const) for (const [k, v] of Object.entries(a[key])) out[key][k] = v * frac
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

/** Just P's scoped aggregate for one scan — the size-over-time chart's point
 * (`/api/series`): the root read of a view, from the coarsest tier that has
 * P, without folding anything under it. */
export async function readRootAgg(env: Env, o: { date: string; path: string; lens?: Lens; owner?: OwnerScope }): Promise<{ b: number; o: number } | null> {
  const { date, path, lens, owner } = o
  const dP = path === '' ? 0 : path.split('/').length
  const readRoot = (idx: IndexHandle, l?: Lens) =>
    path === '' ? readRows(idx, 1, 1, '', '￿', undefined, l) : readRows(idx, dP, dP, path, path, undefined, l)
  // P's row from the coarsest tier of `sort` that has it; null = no tier.
  const rootRows = async (sort: string, l?: Lens): Promise<Row[] | null> => {
    for (const e of COARSE_EXPS) {
      const idx = await tryOpen(env, date, `coarse${e}${sort === 'path' ? '' : `-${sort}`}`)
      if (!idx) continue
      const rows = await readRoot(idx, l)
      if (rows.length) return rows
    }
    const fine = await tryOpen(env, date, sort)
    return fine ? readRoot(fine, l) : null
  }
  const ol = lens ? await ownerLensFor(env, date, lens) : null
  const rows = await rootRows(lens ? 'user' : 'path', lens)
  if (rows == null) return null
  const mine = newAgg()
  for (const r of rows) if (ownerOk(r.usr, owner)) merge(mine, r)
  if (!ol) return { b: mine.b, o: mine.o }
  let all: Agg | null = null
  if (ol.needsTotal(path)) {
    const allRows = await rootRows('path')
    if (allRows?.length) { all = newAgg(); for (const r of allRows) merge(all, r) }
  }
  const agg = lensAgg(ol, lens!.key, path, all, mine)
  return { b: agg.b, o: agg.o }
}

/** The owner lens for a scan: the live claims folded against it (cached per
 * (scan, ledger head) by the totals machinery) — null when nobody claims. */
async function ownerLensFor(env: Env, date: string, lens: Lens): Promise<OwnerLens | null> {
  return ownerLens((await markTotals(env, date)).claims, lens.key)
}

/** A node under a user lens once claims apply: U's bytes there (`ol.value`),
 * shaped like the subtree total when that was read (a claimed band is all
 * of the node's mix), else like U's attributed slice — stretched when U's
 * claims below add bytes that slice never had. Every byte is U's, so the
 * per-user split is U alone. `mine` null = the node was not read (an unread
 * ancestor of a claimed region): its bytes are its bands below. */
function lensAgg(ol: OwnerLens, user: string, path: string, all: Agg | null, mine: Agg | null): Agg {
  const v = ol.value(path, all?.b ?? null, mine?.b ?? null)
  if (v <= 0) return newAgg()
  const shape = all ?? mine
  const out = shape && shape.b > 0 ? scale(shape, v / shape.b) : newAgg()
  if (!shape || shape.b <= 0) out.b = v
  out.ub = { [user]: out.b }
  return out
}

/** Everything a view needs before assembly: the scoped root, the kept
 * (above-threshold, ancestor-closed) scoped aggregates under it, the fold
 * counts, and which tier answered. `null` = nothing in scope under P. */
interface Read {
  rootAll: Agg
  rootAgg: Agg
  kept: Map<string, Agg>
  aggDepth: Map<string, number>
  foldedOf: Map<string, number>
  threshold: number
  thrAt: (depth: number) => number
  tier: string
  idx: IndexHandle
  truncated: boolean
  matches?: string[]
  /** The claims fold behind a user lens (null: no lens, or no claims). */
  ownerLens: OwnerLens | null
  /** A path's scoped share of its total (owner pool / lens and mark axis
   * applied). `all` = the subtree total (null under a lens where the path's
   * cover isn't U's); `mine` = the rows the sort's lens/pool kept (null =
   * unread, lens only). */
  scoped: (p: string, all: Agg | null, mine: Agg | null) => Agg
}

async function readView(env: Env, o: ViewOpts): Promise<Read | null> {
  const { date, path, w, h, minArea, atten, lens, owner, query } = o
  const dP = path === '' ? 0 : path.split('/').length
  const sort = lens ? 'user' : 'path'
  const readRoot = (idx: IndexHandle) =>
    path === '' ? readRows(idx, 1, 1, '', '￿', undefined, lens) : readRows(idx, dP, dP, path, path, undefined, lens)
  // The mark axis needs the ledger folded against this scan (cached per
  // (scan, head) by the totals machinery); per node it scales the aggregate
  // to its allowed-fate share. Rows are read by TOTAL bytes at the scoped
  // threshold, so the read is a superset of what the scope keeps.
  const fs: FateScope | null = o.fates ? fateScope((await markTotals(env, date)).marks, o.fates, lens?.key) : null
  // A user lens applies the ownership ledger: claims repaint attribution, so
  // U's bytes under a path can include other people's slices (a band U
  // claimed) or lose U's own (a band someone else claimed). Where the former
  // can happen the by-path tier is read too, so every node's total is known.
  const ol = lens ? await ownerLensFor(env, date, lens) : null
  // Where the by-path tier is read for a lens: P itself when U's claim
  // covers it, else the largest REGION_READS of U's claimed regions under P,
  // to draw inside them. The rest are nodes valued exactly from the manifest
  // (a claim's total is known without a read) — leaf tiles until drilled,
  // like a fold; a user with hundreds of scattered claims would otherwise
  // touch more row groups at the root than one view may.
  const rootTotal = !!ol && ol.needsTotal(path)
  const allRegions = ol && !rootTotal ? ol.regions(path) : []
  const regions: { path: string; depth: number }[] = rootTotal
    ? [{ path, depth: dP }]
    : [...allRegions].sort((a, b) => b.all - a.all).slice(0, REGION_READS)
  // Owner pools filter rows (they are owner slices); the fate share is then
  // computed on the node's total and applied to the pool's share — assumes
  // fates are spread like ownership inside a node. Under a lens the fate
  // fold already works in U's bytes (per-band `us`), so its share is taken
  // of U's lens bytes.
  const scoped = (p: string, all: Agg | null, mine: Agg | null): Agg => {
    const base = ol ? lensAgg(ol, lens!.key, p, all, mine) : mine!
    if (!fs) return base
    const denom = lens ? base : all!
    const share = denom.b > 0 ? fs.value(p, denom.b) / denom.b : 0
    return scale(base, share)
  }

  // Root aggregate P.b (and P's own us for the response root) from the
  // coarsest tier that has P at all — the same numbers in every tier. The
  // by-path tier of the same name serves the lens's region reads.
  const tiers: { name: string; idx: IndexHandle; all?: IndexHandle }[] = []
  for (const e of COARSE_EXPS) {
    const idx = await tryOpen(env, date, `coarse${e}${sort === 'path' ? '' : `-${sort}`}`)
    if (idx && floorOf(idx) != null) {
      const allIdx = regions.length ? await tryOpen(env, date, `coarse${e}`) : null
      tiers.push({ name: `coarse${e}`, idx, ...(allIdx ? { all: allIdx } : {}) })
    }
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
    // A lens user the scan attributes nothing to under P may still own it
    // all by claim: an empty attribution root is a zero, not a miss.
    if (!rootRows.length && !ol) throw new NotFound(path)
  }
  const rootAll = newAgg()
  const rootMine = newAgg()
  for (const r of rootRows) {
    merge(rootAll, r)
    if (ownerOk(r.usr, owner)) merge(rootMine, r)
  }
  // P's total, when U's claim covers P (one point read on the by-path tier).
  let rootTot: Agg | null = null
  if (rootTotal) {
    rootTot = newAgg()
    for (const r of await readRootAllRows(env, date, path, dP)) merge(rootTot, r)
  }
  let rootAgg = scoped(path, ol ? rootTot : rootAll, rootMine)
  // Nothing in scope under P: an empty view, not a zero threshold that would
  // keep every row the tier holds.
  if (rootAgg.b <= 0) return null
  const threshold = o.threshold ?? (rootAgg.b * minArea) / (w * h)
  const thrAt = (depth: number) => threshold * atten ** Math.max(0, depth - dP - 1)

  // The coarsest tier whose floor the UNSCOPED query can't see below. A
  // narrow scope lowers the node threshold (its root is smaller), but the
  // tier is chosen by the view's total bytes: the same rows a plain view of
  // P reads, scoped per node. Paths below that tier's floor fold into their
  // parent's (other) — exactly, since (other) = parent − Σ kept kids on the
  // scoped aggregates — instead of dragging the read onto the floor-free
  // tier for every scoped root view.
  const thrAll = o.threshold ?? (rootAll.b * minArea) / (w * h)
  let pick: { name: string; idx: IndexHandle } | null = null
  for (const t of tiers) {
    if (thrAll >= floorOf(t.idx)!) { pick = t; break }
  }
  if (!pick) pick = { name: 'fine', idx: fine ?? await openFine(env, date, sort, lens), ...(regions.length ? { all: await openFine(env, date, 'path') } : {}) }
  const idx = pick.idx

  // Everything under P that this canvas can draw. Depth is unbounded — the
  // attenuated threshold bounds both row count and depth by construction.
  const pLo = path === '' ? '' : path + '/'
  const pHi = path === '' ? '￿' : path + '0' // '0' sorts just past '/'
  const rows = await readRows(idx, dP + 1, 1e9, pLo, pHi, thrAt, lens)
  // The lens's claimed regions from the by-path tier (their own rows and
  // everything under them): every path there whose U-share can clear the
  // threshold is present, since all ≥ U's share. P itself as a region means
  // U's claim covers the whole view: the full range.
  const regionRects: Rect[] = regions.map(r => r.path === path
    ? { dLo: dP + 1, dHi: 1e9, pLo, pHi }
    : { dLo: r.depth, dHi: 1e9, pLo: r.path, pHi: r.path + '0' })
  const allRows = regionRects.length ? await readRects(pick.all!, regionRects, thrAt) : []
  const allAggs = new Map<string, Agg>() // totals per path (the fate share's denominator; a lens: only inside its regions)
  const mineAggs = new Map<string, Agg | null>() // the sort's own rows per path (U's slice, or the pool's); null = unread
  const aggDepth = new Map<string, number>()
  for (const r of rows) {
    let m = mineAggs.get(r.path)
    if (!m) { mineAggs.set(r.path, (m = newAgg())); aggDepth.set(r.path, r.depth) }
    if (ownerOk(r.usr, owner)) merge(m, r)
    if (!ol) {
      let all = allAggs.get(r.path)
      if (!all) allAggs.set(r.path, (all = newAgg()))
      merge(all, r)
    }
  }
  for (const r of allRows) {
    let all = allAggs.get(r.path)
    if (!all) { allAggs.set(r.path, (all = newAgg())); aggDepth.set(r.path, r.depth) }
    merge(all, r)
  }
  // Every region is a node — read ones from their rows, the rest valued
  // from the manifest — and so is each region's ancestor between P and it
  // (the kept set is ancestor-closed by construction); an ancestor whose U
  // slice never reached the threshold is unread and valued as its bands.
  for (const r of allRegions) {
    for (let q = r.path; q.length > path.length; q = parentOf(q)) {
      if (!aggDepth.has(q)) { aggDepth.set(q, q.split('/').length); mineAggs.set(q, null) }
    }
  }
  // Nothing is drawn inside an unread region (its total is a leaf until a
  // drill reads it), so U's attributed rows under one are not nodes.
  const unread = allRegions.filter(r => !regions.some(q => q.path === r.path)).map(r => r.path + '/')
  const insideUnread = (p: string) => unread.some(u => p.startsWith(u))
  let aggs = new Map<string, Agg>() // the scoped aggregate per path
  for (const p of aggDepth.keys()) {
    if (unread.length && insideUnread(p)) continue
    const mine = mineAggs.get(p) ?? null
    const all = allAggs.get(p) ?? null
    // Reads return whole row groups, so U's attributed rows inside a read
    // region can sit below the threshold; the by-path read would have held
    // any path whose total clears it. One it didn't is under the fold.
    if (ol && !all && ol.needsTotal(p) && !ol.isClaim(p)) continue
    aggs.set(p, fs || ol ? scoped(p, ol ? (ol.needsTotal(p) ? all : null) : all, mine) : mine!)
  }
  // Unread regions know their object counts from the manifest; an unread
  // ancestor's count is the sum of the regions under it (its own attributed
  // objects, below the threshold, are not known).
  for (const r of allRegions) {
    if (regions.some(q => q.path === r.path)) continue
    for (let q = r.path; q.length > path.length; q = parentOf(q)) {
      const a = aggs.get(q)
      if (a && mineAggs.get(q) == null && !allAggs.has(q)) a.o += r.objects
    }
  }
  let matches: string[] | undefined
  if (query) {
    const f = nameFilter(
      { path, agg: rootAgg },
      new Map([...aggs].map(([p, agg]) => [p, { depth: aggDepth.get(p)!, agg }])),
      query,
      (into, from) => { for (const k of ['b', 'o', 'wts', 'wb'] as const) into[k] += from[k]; if (from.a != null) into.a = into.a == null ? from.a : Math.max(into.a, from.a); for (const key of ['cb', 'ub'] as const) for (const [k, v] of Object.entries(from[key])) into[key][k] = (into[key][k] ?? 0) + v },
      newAgg,
    )
    rootAgg = f.root
    aggs = new Map([...f.aggs].map(([p, e]) => [p, e.agg]))
    matches = [...f.matches].sort()
  }
  // Nodes with nothing in scope fold away entirely.
  for (const [p, a] of aggs) if (a.b <= 0) aggs.delete(p)
  const keptList = [...aggs.entries()].filter(([p, a]) => a.b >= thrAt(aggDepth.get(p)!))
  const truncated = keptList.length > HARD_CAP
  // Descendant-inclusive bytes are monotone (ancestor ≥ descendant), so the
  // kept set — and even its top-N-by-bytes truncation — is ancestor-closed:
  // every kept path's parent is kept (or is P itself). Linking is trivial.
  const kept = new Map(
    (truncated ? keptList.sort((a, b) => b[1].b - a[1].b).slice(0, HARD_CAP) : keptList),
  )
  // Sub-threshold child count per parent. A coarse tier only holds paths
  // above its floor, so this counts the folded children it can see; the
  // (other) bytes themselves are exact regardless (parent − Σ kept).
  const foldedOf = new Map<string, number>()
  for (const [p, a] of aggs) {
    if (kept.has(p)) continue
    if (a.b >= thrAt(aggDepth.get(p)!)) continue // truncation casualty, not a fold
    const par = parentOf(p)
    const key = kept.has(par) ? par : par === path || (path === '' && !p.includes('/')) ? path : null
    if (key !== null) foldedOf.set(key, (foldedOf.get(key) ?? 0) + 1)
  }
  return { rootAll, rootAgg, kept, aggDepth, foldedOf, threshold, thrAt, tier: pick.name, idx, truncated, ...(matches ? { matches } : {}), ownerLens: ol, scoped }
}

/** P's own row(s) from the by-path tiers — coarsest that has it, else fine. */
async function readRootAllRows(env: Env, date: string, path: string, dP: number): Promise<Row[]> {
  const read = (idx: IndexHandle) => (path === '' ? readRows(idx, 1, 1, '', '￿') : readRows(idx, dP, dP, path, path))
  for (const e of COARSE_EXPS) {
    const idx = await tryOpen(env, date, `coarse${e}`)
    if (!idx) continue
    const rows = await read(idx)
    if (rows.length) return rows
  }
  return read(await openFine(env, date, 'path'))
}

/** Claimed regions read (largest first) per lens view; the rest are
 * manifest-valued leaves. Two span queries' worth of rects. */
const REGION_READS = 24

const parentOf = (p: string): string => {
  const cut = p.lastIndexOf('/')
  return cut === -1 ? '' : p.slice(0, cut)
}

/** Kept paths grouped under their parent (or P for the top level). */
function kidsIndex(kept: Map<string, Agg>, path: string): Map<string, string[]> {
  const kidsOf = new Map<string, string[]>()
  for (const [p] of kept) {
    const key = kept.has(parentOf(p)) ? parentOf(p) : path
    const arr = kidsOf.get(key)
    if (arr) arr.push(p)
    else kidsOf.set(key, [p])
  }
  return kidsOf
}

const rootName = (path: string) => (path === '' ? 'marin GCS' : path.split('/').pop()!)

export async function buildView(env: Env, o: ViewOpts): Promise<View> {
  const { path, query } = o
  const dP = path === '' ? 0 : path.split('/').length
  const v = await readView(env, o)
  if (!v) {
    return { tree: { n: rootName(path), b: 0, o: 0 }, tier: 'none', index: 'none', threshold: 0, nodes: 0, truncated: false, ...(query ? { matches: [] } : {}) }
  }
  const { kept, aggDepth, foldedOf, thrAt } = v
  const nodeOf = (name: string, a: Agg): ViewNode => ({
    n: name,
    b: Math.round(a.b),
    o: Math.round(a.o),
    ...display(a),
  })
  const kidsOf = kidsIndex(kept, path)
  const matched = new Set(v.matches ?? [])
  const build = (p: string, a: Agg): ViewNode => {
    const node = nodeOf(p === path ? rootName(path) : p.split('/').pop()!, a)
    if (matched.has(p)) node.m = 1
    const childPaths = kidsOf.get(p) ?? []
    if (!childPaths.length) return node
    const kids = childPaths
      .map(cp => build(cp, kept.get(cp)!))
      .sort((x, y) => y.b - x.b)
    const kidAggs = childPaths.map(cp => kept.get(cp)!)
    const rest = subtract(a, kidAggs)
    node.c = kids
    if (rest.b > thrAt((aggDepth.get(childPaths[0]!) ?? dP + 1))) {
      node.c.push({ ...nodeOf('(other)', rest), f: foldedOf.get(p) ?? 0 } as ViewNode)
    }
    return node
  }
  const tree = build(path, v.rootAgg)
  return { tree, tier: v.tier, index: v.idx.mode, threshold: v.threshold, nodes: kept.size, truncated: v.truncated, ...(v.matches ? { matches: v.matches } : {}) }
}

// --- the diff: two scans, one byte floor -----------------------------------

export interface DiffOpts extends Omit<ViewOpts, 'date'> {
  from: string
  to: string
  /** Frontier rows kept (largest |Δ| first); the expanded skeleton always ships. */
  top: number
}

export interface DiffRow {
  /** Path relative to P (`(other)` = the fold under its parent). */
  p: string
  d: number
  k: 'dir'
  s: 'added' | 'removed' | 'changed' | 'unchanged'
  a: number
  b: number
  oa: number
  ob: number
  /** Expanded: its own Δ is carried by its children. */
  x?: true
  /** Read by a point lookup on the side where it sat under the floor. */
  l?: 1 | 2
}

export interface Diff {
  rows: DiffRow[]
  total_a: number
  total_b: number
  objects_a: number
  objects_b: number
  expansions: number
  truncated: boolean
  threshold: number
  tier: string
  lookups: number
  /** The lookup budget ran out: some one-sided names may really be folded. */
  lookups_capped: boolean
}

const LOOKUP_CAP = 240

/** What changed under P between two scans (specs/view-serving.md §2):
 * both sides are read from their index tiers at ONE absolute byte floor —
 * the larger side's pixel-budget threshold — so a directory is named on both
 * sides or folded on both, never named here and hidden in `(other)` there
 * (the artifact a per-side budget fold produces: a static 337 Ti dir
 * reading as −81 / +141 Ti of churn). A name that clears the floor on one
 * side only is read point-blank from the other side's floor-free tier: if
 * it exists there it shows with its real size (it crossed the floor), else
 * it was added / removed. `(other)` is parent − Σ named on each side, so its
 * Δ is the sub-floor churn, truthfully. */
export async function buildDiff(env: Env, o: DiffOpts): Promise<Diff> {
  const { from, to, path, lens, owner, query } = o
  const dP = path === '' ? 0 : path.split('/').length
  const sort = lens ? 'user' : 'path'
  const [ra, rb] = await Promise.all([
    readRootAgg(env, { date: from, path, lens }),
    readRootAgg(env, { date: to, path, lens }),
  ])
  if (!ra && !rb) throw new NotFound(path)
  const threshold = o.threshold ?? (Math.max(ra?.b ?? 0, rb?.b ?? 0) * o.minArea) / (o.w * o.h)
  const [va, vb] = await Promise.all([
    ra ? readView(env, { ...o, date: from, threshold }) : null,
    rb ? readView(env, { ...o, date: to, threshold }) : null,
  ])
  const kidsA = va ? kidsIndex(va.kept, path) : new Map<string, string[]>()
  const kidsB = vb ? kidsIndex(vb.kept, path) : new Map<string, string[]>()

  // Point lookups on the floor-free tier, memoized per side.
  const fineOf = new Map<string, Promise<IndexHandle>>()
  const fine = (date: string) => {
    let h = fineOf.get(date)
    if (!h) fineOf.set(date, (h = openFine(env, date, sort, lens)))
    return h
  }
  const fineAllOf = new Map<string, Promise<IndexHandle>>()
  const fineAll = (date: string) => {
    let h = fineAllOf.get(date)
    if (!h) fineAllOf.set(date, (h = openFine(env, date, 'path')))
    return h
  }
  let lookups = 0
  let capped = false
  const inQuery = (p: string, v: Read): boolean =>
    !query || (v.matches ?? []).some(m => p === m || p.startsWith(m + '/'))
  const lookup = async (date: string, v: Read, p: string, depth: number): Promise<Agg | null> => {
    if (!inQuery(p, v)) return null
    if (lookups >= LOOKUP_CAP) { capped = true; return null }
    lookups++
    const rows = await readRows(await fine(date), depth, depth, p, p, undefined, lens)
    const needTot = !!v.ownerLens?.needsTotal(p)
    const allRows = needTot ? await readRows(await fineAll(date), depth, depth, p, p) : rows
    if (!rows.length && !allRows.length) return null
    const all = newAgg()
    const mine = newAgg()
    for (const r of allRows) merge(all, r)
    for (const r of rows) if (ownerOk(r.usr, owner)) merge(mine, r)
    const a = v.scoped(p, v.ownerLens && !needTot ? null : all, mine)
    return a.b > 0 ? a : null
  }

  const rows: DiffRow[] = []
  let expansions = 0
  const rel = (p: string) => (path === '' ? p : p.slice(path.length + 1))
  const status = (a: Agg | null, b: Agg | null): DiffRow['s'] =>
    !a ? 'added' : !b ? 'removed' : Math.round(a.b) !== Math.round(b.b) || Math.round(a.o) !== Math.round(b.o) ? 'changed' : 'unchanged'
  const emit = (p: string, d: number, a: Agg | null, b: Agg | null, x: boolean, l?: 1 | 2) => {
    rows.push({
      p, d, k: 'dir', s: status(a, b),
      a: Math.round(a?.b ?? 0), b: Math.round(b?.b ?? 0), oa: Math.round(a?.o ?? 0), ob: Math.round(b?.o ?? 0),
      ...(x ? { x: true } : {}), ...(l ? { l } : {}),
    })
  }
  // Level by level, so a level's point lookups run in parallel (each is a
  // D1 span query + a row-group read; in series they dominated the walk).
  interface Item { p: string; d: number; a: Agg | null; b: Agg | null; l?: 1 | 2 }
  let level: Item[] = [{ p: path, d: dP, a: va?.rootAgg ?? null, b: vb?.rootAgg ?? null }]
  const PAR = 8
  while (level.length) {
    const next: Item[] = []
    // Expansion decisions and the names under each expanded node.
    const plans = level.map(it => {
      const { p, a, b } = it
      const ka = kidsA.get(p) ?? []
      const kb = kidsB.get(p) ?? []
      // Both sides present, something named under it on either side, and
      // the totals differ: the children carry the Δ. One-sided subtrees ride
      // whole on their own row; a byte-identical subtree is one unchanged
      // row (the renderer infers it as filler), not a walk of its skeleton.
      const same = !!(a && b && Math.round(a.b) === Math.round(b.b) && Math.round(a.o) === Math.round(b.o))
      const expand = !!(a && b && !same && (ka.length || kb.length))
      const names = expand ? [...new Set([...ka, ...kb])].sort() : []
      return { it, expand, names }
    })
    // The names one side lacks, looked up on that side — in parallel.
    const asks: { cp: string; d: number; side: 1 | 2; v: Read; date: string }[] = []
    for (const { it, names } of plans) {
      for (const cp of names) {
        if (va && !va.kept.has(cp)) asks.push({ cp, d: it.d + 1, side: 1, v: va, date: from })
        if (vb && !vb.kept.has(cp)) asks.push({ cp, d: it.d + 1, side: 2, v: vb, date: to })
      }
    }
    const found = new Map<string, Agg | null>() // `${side}:${cp}`
    for (let i = 0; i < asks.length; i += PAR) {
      const chunk = asks.slice(i, i + PAR)
      const got = await Promise.all(chunk.map(q => lookup(q.date, q.v, q.cp, q.d)))
      chunk.forEach((q, j) => found.set(`${q.side}:${q.cp}`, got[j]))
    }
    for (const { it, expand, names } of plans) {
      const { p, d, a, b } = it
      if (p !== path) emit(rel(p), d - dP, a, b, expand, it.l)
      if (!expand) continue
      expansions++
      const sum = { a: newAgg(), b: newAgg() }
      for (const cp of names) {
        const ca = va?.kept.get(cp) ?? found.get(`1:${cp}`) ?? null
        const cb = vb?.kept.get(cp) ?? found.get(`2:${cp}`) ?? null
        const lk: 1 | 2 | undefined = ca && !va!.kept.has(cp) ? 1 : cb && !vb!.kept.has(cp) ? 2 : undefined
        if (ca) { sum.a.b += ca.b; sum.a.o += ca.o }
        if (cb) { sum.b.b += cb.b; sum.b.o += cb.o }
        next.push({ p: cp, d: d + 1, a: ca, b: cb, ...(lk ? { l: lk } : {}) })
      }
      const restA = newAgg()
      restA.b = Math.max(0, a!.b - sum.a.b)
      restA.o = Math.max(0, a!.o - sum.a.o)
      const restB = newAgg()
      restB.b = Math.max(0, b!.b - sum.b.b)
      restB.o = Math.max(0, b!.o - sum.b.o)
      if (restA.b > 0 || restB.b > 0) {
        const key = p === path ? '(other)' : `${rel(p)}/(other)`
        emit(key, d - dP + 1, restA.b > 0 ? restA : null, restB.b > 0 ? restB : null, false)
      }
    }
    level = next
  }

  // Cap like the batch walk did: every expanded ancestor (the skeleton) plus
  // the top-|Δ| changed frontier rows; unchanged frontier rows are inferred
  // as filler by the renderer.
  const skeleton = rows.filter(r => r.x)
  const frontier = rows
    .filter(r => !r.x && r.s !== 'unchanged')
    .sort((r1, r2) => Math.abs(r2.b - r2.a) - Math.abs(r1.b - r1.a))
  return {
    rows: [...skeleton, ...frontier.slice(0, o.top)],
    total_a: Math.round(va?.rootAgg.b ?? 0),
    total_b: Math.round(vb?.rootAgg.b ?? 0),
    objects_a: Math.round(va?.rootAgg.o ?? 0),
    objects_b: Math.round(vb?.rootAgg.o ?? 0),
    expansions,
    truncated: frontier.length > o.top,
    threshold,
    tier: (vb ?? va)!.tier,
    lookups,
    lookups_capped: capped,
  }
}

async function openFine(env: Env, date: string, sort: string, lens?: Lens): Promise<IndexHandle> {
  try {
    return await openIndex(env, date, sort)
  } catch (e) {
    if (lens && String((e as Error).message).includes('not synced')) throw new LensUnavailable(sort)
    throw e
  }
}

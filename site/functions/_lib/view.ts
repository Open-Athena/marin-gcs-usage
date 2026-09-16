/** One view = "path + scan + pixel budget", answered from the index tiers
 * (specs/view-serving.md §1–2). Shared by `/api/subtree` (the map),
 * `/api/diff` and `/api/series`.
 *
 * gcs's `view.ts` minus its scope axes: CoreWeave has no attribution (no
 * user lens / owner pools), no storage classes, no index extras, and its
 * marks live in a different ledger (not folded here yet). What stays is the
 * whole serving mechanism — tier planning, the pixel-budget fold, the name
 * filter, the depth-capped first paint, and the two-scan diff at one floor.
 *
 * Treemap area is proportional to bytes, so "everything this canvas can draw
 * under P" is one predicate: b ≥ P.b · minArea / (w·h), attenuated per level.
 * Every tier holds rows `(path, depth, usr, b, o, …)`, descendant-
 * inclusive, sorted `(depth, path)`, so a query is row-group-pruned range
 * reads, a threshold filter, and a nested-tree assembly with `(other)` =
 * parent − Σ kept children (exact by subtraction).
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
import { type IndexHandle, openIndex, readAsks, readRows, type Row, type Trace, withTrace } from './index.js'
import { nameFilter, type NamePred } from './scope.js'

export const MIN_AREA_DEFAULT = 12 // px² of the smallest legible cell (~3×4)
// Each nesting level below the query root loses canvas to chrome (title bars,
// insets) — and deeper detail is one drill away regardless. The per-node
// threshold doubles per level: thr(depth) = thr · ATTEN^(depth − dP − 1).
export const ATTEN_DEFAULT = 2
export const QUANT = 128 // px quantization for w/h → cache-key stability
const HARD_CAP = 50_000 // response nodes; way above any real canvas budget
// Coarse tiers the job writes, coarsest first (index.py COARSE_EXPS).
const COARSE_EXPS = [16, 20, 24]

export type ViewNode = { n: string; b: number; o: number; c?: ViewNode[]; f?: number } & Record<string, unknown>

export interface ViewOpts {
  date: string
  path: string
  /** Timing sink for the response's `Server-Timing` header (see `index.ts` `Trace`). */
  trace?: Trace
  w: number
  h: number
  minArea: number
  atten: number
  /** Absolute byte threshold instead of the pixel-budget one. Still
   * attenuated per level by `atten`. */
  threshold?: number
  /** `depth=N`: read only N levels below the root (rows at the cap arrive
   * without children — still drillable branches). The same pixel budget, so
   * the top level is *identical* to the uncapped view's; a `depth=1` fetch is
   * one depth-band read that the client shows while the full tree loads, and
   * the full tree then fills in under tiles that don't move. */
  maxDepth?: number
  /** `q=`: name filter over the read rows' paths (see `scope.ts`). */
  query?: NamePred
}

export interface View {
  tree: ViewNode
  /** Which tier answered: `coarse<E>` or `fine` (floor-free). */
  tier: string
  /** How that tier's metadata was read: `d1` (footer in D1) or `blob`. */
  index: string
  threshold: number
  nodes: number
  truncated: boolean
  /** With `query`: the outermost matching paths. */
  matches?: string[]
}

export class NotFound extends Error {}

interface Agg {
  b: number
  o: number
  wts: number
  wb: number
}

const newAgg = (): Agg => ({ b: 0, o: 0, wts: 0, wb: 0 })

function merge(a: Agg, r: Row): void {
  a.b += r.b
  a.o += r.o
  a.wts += r.wts
  a.wb += r.wb
}

function subtract(parent: Agg, kids: Agg[]): Agg {
  const out = newAgg()
  const sum = (f: (a: Agg) => number) => kids.reduce((s, k) => s + f(k), 0)
  out.b = parent.b - sum(a => a.b)
  out.o = Math.max(0, parent.o - sum(a => a.o))
  out.wts = parent.wts - sum(a => a.wts)
  out.wb = Math.max(0, parent.wb - sum(a => a.wb))
  return out
}

function display(a: Agg): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  if (a.wb) out.d = Math.round(a.wts / a.wb / 86400)
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

const floorOf = (h: IndexHandle): number | null => h.floor

const readRoot = (idx: IndexHandle, path: string, dP: number) =>
  path === '' ? readRows(idx, 1, 1, '', '￿') : readRows(idx, dP, dP, path, path)

/** Just P's aggregate for one scan — the size-over-time chart's point
 * (`/api/series`): the root read of a view, from the coarsest tier that has
 * P, without folding anything under it. */
export async function readRootAgg(env: Env, o: { date: string; path: string }): Promise<{ b: number; o: number } | null> {
  const { date, path } = o
  const dP = path === '' ? 0 : path.split('/').length
  // P's row from the coarsest tier, else the floor-free one; null = no tier.
  // Two hops at most: a point read costs the same few round trips on any tier.
  const top = await tryOpen(env, date, `coarse${COARSE_EXPS[0]}`)
  let rows: Row[] = []
  if (top) rows = await readRoot(top, path, dP)
  if (!rows.length) {
    const fine = await tryOpen(env, date, 'path')
    if (!fine) return null
    rows = await readRoot(fine, path, dP)
  }
  const agg = newAgg()
  for (const r of rows) merge(agg, r)
  return { b: agg.b, o: agg.o }
}

/** Everything a view needs before assembly: the root, the kept
 * (above-threshold, ancestor-closed) aggregates under it, the fold counts,
 * and which tier answered. `null` = nothing under P. */
interface Read {
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
}

async function readView(env: Env, o: ViewOpts): Promise<Read | null> {
  const { date, path, w, h, minArea, atten, query, maxDepth } = o
  const dP = path === '' ? 0 : path.split('/').length
  // Root aggregate P.b from the coarsest tier that has P at all — the same
  // numbers in every tier.
  const tr = o.trace
  let t0 = performance.now()
  const tiers = (await Promise.all(COARSE_EXPS.map(async e => {
    const idx = await tryOpen(env, date, `coarse${e}`)
    if (!idx || floorOf(idx) == null) return null
    return { name: `coarse${e}`, idx: withTrace(idx, tr) }
  }))).filter((t): t is { name: string; idx: IndexHandle } => t != null)
  tr?.('open', performance.now() - t0)
  let rootRows: Row[] = []
  let fine: IndexHandle | null = null
  t0 = performance.now()
  for (const t of tiers) {
    rootRows = await readRoot(t.idx, path, dP)
    if (rootRows.length) break
  }
  if (!rootRows.length) {
    fine = withTrace(await openIndex(env, date, 'path'), tr)
    rootRows = await readRoot(fine, path, dP)
    if (!rootRows.length) throw new NotFound(path)
  }
  let rootAgg = newAgg()
  for (const r of rootRows) merge(rootAgg, r)
  // Nothing under P: an empty view, not a zero threshold that would keep
  // every row the tier holds.
  if (rootAgg.b <= 0) return null
  const threshold = o.threshold ?? (rootAgg.b * minArea) / (w * h)
  const thrAt = (depth: number) => threshold * atten ** Math.max(0, depth - dP - 1)

  // The coarsest tier whose floor the query can't see below. Paths below
  // that tier's floor fold into their parent's (other) — exactly, since
  // (other) = parent − Σ kept kids.
  let pick: { name: string; idx: IndexHandle } | null = null
  for (const t of tiers) {
    if (threshold >= floorOf(t.idx)!) { pick = t; break }
  }
  tr?.('root', performance.now() - t0)
  if (!pick) pick = { name: 'fine', idx: fine ?? withTrace(await openIndex(env, date, 'path'), tr) }
  const idx = pick.idx

  // Everything under P that this canvas can draw. Depth is unbounded — the
  // attenuated threshold bounds both row count and depth by construction.
  const pLo = path === '' ? '' : path + '/'
  const pHi = path === '' ? '￿' : path + '0' // '0' sorts just past '/'
  // `maxDepth` caps the band read at dP + N: the rows at the cap come back
  // childless (the client treats a `c`-less branch as drillable), and the
  // deeper bands — the bulk of the work — are never touched.
  t0 = performance.now()
  const rows = await readRows(idx, dP + 1, maxDepth != null ? dP + maxDepth : 1e9, pLo, pHi, thrAt)
  tr?.('rows', performance.now() - t0)
  let aggs = new Map<string, Agg>()
  const aggDepth = new Map<string, number>()
  const foldedOf = new Map<string, number>()
  // The threshold applies to plain per-path byte totals before a single
  // `Agg` exists (the general path built two aggregate objects per path for
  // every row a root read returns and kept ~1k of them — 2 s of CPU per
  // view on the edge, against ~0.8 s for the decode itself).
  const tot = new Map<string, number>()
  for (const r of rows) tot.set(r.path, (tot.get(r.path) ?? 0) + r.b)
  for (const [p, b] of tot) {
    const d = p.split('/').length
    if (b >= thrAt(d)) aggDepth.set(p, d)
  }
  for (const r of rows) {
    if (!aggDepth.has(r.path)) continue
    let a = aggs.get(r.path)
    if (!a) aggs.set(r.path, (a = newAgg()))
    merge(a, r)
  }
  // the folded (sub-threshold) child count per kept parent
  for (const [p, b] of tot) {
    if (b <= 0 || aggDepth.has(p)) continue
    const par = parentOf(p)
    const key = aggDepth.has(par) ? par : par === path || (path === '' && !p.includes('/')) ? path : null
    if (key !== null) foldedOf.set(key, (foldedOf.get(key) ?? 0) + 1)
  }
  let matches: string[] | undefined
  if (query) {
    // The name filter needs every read path (not just the kept ones): a
    // match under the fold still contributes its bytes to its ancestors.
    const all = new Map<string, { depth: number; agg: Agg }>()
    for (const r of rows) {
      let e = all.get(r.path)
      if (!e) all.set(r.path, (e = { depth: r.depth, agg: newAgg() }))
      merge(e.agg, r)
    }
    const f = nameFilter(
      { path, agg: rootAgg },
      all,
      query,
      (into, from) => { for (const k of ['b', 'o', 'wts', 'wb'] as const) into[k] += from[k] },
      newAgg,
    )
    rootAgg = f.root
    aggs = new Map([...f.aggs].map(([p, e]) => [p, e.agg]))
    for (const [p, e] of f.aggs) aggDepth.set(p, e.depth)
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
  return { rootAgg, kept, aggDepth, foldedOf, threshold, thrAt, tier: pick.name, idx, truncated, ...(matches ? { matches } : {}) }
}

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

const rootName = (path: string) => (path === '' ? 'Marin CoreWeave' : path.split('/').pop()!)

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
  /** Totals only: both sides' root reads, no walk (`rows` empty) — what the
   * diff section's headline shows while the full diff aligns. */
  summary?: boolean
  /** Expand at most this many levels below P (rows at the cap come back
   * unexpanded, as leaves) — `depth=1` is the top-level diff the page draws
   * first, before the full walk lands. */
  depth?: number
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
 * (the artifact a per-side budget fold produces). A name that clears the
 * floor on one side only is read point-blank from the other side's
 * floor-free tier: if it exists there it shows with its real size (it
 * crossed the floor), else it was added / removed. `(other)` is parent −
 * Σ named on each side, so its Δ is the sub-floor churn, truthfully. */
export async function buildDiff(env: Env, o: DiffOpts): Promise<Diff> {
  const { from, to, path, query } = o
  const dP = path === '' ? 0 : path.split('/').length
  const tr = o.trace
  let t0 = performance.now()
  const [ra, rb] = await Promise.all([
    readRootAgg(env, { date: from, path }),
    readRootAgg(env, { date: to, path }),
  ])
  tr?.('rootagg', performance.now() - t0)
  if (!ra && !rb) throw new NotFound(path)
  const threshold = o.threshold ?? (Math.max(ra?.b ?? 0, rb?.b ?? 0) * o.minArea) / (o.w * o.h)
  t0 = performance.now()
  // A depth-capped walk only ever consults rows down to that depth, so the
  // views read just those bands (`depth=1`: two groups instead of ~25 a side).
  const [va, vb] = await Promise.all([
    ra ? readView(env, { ...o, date: from, threshold, ...(o.depth != null ? { maxDepth: o.depth } : {}) }) : null,
    rb ? readView(env, { ...o, date: to, threshold, ...(o.depth != null ? { maxDepth: o.depth } : {}) }) : null,
  ])
  tr?.('views', performance.now() - t0)
  const walkStart = performance.now()
  const totals = {
    total_a: Math.round(va?.rootAgg.b ?? 0),
    total_b: Math.round(vb?.rootAgg.b ?? 0),
    objects_a: Math.round(va?.rootAgg.o ?? 0),
    objects_b: Math.round(vb?.rootAgg.o ?? 0),
    threshold,
    tier: (vb ?? va)!.tier,
  }
  if (o.summary) return { rows: [], ...totals, expansions: 0, truncated: false, lookups: 0, lookups_capped: false }
  const kidsA = va ? kidsIndex(va.kept, path) : new Map<string, string[]>()
  const kidsB = vb ? kidsIndex(vb.kept, path) : new Map<string, string[]>()

  // Point lookups on the floor-free tier, memoized per side.
  const fineOf = new Map<string, Promise<IndexHandle>>()
  const fine = (date: string) => {
    let h = fineOf.get(date)
    if (!h) fineOf.set(date, (h = openIndex(env, date, 'path').then(x => withTrace(x, tr))))
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
    const rows = await readRows(await fine(date), depth, depth, p, p)
    if (!rows.length) return null
    const a = newAgg()
    for (const r of rows) merge(a, r)
    return a.b > 0 ? a : null
  }

  // One side's lookups for a whole level in one read: `readAsks` turns the
  // level's (depth, path) asks into a few span queries + one round of group
  // reads. A batch too wide for the reader's group cap falls back to per-ask
  // reads rather than failing the diff.
  const PAR = 8
  const lookupMany = async (date: string, v: Read, items: { cp: string; d: number }[]): Promise<Map<string, Agg | null>> => {
    const out = new Map<string, Agg | null>()
    const todo: { cp: string; d: number }[] = []
    for (const q of items) if (inQuery(q.cp, v)) todo.push(q); else out.set(q.cp, null)
    const perAsk = async (qs: { cp: string; d: number }[]) => {
      for (let i = 0; i < qs.length; i += PAR) {
        const chunk = qs.slice(i, i + PAR)
        const got = await Promise.all(chunk.map(q => lookup(date, v, q.cp, q.d)))
        chunk.forEach((q, j) => out.set(q.cp, got[j]))
      }
    }
    if (!todo.length) return out
    const room = Math.max(0, LOOKUP_CAP - lookups)
    const take = todo.slice(0, room)
    if (todo.length > room) { capped = true; for (const q of todo.slice(room)) out.set(q.cp, null) }
    if (!take.length) return out
    const want = new Map(take.map(q => [`${q.d}\0${q.cp}`, q]))
    let got: Row[]
    try {
      got = (await readAsks(await fine(date), take.map(q => ({ depth: q.d, path: q.cp })), r => want.has(`${r.depth}\0${r.path}`), { maxGroups: 120 })).rows
    } catch (e) {
      if (!/too wide/.test(String((e as Error).message ?? e))) throw e
      await perAsk(take)
      return out
    }
    lookups += take.length
    const byPath = new Map<string, Agg>()
    for (const r of got) {
      let e = byPath.get(r.path)
      if (!e) byPath.set(r.path, (e = newAgg()))
      merge(e, r)
    }
    for (const q of take) {
      const a = byPath.get(q.cp)
      out.set(q.cp, a && a.b > 0 ? a : null)
    }
    return out
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
  // Level by level, so a level's point lookups run in parallel.
  interface Item { p: string; d: number; a: Agg | null; b: Agg | null; l?: 1 | 2 }
  let level: Item[] = [{ p: path, d: dP, a: va?.rootAgg ?? null, b: vb?.rootAgg ?? null }]
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
      const expand = !!(a && b && !same && (ka.length || kb.length)) && (o.depth == null || it.d - dP < o.depth)
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
    const sideAsks = (side: 1 | 2) => asks.filter(q => q.side === side)
    const [gotA, gotB] = await Promise.all([
      va && sideAsks(1).length ? lookupMany(from, va, sideAsks(1)) : new Map<string, Agg | null>(),
      vb && sideAsks(2).length ? lookupMany(to, vb, sideAsks(2)) : new Map<string, Agg | null>(),
    ])
    for (const [cp, agg] of gotA) found.set(`1:${cp}`, agg)
    for (const [cp, agg] of gotB) found.set(`2:${cp}`, agg)
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
  tr?.('walk', performance.now() - walkStart)
  const skeleton = rows.filter(r => r.x)
  const frontier = rows
    .filter(r => !r.x && r.s !== 'unchanged')
    .sort((r1, r2) => Math.abs(r2.b - r2.a) - Math.abs(r1.b - r1.a))
  return {
    rows: [...skeleton, ...frontier.slice(0, o.top)],
    ...totals,
    expansions,
    truncated: frontier.length > o.top,
    lookups,
    lookups_capped: capped,
  }
}

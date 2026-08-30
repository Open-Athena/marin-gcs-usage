/** Pixel-budget subtree serving over the floor-free path index
 * (specs/path-index-lazy-drill.md).
 *
 *   GET /api/subtree?date=<scan>&path=<P>&w=<px>&h=<px>[&minArea=<px²>]
 *
 * Treemap area is proportional to bytes, so "everything this canvas can
 * draw under P" is one predicate: pb ≥ P.pb · minArea / (w·h). The index
 * (`listing/<date>/path-index.parquet`) holds every ancestor path ×
 * attribution, descendant-inclusive, sorted (depth, path) — so a query is
 * row-group-pruned range reads (hyparquet over the S3Store's byte ranges),
 * a client-side threshold filter, and a nested-tree assembly with the same
 * `(other)`-residual subtraction as `tree_build`. Ancestors of a kept path
 * are kept automatically (descendant-inclusive bytes are monotone), so no
 * orphan pruning is needed.
 *
 * Responses are immutable per (date, path, w₁₂₈, h₁₂₈, minArea) — scans
 * never change — and cached in the edge cache accordingly. w/h arrive
 * quantized-up to 128px so resizes mostly re-hit the cache.
 */
import { CW_SCOPE, type Env, GCS_SCOPE, requireScope } from '../_lib/auth.js'
import { makeStore, openIndex, readRows, type Lens, type Row } from '../_lib/index.js'

const MIN_AREA_DEFAULT = 12 // px² of the smallest legible cell (~3×4)
// Each nesting level below the query root loses canvas to chrome (title bars,
// insets) — and deeper detail is one drill away regardless. The per-node
// threshold doubles per level: thr(depth) = thr · ATTEN^(depth − dP − 1).
const ATTEN_DEFAULT = 2
const QUANT = 128 // px quantization for w/h → cache-key stability
const HARD_CAP = 50_000 // response nodes; way above any real canvas budget
const CACHE = 'private, max-age=86400' // immutable per scan; browser may hold it

// Per-isolate cache — the coarse tree is ~30 MB and immutable per date.
const trees = new Map<string, Promise<TreeNode>>()

type TreeNode = { n: string; b: number; o: number; c?: TreeNode[]; f?: number } & Record<string, unknown>

async function treeFor(env: Env, date: string): Promise<TreeNode> {
  const cached = trees.get(date)
  if (cached) return cached
  const p = (async () => {
    const store = makeStore(env)
    const r = await store.get(`snapshots/${date}/tree.json`)
    return JSON.parse(new TextDecoder().decode(r.bytes)) as TreeNode
  })()
  trees.set(date, p)
  p.catch(() => trees.delete(date))
  return p
}

/** Serve from the coarse tier: walk tree.json to P and re-fold by the
 * pixel-budget threshold. Valid whenever the query's threshold is at least
 * the pipeline floors (then tree.json's kept set ⊇ the query's). */
function sliceTree(root: TreeNode, path: string, thrAt: (d: number) => number, dP: number): TreeNode | null {
  let node: TreeNode | undefined = root
  for (const seg of path === '' ? [] : path.split('/')) {
    node = node?.c?.find(c => c.n === seg)
    if (!node) return null
  }
  const fold = (n: TreeNode, depth: number): TreeNode => {
    const out: TreeNode = { ...n }
    delete out.c
    const kids = n.c ?? []
    if (!kids.length) return out
    const thr = thrAt(depth + 1)
    const kept = kids.filter(c => !c.n.startsWith('(') && c.b >= thr).map(c => fold(c, depth + 1))
    const folded = kids.filter(c => c.n.startsWith('(') || c.b < thr)
    if (kept.length || folded.length) {
      out.c = kept.sort((a, b) => b.b - a.b)
      const restB = folded.reduce((s, c) => s + c.b, 0)
      if (restB > 0) {
        // merge sub-threshold + pre-folded children into one (other)
        const restO = folded.reduce((s, c) => s + c.o, 0)
        const maps: Record<string, Record<string, number>> = {}
        for (const key of ['cb', 'tm', 'sh'] as const) {
          const acc: Record<string, number> = {}
          for (const c of folded) {
            for (const [k, v] of Object.entries((c[key] as Record<string, number>) ?? {})) acc[k] = (acc[k] ?? 0) + v
          }
          if (Object.keys(acc).length) maps[key] = acc
        }
        const us: Record<string, number> = {}
        for (const c of folded) for (const [u, b] of (c.us as [string, number][]) ?? []) us[u] = (us[u] ?? 0) + b
        const other: TreeNode = { n: '(other)', b: restB, o: restO, f: folded.reduce((s, c) => s + (c.f ?? 1), 0), ...maps }
        if (Object.keys(us).length) other.us = Object.entries(us).sort((a, b) => b[1] - a[1])
        out.c.push(other)
      }
      if (!out.c.length) delete out.c
    }
    return out
  }
  return fold(node, dP)
}

interface Agg {
  b: number
  o: number
  wts: number
  wb: number
  a: number | null // subtree-max last-read epoch day (read lens); null = never read
  cb: Record<string, number>
  tm: Record<string, number>
  ub: Record<string, number>
  sh: Record<string, number>
}

const newAgg = (): Agg => ({ b: 0, o: 0, wts: 0, wb: 0, a: null, cb: {}, tm: {}, ub: {}, sh: {} })

function merge(a: Agg, r: Row): void {
  a.b += r.b
  a.o += r.o
  a.wts += r.wts
  a.wb += r.wb
  if (r.a != null) a.a = a.a == null ? r.a : Math.max(a.a, r.a)
  for (const [k, v] of [['2', r.c2], ['3', r.c3], ['4', r.c4]] as [string, number][]) {
    if (v) a.cb[k] = (a.cb[k] ?? 0) + v
  }
  const team = r.team ?? 'unattributed'
  a.tm[team] = (a.tm[team] ?? 0) + r.b
  if (r.usr) a.ub[r.usr] = (a.ub[r.usr] ?? 0) + r.b
  else if (team !== 'unattributed') a.sh[team] = (a.sh[team] ?? 0) + r.b
}

function subtract(parent: Agg, kids: Agg[]): Agg {
  const out = newAgg()
  out.a = parent.a // max isn't subtractable; the residual's read day ≤ parent's
  const sum = (f: (a: Agg) => number) => kids.reduce((s, k) => s + f(k), 0)
  out.b = parent.b - sum(a => a.b)
  out.o = Math.max(0, parent.o - sum(a => a.o))
  out.wts = parent.wts - sum(a => a.wts)
  out.wb = Math.max(0, parent.wb - sum(a => a.wb))
  for (const key of ['cb', 'tm', 'ub', 'sh'] as const) {
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
  if (Object.keys(a.tm).length) out.tm = desc(a.tm)
  if (Object.keys(a.sh).length) out.sh = desc(a.sh)
  if (Object.keys(a.ub).length) {
    out.us = Object.entries(a.ub).sort((x, y) => y[1] - x[1]).map(([u, b]) => [u, b])
  }
  return out
}

export const onRequestGet = async (ctx: { request: Request; env: Env }): Promise<Response> => {
  const { GCS_HMAC_KEY_ID, GCS_HMAC_SECRET } = ctx.env
  if (!GCS_HMAC_KEY_ID || !GCS_HMAC_SECRET) {
    return new Response('subtree API not configured (missing GCS HMAC creds)', { status: 503 })
  }
  const url = new URL(ctx.request.url)
  const date = url.searchParams.get('date') ?? ''
  const path = (url.searchParams.get('path') ?? '').replace(/\/+$/, '')
  const w = Math.ceil((Number(url.searchParams.get('w')) || 1280) / QUANT) * QUANT
  const h = Math.ceil((Number(url.searchParams.get('h')) || 800) / QUANT) * QUANT
  const minArea = Number(url.searchParams.get('minArea')) || MIN_AREA_DEFAULT
  const atten = Number(url.searchParams.get('atten')) || ATTEN_DEFAULT
  // Optional lens: `lens=user:<id>` / `lens=team:<name>` — treemap of that
  // user's/team's bytes, served from the by-user/by-team index variant
  // (specs/path-agnostic-serving.md §2.3). Skips the coarse tier (tree.json has
  // no per-user split).
  const lensRaw = url.searchParams.get('lens')
  let lens: Lens | undefined
  let lensVariant = 'path'
  if (lensRaw) {
    const m = /^(user|team):(.+)$/.exec(lensRaw)
    if (!m) return new Response('bad lens (want user:<id> or team:<name>)', { status: 400 })
    lens = { col: m[1] === 'user' ? 'u' : 't', key: m[2] }
    lensVariant = m[1] === 'user' ? 'user' : 'team'
  }
  if (!/^\d{4}-\d{2}-\d{2}(?:T\d{4})?$/.test(date)) return new Response('bad date', { status: 400 })
  if (path.startsWith('cw/')) {
    const gated = await requireScope(ctx, CW_SCOPE)
    if (gated instanceof Response) return gated
  }
  const gated = await requireScope(ctx, GCS_SCOPE)
  if (gated instanceof Response) return gated

  const cacheKey = new Request(
    `https://subtree.cache/${date}/${encodeURIComponent(path)}?w=${w}&h=${h}&a=${minArea}&t=${atten}&l=${lensRaw ?? ''}`,
  )
  const cache = (caches as unknown as { default: Cache }).default
  const hit = await cache.match(cacheKey)
  if (hit) return hit

  try {
    const dP = path === '' ? 0 : path.split('/').length

    // Tier 1 — coarse: when P is in tree.json and the query threshold clears
    // the pipeline floors (parent-relative MIN_FRAC ∨ 5GB abs), tree.json's
    // kept set is a superset of the query's — re-fold it by pixel budget and
    // never touch the index. This covers root/shallow queries, whose index
    // prefix ranges would otherwise span most of the file.
    const coarse = lens ? null : await treeFor(ctx.env, date).catch(() => null)
    if (coarse) {
      let node: TreeNode | undefined = coarse
      for (const seg of path === '' ? [] : path.split('/')) node = node?.c?.find(c => c.n === seg)
      if (node) {
        const threshold = (node.b * minArea) / (w * h)
        // Coarse suffices when the query can't want anything below the 5GB
        // pipeline floor. A shallow node whose parent-relative floor exceeds
        // the threshold may fold marginally early — its (other) stays
        // drillable, and that drill's lower threshold routes to the fine tier.
        if (threshold >= 5e9) {
          const thrAt = (depth: number) => threshold * atten ** Math.max(0, depth - dP - 1)
          const tree = sliceTree(coarse, path, thrAt, dP)
          if (tree) {
            const count = (n: TreeNode): number => 1 + (n.c ?? []).reduce((s, c) => s + count(c), 0)
            const body = JSON.stringify({
              date, path, w, h, minArea, atten, tier: 'coarse', index: 'coarse',
              threshold: Math.round(threshold), nodes: count(tree), truncated: false, tree,
            })
            const res = new Response(body, {
              headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': CACHE },
            })
            await cache.put(cacheKey, res.clone())
            return res
          }
        }
      }
    }

    // Tier 2 — fine: narrow/deep queries against the floor-free index (the
    // lens variant when a lens is set — its rows carry the lens's bytes).
    const idx = await openIndex(ctx.env, date, lensVariant)

    // Root aggregate P.pb (and P's own tm/us for the response root).
    const rootAgg = newAgg()
    if (path === '') {
      for (const r of await readRows(idx, 1, 1, '', '￿', undefined, lens)) merge(rootAgg, r)
    } else {
      const rows = await readRows(idx, dP, dP, path, path, undefined, lens)
      if (!rows.length) return new Response('path not found', { status: 404 })
      for (const r of rows) merge(rootAgg, r)
    }
    const threshold = (rootAgg.b * minArea) / (w * h)
    const thrAt = (depth: number) => threshold * atten ** Math.max(0, depth - dP - 1)

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

    type Node = { n: string; b: number; o: number; c?: Node[]; f?: number } & Record<string, unknown>
    const nodeOf = (name: string, a: Agg): Node => ({
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
    const foldedOf = new Map<string, number>() // sub-threshold child count per parent
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
    const build = (p: string, a: Agg): Node => {
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
        node.c.push({ ...nodeOf('(other)', rest), f: foldedOf.get(p) ?? 0 } as Node)
      }
      return node
    }
    const tree = build(path, rootAgg)

    const body = JSON.stringify({
      date,
      path,
      w,
      h,
      minArea,
      atten,
      tier: 'fine',
      index: idx.mode,
      ...(lensRaw ? { lens: lensRaw } : {}),
      threshold: Math.round(threshold),
      nodes: keptMap.size,
      truncated,
      tree,
    })
    const res = new Response(body, {
      headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': CACHE },
    })
    await cache.put(cacheKey, res.clone())
    return res
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e)
    if (msg.includes('404') || msg.toLowerCase().includes('not found')) {
      return new Response('no path index for that date', { status: 404 })
    }
    return new Response(`subtree error: ${msg}`, { status: 500 })
  }
}

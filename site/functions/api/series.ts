/** Bytes under a path per scan — the size-over-time chart, scoped like the
 * map (specs/view-serving.md §1 "series.json" → this).
 *
 *   GET /api/series?path=<P>[&lens=user:<id>|user:me][&o=owned|unowned]
 *
 * One point per scan the index knows: P's own row in that scan's coarsest
 * tier that holds it (or the floor-free tier), scoped per row like a view's
 * root. Nothing is precomputed per prefix and nothing is floored: a path
 * below every tier falls through to the floor-free tier. Scans predating the
 * index (or the scope's variant) have no per-prefix point — but for the
 * unscoped whole-bucket series (empty path, no lens / owner / class filter)
 * a scan's `meta.json` total is as good as an index row, so those scans are
 * filled in from the snapshot dir: the chart keeps any history that never
 * got (or lost) its tiers, rather than showing a gap.
 */
import { type Ctx, json, requireScope, requireViewer } from '../_lib/auth.js'
import { snapshotsPrefix } from '../_lib/shared.js'
import { type Lens, makeStore, pathGens, pathScans, storeReady } from '../_lib/index.js'
import { hasLedger, ledgerHead } from '../_lib/ledger.js'
import { classKey, ownerKey, ownerOk, parseClasses, parseOwner, QueryError, queryParam } from '../_lib/scope.js'
import { liveTotal, rollupTotal, staticFilterStore, staticLiteral, staticTag } from '../_lib/staticFilter.js'
import { indexedOnly, reject, rejectBody, rejectQuery, rejectScope } from '../_lib/indexedOnly.js'
import { readRootAgg, readRootRows } from '../_lib/view.js'
import { type OverTime, overTimePoint, readOverTime } from '../_lib/overTime.js'
import { parsePaths } from '../_lib/filter.js'
import { SERIES_MAX_PATHS } from '../_lib/seriesLimits.js'
import { metaRoots, rootPoints, type RootRow } from '../_lib/series.js'
import { cacheKeyFor, cacheMatch, cacheStore, serverTiming } from '../_lib/edgeCache.js'
import { LENS_PRIMARY_ONLY, storeKey, withStore } from '../_lib/stores.js'
import { lensParam, ME_UNRESOLVED, resolveLens } from '../_lib/me.js'
import { askBox, boxFor, boxStatus, type BoxEnv } from '../_lib/queryBox.js'

// The default store's snapshot dirs (`snapshots/<date>/`; other stores live in
// a named subdir that DATE_RE keeps out), and the scan-id shape they're named by.
const DATE_RE = /^\d{4}-\d{2}-\d{2}(T\d{4})?$/

/** Scans present as snapshot dirs but absent from the index (oldest first). */
async function unindexedScans(env: Ctx['env'], indexed: Set<string>): Promise<string[]> {
  const store = makeStore(env)
  const out: string[] = []
  let cursor: string | undefined
  do {
    const page = await store.list(snapshotsPrefix(env), { cursor })
    for (const e of page.entries) {
      const d = e.key.slice(snapshotsPrefix(env).length).replace(/\/$/, '')
      if (e.isDir && DATE_RE.test(d) && !indexed.has(d)) out.push(d)
    }
    cursor = page.cursor
  } while (cursor)
  return out.sort()
}

type Meta = { total_bytes?: number; total_objects?: number; buckets?: Record<string, { total_bytes: number; total_objects: number }> }

async function readMeta(env: Ctx['env'], date: string): Promise<Meta | null> {
  try {
    const { bytes } = await makeStore(env).get(`${snapshotsPrefix(env)}${date}/meta.json`)
    return JSON.parse(new TextDecoder().decode(bytes)) as Meta
  } catch (e) {
    console.log(`series /: no meta.json point for ${date}: ${(e as Error).message}`)
    return null
  }
}

/** A whole-bucket point from a scan's `meta.json` (no tiers needed). */
async function metaPoint(env: Ctx['env'], date: string): Promise<{ date: string; b: number; o: number } | null> {
  const m = await readMeta(env, date)
  return m && typeof m.total_bytes === 'number' ? { date, b: m.total_bytes, o: m.total_objects ?? 0 } : null
}

export const onRequestGet = async (ctx0: Ctx & { waitUntil?: (p: Promise<unknown>) => void }): Promise<Response> => {
  // `store=<key>`: a secondary store's env overlay (none = the primary, as is).
  const ctx = withStore(ctx0)
  if (ctx instanceof Response) return ctx
  const { env, request } = ctx
  if (!env.DB) return json({ error: 'index backend not configured (DB)' }, 503)
  if (!storeReady(env)) return json({ error: 'index reader not configured' }, 503)
  const st = serverTiming()
  const gated = await st.time('auth', requireViewer(ctx))
  if (gated instanceof Response) return gated
  const url = new URL(request.url)
  const path = (url.searchParams.get('path') ?? '').replace(/\/+$/, '')
  if (path.includes('..') || path.startsWith('/')) return json({ error: 'bad path' }, 400)
  // `paths=` (specs/filter-views.md §4): the filter's match roots — the point
  // is the sum of one root read per scan. Bounded like a view's region reads.
  const paths = parsePaths(url.searchParams.getAll('paths'))
  if (paths.some(p => p.includes('..') || p.startsWith('/'))) return json({ error: 'bad paths' }, 400)
  if (paths.length > SERIES_MAX_PATHS) return json({ error: `too many paths (max ${SERIES_MAX_PATHS})` }, 400)
  const lensRaw = url.searchParams.get('lens')
  let lens: Lens | undefined
  if (lensRaw) {
    if (env.STORE_KEY) return json({ error: LENS_PRIMARY_ONLY }, 400)
    const m = /^user:(.+)$/.exec(lensRaw)
    if (!m) return json({ error: 'bad lens (want user:<id>)' }, 400)
    // `user:me` → the caller's id: the shared cache key and the body carry the id.
    const resolved = await resolveLens(env, gated, { key: m[1] })
    if (!resolved) return json({ error: ME_UNRESOLVED }, 400)
    lens = resolved
  }
  const lensTag = lensParam(lens)
  const owner = parseOwner(url.searchParams.get('o'))
  const classes = parseClasses(url.searchParams.get('cl'))
  // `q=` (`qs=`): the map's filter. A single literal on a scan of the static name index's generation is
  // answered from it — Σ that scan's own match roots under `path` (`staticFilter.ts`); other scans and
  // other queries take `paths=`, the current scan's match roots, as before. A view root the query
  // matches is matched whole: its plain series.
  let qp: ReturnType<typeof queryParam>
  try {
    qp = queryParam(url.searchParams, env.QUERY_SYNTAX)
  } catch (e) {
    // Indexed-only: a form it refuses is refused as such (`a b` is "one term", not "too short").
    const r = e instanceof QueryError && indexedOnly(env) ? rejectQuery(url.searchParams.get('q'), url.searchParams.get('qs'), env.QUERY_SYNTAX) : null
    if (r) return new Response(rejectBody(r), { status: 400, headers: { 'content-type': 'application/json' } })
    if (e instanceof QueryError) return json({ error: `bad query: ${e.message}` }, 400)
    throw e
  }
  // An indexed-only deployment: one literal, unscoped (`_lib/indexedOnly.ts`).
  const strict = !!qp.query && indexedOnly(env)
  const refused = strict ? rejectQuery(url.searchParams.get('q'), url.searchParams.get('qs'), env.QUERY_SYNTAX) ?? rejectScope(!!(lens || owner || classes)) : null
  if (refused) return new Response(rejectBody(refused), { status: 400, headers: { 'content-type': 'application/json' } })
  const query = qp.query && !qp.query(path) ? qp.query : undefined
  if (qp.query && !query) paths.length = 0
  const sfs = query && !lens && !classes ? staticFilterStore(env) : null
  const skey = sfs ? staticLiteral(query!.ast) : null
  // `split=roots` (specs/done/root-geneses.md §1): the unscoped store root only —
  // one trace per depth-1 row (bucket) beside the total.
  const split = url.searchParams.get('split')
  if (split && split !== 'roots') return json({ error: 'bad split (want roots)' }, 400)
  if (split && (path || paths.length || lens || owner || classes || query)) return json({ error: 'split=roots is for the unscoped store root only' }, 400)

  // Every scan with a synced floor-free index, oldest first.
  const rows = await st.time('scans', pathScans(env, true))
  const dates = rows.results.map(r => r.date)
  // A user lens or an owner pool applies the live assignments, so its key carries the ledger head.
  const head = lens || owner && await hasLedger(env) ? await ledgerHead(env) : 0
  // Unscoped whole-bucket series only: scans without tiers still have a total in meta.json.
  const extra = path === '' && !paths.length && !query && !lens && !owner && !classes ? await st.time('unindexed', unindexedScans(env, new Set(dates))) : []
  // Two-tier cache (colo + KV, `_lib/edgeCache.ts`), keyed by every input
  // including the scan list and the ledger head, so an entry is immutable and
  // a new scan is a new key. This used to `cache.put` a `private` response
  // straight into the Workers Cache API, which refuses those (413) — so every
  // chart load re-read one point per scan (≈8 rounds of D1 + range reads for
  // a 94-scan history, 5–20 s) while the diff beside it was a cache hit.
  const g = await st.time('gens', pathGens(env, dates))
  const cacheKey = cacheKeyFor('series', `${encodeURIComponent(path)}?P=${encodeURIComponent(paths.join(','))}&l=${lensTag}&o=${owner ? ownerKey(owner) : ''}&cl=${classKey(classes)}&s=${split ?? ''}&qs=${query ? qp.syntax : ''}&q=${encodeURIComponent(query ? url.searchParams.get('q') ?? '' : '')}&st=${staticTag(env, query)}&d=${dates.join(',')}&x=${extra.join(',')}&head=${head}&g=${g}`, storeKey(env))
  const hit = await st.time('cache', cacheMatch(env, cacheKey))
  if (hit) return hit

  // The serving box first, when the deployment has one (`_lib/queryBox.ts`):
  // it must hold exactly the scans this index knows (`n` / `first` / `last`,
  // else it declines), and the tier-less scans' `meta.json` points are the
  // Worker's alone.
  const benv = env as typeof env & BoxEnv
  const box = boxFor(benv, url)
  let engine = benv.QUERY_BOX_URL ? 'worker;box=skip' : undefined
  if (box && !extra.length && dates.length) {
    const params = new URLSearchParams(url.searchParams)
    params.set('n', String(dates.length))
    params.set('first', dates[0])
    params.set('last', dates[dates.length - 1])
    const a = await st.time('box', askBox(benv, box, 'series', params))
    if (a.kind === 'answer' && a.status !== 200) return boxStatus(a)
    if (a.kind === 'answer') return cacheStore(env, cacheKey, a.body, { 'server-timing': st.header(), 'x-query-engine': a.engine }, ctx.waitUntil?.bind(ctx))
    engine = `worker;fallback=${a.why}`
  }

  // Fast path (specs/obs-axis-indexing.md Phase 1): a path's series (or a
  // filter's, one line per match root) reads the cross-scan over-time index —
  // ⌈scans/K⌉ pruned reads per root — instead of one point read per scan.
  // Read only on a cache miss (it used to precede the check: ~1.2 s per hit).
  // `point()` below takes any scan the groups cover from here (absent there =
  // absent, the groups are floor-free); only the unsealed tip falls through to
  // the per-scan read. Lens / owner / class scopes and `split` aren't in the
  // index. Any line unreadable → all per-scan.
  // The static filter's match roots under `path`, or its rollup there (a heavy literal under a heavy
  // directory: the per-child running totals give every scan's total). Null: not a static literal, declined,
  // or a rollup under an owner pool (rollups carry no owners).
  const found = skey ? await st.time('static', sfs!.source.hits(skey, path)) : null
  const shits = found?.rollup && owner ? null : found
  // The scans it is exact on: the drilldown's are its base generation's.
  const sscans = shits ? new Set(shits.scans ?? await sfs!.scans()) : null
  // Indexed-only: no answer is `scan-not-indexed` (never the client's roots read per scan).
  if (query && strict && !shits) return new Response(rejectBody(reject('scan-not-indexed')), { status: 400, headers: { 'content-type': 'application/json' } })
  if (query && !shits && !paths.length) return json({ error: 'a filtered series needs its match roots (paths=)' }, 400)
  /** Indexed-only: the scans the answer doesn't cover (gaps in the series, named). */
  const unindexed: string[] = []
  const indexable = !split && !lens && !owner && !classes && !shits
  const lines = indexable ? await st.time('overtime', Promise.all((paths.length ? paths : [path]).map(p => readOverTime(env, p)))) : []
  const ot: OverTime[] | null = lines.length && lines.every(Boolean) ? lines as OverTime[] : null

  const points: { date: string; b: number; o: number }[] = []
  // split=roots: each scan's depth-1 rows (the total is their sum), and for
  // tier-less scans whatever meta.json says about its roots.
  const rootsByDate = new Map<string, RootRow[]>()
  // One scan's point; a D1 hiccup ("internal error") gets one more try, and
  // anything else unreadable is a missing point, not a failed chart — logged,
  // since a silently absent point looks like a gap in the data.
  const point = async (date: string, tries = 2): Promise<{ date: string; b: number; o: number } | null> => {
    try {
      if (shits && sscans!.has(date)) {
        const t = shits.rollup ? rollupTotal(shits.rollup, date) : liveTotal(shits.hits, date, u => ownerOk(u, owner))
        return { date, b: t.b, o: t.o }
      }
      // A scan the answer doesn't cover: the client's match roots (`paths=`), unless they come from a rollup,
      // which lists only some of them (a gap, not a wrong point).
      if (shits && (!paths.length || shits.rollup || strict)) { if (strict) unindexed.push(date); return null }
      const covered = ot ? overTimePoint(ot, date) : undefined
      if (covered !== undefined) return covered && { date, ...covered }
      if (split) {
        const rows = await readRootRows(env, date)
        if (!rows) return null
        rootsByDate.set(date, rows)
        return { date, b: rows.reduce((n, r) => n + r.b, 0), o: rows.reduce((n, r) => n + r.o, 0) }
      }
      if (paths.length) {
        // Σ over the match roots; a root absent from a scan contributes 0.
        const parts = await Promise.all(paths.map(p => readRootAgg(env, { date, path: p, lens, owner, classes })))
        const b = parts.reduce((n, a) => n + (a?.b ?? 0), 0)
        const o = parts.reduce((n, a) => n + (a?.o ?? 0), 0)
        return parts.some(Boolean) ? { date, b, o } : null
      }
      const a = await readRootAgg(env, { date, path, lens, owner, classes })
      return a ? { date, ...a } : null
    } catch (e) {
      const msg = (e as Error).message
      if (tries > 1 && /internal error/i.test(msg)) return point(date, tries - 1)
      console.log(`series ${path || '/'} ${lensTag || owner || ''}: no point for ${date}: ${msg}`)
      return null
    }
  }
  // Scans are independent reads (each a few D1 round trips and one range
  // GET); a dozen at a time keeps a 40-scan history to ~4 rounds.
  for (let i = 0; i < dates.length; i += 12) {
    const got = await st.time('points', Promise.all(dates.slice(i, i + 12).map(d => point(d))))
    for (const g of got) if (g) points.push(g)
  }
  // A single-bucket store's tier-less history belongs to its sole root: the
  // earliest indexed scan having exactly one root says which.
  const first = [...rootsByDate.keys()].sort()[0]
  const soleRoot = first && rootsByDate.get(first)!.length === 1 ? rootsByDate.get(first)![0].path : null
  for (let i = 0; i < extra.length; i += 12) {
    const got = await Promise.all(extra.slice(i, i + 12).map(async d => {
      if (!split) return metaPoint(env, d)
      const m = await readMeta(env, d)
      if (!m || typeof m.total_bytes !== 'number') return null
      const roots = metaRoots(m, soleRoot)
      if (roots.length) rootsByDate.set(d, roots)
      return { date: d, b: m.total_bytes, o: m.total_objects ?? 0 }
    }))
    for (const g of got) if (g) points.push(g)
  }
  points.sort((a, b) => a.date.localeCompare(b.date))
  const body = JSON.stringify({ path, ...(unindexed.length ? { unindexed: unindexed.sort() } : {}), ...(paths.length ? { paths } : {}), ...(lens ? { lens: lensTag } : {}), ...(owner ? { owner } : {}), points, ...(split ? { roots: rootPoints(rootsByDate) } : {}) })
  return cacheStore(env, cacheKey, body, { 'server-timing': st.header(), ...(engine ? { 'x-query-engine': engine } : {}) }, ctx.waitUntil?.bind(ctx))
}

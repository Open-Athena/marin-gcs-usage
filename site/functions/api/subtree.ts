/** Pixel-budget subtree of any path, served from the index tiers
 * (specs/view-serving.md; the folding lives in `_lib/view.ts`).
 *
 *   GET /api/subtree?date=<scan>&path=<P>&w=<px>&h=<px>[&minArea=<px²>][&depth=<levels>][&q=<name filter>]
 *
 * gcs's endpoint minus its scope axes (no owner / mark / class axes on cw).
 * Responses are immutable per (date, path, w₁₂₈, h₁₂₈, minArea, atten, depth,
 * q) and cached in the edge cache accordingly; w/h arrive quantized-up to
 * 128px so resizes mostly re-hit the cache. Viewer-gated like `/data/*`.
 */
import { type Ctx, requireViewer } from '../_lib/auth.js'
import { parseQuery } from '../_lib/scope.js'
import { ATTEN_DEFAULT, buildView, MIN_AREA_DEFAULT, NotFound, QUANT } from '../_lib/view.js'
import { cacheKeyFor, cacheMatch, cacheStore, serverTiming } from '../_lib/edgeCache.js'

export const onRequestGet = async (ctx: Ctx & { waitUntil?: (p: Promise<unknown>) => void }): Promise<Response> => {
  const st = serverTiming()
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
  if (!/^\d{4}-\d{2}-\d{2}(?:T\d{4})?$/.test(date)) return new Response('bad date', { status: 400 })
  if (path.includes('..') || path.startsWith('/')) return new Response('bad path', { status: 400 })
  // `depth=N`: cap the read N levels below the root (see ViewOpts.maxDepth).
  const depth = Number(url.searchParams.get('depth')) || undefined
  const qRaw = url.searchParams.get('q') ?? ''
  const query = parseQuery(qRaw) ?? undefined

  const gated = await st.time('auth', requireViewer(ctx))
  if (gated instanceof Response) return gated

  const cacheKey = cacheKeyFor('subtree',
    `${date}/${encodeURIComponent(path)}?w=${w}&h=${h}&a=${minArea}&t=${atten}&D=${depth ?? ''}&q=${encodeURIComponent(query ? qRaw : '')}`,
  )
  const hit = await st.time('match', cacheMatch(ctx.env, cacheKey))
  if (hit) return hit

  try {
    const view = await buildView(ctx.env, { date, path, w, h, minArea, atten, maxDepth: depth, query, trace: st.trace })
    const body = JSON.stringify({
      date,
      path,
      w,
      h,
      minArea,
      atten,
      tier: view.tier,
      index: view.index,
      threshold: Math.round(view.threshold),
      nodes: view.nodes,
      truncated: view.truncated,
      ...(query ? { q: qRaw, matches: view.matches } : {}),
      tree: view.tree,
    })
    return await cacheStore(ctx.env, cacheKey, body, { 'server-timing': st.header() }, ctx.waitUntil?.bind(ctx))
  } catch (e) {
    if (e instanceof NotFound) return new Response('path not found', { status: 404 })
    const msg = String((e as Error).message ?? e)
    if (msg.includes('not synced')) return new Response(msg, { status: 409 })
    if (msg.startsWith('query too wide')) return new Response(msg, { status: 413 })
    return new Response(`subtree failed: ${msg}`, { status: 500 })
  }
}

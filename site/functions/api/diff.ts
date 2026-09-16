/** What changed under a path between two scans, read server-side from both
 * scans' index tiers at one shared byte floor (`_lib/view.ts` `buildDiff`).
 *
 *   GET /api/diff?from=<scan>&to=<scan>&path=<P>&w=<px>&h=<px>[&minArea=<px²>][&top=<n>]
 *                 [&q=<name filter>][&summary=1][&depth=<levels>]
 *
 * `summary=1` answers with the totals only (both sides' root reads, no walk
 * — `rows` empty): the section's headline, seconds before the rows.
 *
 * The response is the treemap's row list (`{ rows, total_a, total_b,
 * objects_a, objects_b, expansions, truncated }` — the shape the batch job's
 * `diff.json` had), immutable per (from, to, path, budget, q) and
 * edge-cached accordingly. gcs's endpoint minus its scope axes.
 */
import { type Ctx, requireViewer } from '../_lib/auth.js'
import { parseQuery } from '../_lib/scope.js'
import { ATTEN_DEFAULT, buildDiff, MIN_AREA_DEFAULT, NotFound, QUANT } from '../_lib/view.js'
import { cacheKeyFor, cacheMatch, cacheStore, serverTiming } from '../_lib/edgeCache.js'
const SCAN_RE = /^\d{4}-\d{2}-\d{2}(?:T\d{4})?$/

export const onRequestGet = async (ctx: Ctx & { waitUntil?: (p: Promise<unknown>) => void }): Promise<Response> => {
  const st = serverTiming()
  const { GCS_HMAC_KEY_ID, GCS_HMAC_SECRET } = ctx.env
  if (!GCS_HMAC_KEY_ID || !GCS_HMAC_SECRET) {
    return new Response('diff API not configured (missing GCS HMAC creds)', { status: 503 })
  }
  const url = new URL(ctx.request.url)
  const from = url.searchParams.get('from') ?? ''
  const to = url.searchParams.get('to') ?? ''
  const path = (url.searchParams.get('path') ?? '').replace(/\/+$/, '')
  const w = Math.ceil((Number(url.searchParams.get('w')) || 1280) / QUANT) * QUANT
  const h = Math.ceil((Number(url.searchParams.get('h')) || 800) / QUANT) * QUANT
  const minArea = Number(url.searchParams.get('minArea')) || MIN_AREA_DEFAULT
  const atten = Number(url.searchParams.get('atten')) || ATTEN_DEFAULT
  const top = Math.min(5000, Number(url.searchParams.get('top')) || 500)
  const summary = url.searchParams.get('summary') === '1'
  const depth = Number(url.searchParams.get('depth')) || undefined
  if (!SCAN_RE.test(from) || !SCAN_RE.test(to)) return new Response('bad from/to', { status: 400 })
  if (from >= to) return new Response('from must precede to', { status: 400 })
  if (path.includes('..') || path.startsWith('/')) return new Response('bad path', { status: 400 })
  const qRaw = url.searchParams.get('q') ?? ''
  const query = parseQuery(qRaw) ?? undefined

  const gated = await st.time('auth', requireViewer(ctx))
  if (gated instanceof Response) return gated

  const cacheKey = cacheKeyFor('diff',
    `${from}/${to}/${encodeURIComponent(path)}?w=${w}&h=${h}&a=${minArea}&t=${atten}&n=${top}&q=${encodeURIComponent(query ? qRaw : '')}&s=${summary ? 1 : 0}&D=${depth ?? ''}`,
  )
  const hit = await st.time('match', cacheMatch(ctx.env, cacheKey))
  if (hit) return hit

  try {
    const diff = await buildDiff(ctx.env, { from, to, path, w, h, minArea, atten, top, query, summary, depth, trace: st.trace })
    const body = JSON.stringify({
      prev: from,
      curr: to,
      path,
      ...(query ? { q: qRaw } : {}),
      ...diff,
      threshold: Math.round(diff.threshold),
    })
    return await cacheStore(ctx.env, cacheKey, body, { 'server-timing': st.header() }, ctx.waitUntil?.bind(ctx))
  } catch (e) {
    if (e instanceof NotFound) return new Response('path not found in either scan', { status: 404 })
    const msg = String((e as Error).message ?? e)
    if (msg.includes('not synced')) return new Response(msg, { status: 409 })
    if (msg.startsWith('query too wide')) return new Response(msg, { status: 413 })
    return new Response(`diff failed: ${msg}`, { status: 500 })
  }
}

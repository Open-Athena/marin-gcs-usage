/** What changed under a path between two scans, read server-side from both
 * scans' index tiers at one shared byte floor (`_lib/view.ts` `buildDiff`).
 *
 *   GET /api/diff?from=<scan>&to=<scan>&path=<P>&w=<px>&h=<px>[&minArea=<px²>][&top=<n>]
 *                 [&lens=user:<id>][&o=claimed|unclaimed][&k=<⊆ksu>][&q=<name filter>]
 *
 * Same scope axes as `/api/subtree`; the response is the treemap's row list
 * (`{ rows, total_a, total_b, objects_a, objects_b, expansions, truncated }`),
 * immutable per (from, to, path, budget, scope) plus the ledger head when
 * `k` is set, and edge-cached accordingly.
 */
import { CW_SCOPE, type Env, GCS_SCOPE, requireScope } from '../_lib/auth.js'
import { parseFates } from '../_lib/fates.js'
import type { Lens } from '../_lib/index.js'
import { ledgerHead } from '../_lib/ledger.js'
import { parseOwner, parseQuery } from '../_lib/scope.js'
import { ATTEN_DEFAULT, buildDiff, LensUnavailable, MIN_AREA_DEFAULT, NotFound, QUANT } from '../_lib/view.js'

const CACHE = 'private, max-age=86400'
const SCAN_RE = /^\d{4}-\d{2}-\d{2}(?:T\d{4})?$/

export const onRequestGet = async (ctx: { request: Request; env: Env }): Promise<Response> => {
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
  if (!SCAN_RE.test(from) || !SCAN_RE.test(to)) return new Response('bad from/to', { status: 400 })
  if (from >= to) return new Response('from must precede to', { status: 400 })
  if (path.includes('..') || path.startsWith('/')) return new Response('bad path', { status: 400 })

  const lensRaw = url.searchParams.get('lens')
  let lens: Lens | undefined
  if (lensRaw) {
    const m = /^user:(.+)$/.exec(lensRaw)
    if (!m) return new Response('bad lens (want user:<id>)', { status: 400 })
    lens = { key: m[1] }
  }
  const rawOwner = url.searchParams.get('o')
  const owner = parseOwner(rawOwner)
  const fates = parseFates(url.searchParams.get('k'))
  const qRaw = url.searchParams.get('q') ?? ''
  const query = parseQuery(qRaw) ?? undefined

  const scope = path.startsWith('cw/') ? CW_SCOPE : GCS_SCOPE
  const gated = await requireScope(ctx as never, scope)
  if (gated instanceof Response) return gated

  const head = (fates || lens) && ctx.env.DB ? await ledgerHead(ctx.env) : 0
  const cacheKey = new Request(
    `https://diff.cache/${from}/${to}/${encodeURIComponent(path)}?w=${w}&h=${h}&a=${minArea}&t=${atten}&n=${top}&l=${lensRaw ?? ''}` +
      `&o=${rawOwner ?? ''}&k=${fates ? [...fates].sort().join(',') : ''}&q=${encodeURIComponent(query ? qRaw : '')}&head=${head}`,
  )
  const cache = (caches as unknown as { default: Cache }).default
  const hit = await cache.match(cacheKey)
  if (hit) return hit

  try {
    const diff = await buildDiff(ctx.env, { from, to, path, w, h, minArea, atten, top, lens, owner, fates, query })
    const body = JSON.stringify({
      prev: from,
      curr: to,
      path,
      ...(lensRaw ? { lens: lensRaw } : {}),
      ...(owner ? { owner } : {}),
      ...(fates ? { fates: [...fates].sort() } : {}),
      ...(query ? { q: qRaw } : {}),
      ...diff,
      threshold: Math.round(diff.threshold),
    })
    const res = new Response(body, {
      headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': CACHE },
    })
    await cache.put(cacheKey, res.clone())
    return res
  } catch (e) {
    if (e instanceof NotFound) return new Response('path not found in either scan', { status: 404 })
    if (e instanceof LensUnavailable) return new Response('lens index not available for a scan', { status: 409 })
    const msg = String((e as Error).message ?? e)
    if (msg.startsWith('query too wide')) return new Response(msg, { status: 413 })
    return new Response(`diff failed: ${msg}`, { status: 500 })
  }
}

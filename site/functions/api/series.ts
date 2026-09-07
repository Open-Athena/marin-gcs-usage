/** Bytes under a path per scan — the size-over-time chart, scoped like the
 * map (specs/view-serving.md §1 "series.json" → this).
 *
 *   GET /api/series?path=<P>[&lens=user:<id>][&o=claimed|unclaimed]
 *
 * One point per scan the index knows: P's own row in that scan's coarsest
 * tier that holds it (or the floor-free tier), scoped per row like a view's
 * root. Nothing is precomputed per prefix and nothing is floored: a path
 * below every tier falls through to the floor-free tier. Scans predating the
 * index (or the scope's variant) are simply absent.
 */
import { type Ctx, GCS_SCOPE, json, requireScope } from '../_lib/auth.js'
import type { Lens } from '../_lib/index.js'
import { ledgerHead } from '../_lib/ledger.js'
import { parseOwner } from '../_lib/scope.js'
import { readRootAgg } from '../_lib/view.js'

export const onRequestGet = async (ctx: Ctx): Promise<Response> => {
  const { env, request } = ctx
  if (!env.DB) return json({ error: 'index backend not configured (DB)' }, 503)
  if (!env.GCS_HMAC_KEY_ID || !env.GCS_HMAC_SECRET) return json({ error: 'index reader not configured' }, 503)
  const gated = await requireScope(ctx, GCS_SCOPE)
  if (gated instanceof Response) return gated
  const url = new URL(request.url)
  const path = (url.searchParams.get('path') ?? '').replace(/\/+$/, '')
  if (path.includes('..') || path.startsWith('/')) return json({ error: 'bad path' }, 400)
  const lensRaw = url.searchParams.get('lens')
  let lens: Lens | undefined
  if (lensRaw) {
    const m = /^user:(.+)$/.exec(lensRaw)
    if (!m) return json({ error: 'bad lens (want user:<id>)' }, 400)
    lens = { key: m[1] }
  }
  const owner = parseOwner(url.searchParams.get('o'))

  // Every scan with a synced floor-free index, oldest first.
  const rows = await env.DB.prepare("SELECT DISTINCT date FROM index_schema WHERE variant = 'path' ORDER BY date").all<{ date: string }>()
  const dates = rows.results.map(r => r.date)
  // A user lens applies the live claims, so its key carries the ledger head.
  const head = lens ? await ledgerHead(env) : 0
  const cacheKey = new Request(`https://series.cache/${encodeURIComponent(path)}?l=${lensRaw ?? ''}&o=${owner ?? ''}&d=${dates.join(',')}&head=${head}`)
  const cache = (caches as unknown as { default: Cache }).default
  const hit = await cache.match(cacheKey)
  if (hit) return hit

  const points: { date: string; b: number; o: number }[] = []
  // Scans are independent reads; a handful at a time keeps the range-request
  // fan-out polite without serializing a 40-scan history.
  for (let i = 0; i < dates.length; i += 6) {
    const chunk = dates.slice(i, i + 6)
    const got = await Promise.all(chunk.map(async date => {
      try {
        const a = await readRootAgg(env, { date, path, lens, owner })
        return a ? { date, ...a } : null
      } catch {
        return null // an unreadable scan is a missing point, not a failed chart
      }
    }))
    for (const g of got) if (g) points.push(g)
  }
  const res = json({ path, ...(lensRaw ? { lens: lensRaw } : {}), ...(owner ? { owner } : {}), points }, 200, { 'cache-control': 'private, max-age=300' })
  await cache.put(cacheKey, res.clone())
  return res
}

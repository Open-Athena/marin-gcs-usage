/** Bytes under a path per scan — the size-over-time chart, following the
 * drill like the map (specs/view-serving.md §1 "series.json" → this).
 *
 *   GET /api/series?path=<P>
 *
 * One point per scan the index knows: P's own row in that scan's coarsest
 * tier that holds it (or the floor-free tier). Nothing is precomputed per
 * prefix and nothing is floored: a path below every tier falls through to
 * the floor-free tier. Scans predating the index are simply absent.
 * gcs's endpoint minus its lens/owner axes.
 */
import { type Ctx, json, requireViewer } from '../_lib/auth.js'
import { readRootAgg } from '../_lib/view.js'

export const onRequestGet = async (ctx: Ctx): Promise<Response> => {
  const { env, request } = ctx
  if (!env.DB) return json({ error: 'index backend not configured (DB)' }, 503)
  if (!env.GCS_HMAC_KEY_ID || !env.GCS_HMAC_SECRET) return json({ error: 'index reader not configured' }, 503)
  const gated = await requireViewer(ctx)
  if (gated instanceof Response) return gated
  const url = new URL(request.url)
  const path = (url.searchParams.get('path') ?? '').replace(/\/+$/, '')
  if (path.includes('..') || path.startsWith('/')) return json({ error: 'bad path' }, 400)

  // Every scan with a synced floor-free index, oldest first.
  const rows = await env.DB.prepare("SELECT DISTINCT date FROM index_schema WHERE variant = 'path' ORDER BY date").all<{ date: string }>()
  const dates = rows.results.map(r => r.date)
  const cacheKey = new Request(`https://series.cache/${encodeURIComponent(path)}?d=${dates.join(',')}`)
  const cache = (caches as unknown as { default: Cache }).default
  const hit = await cache.match(cacheKey)
  if (hit) return hit

  const points: { date: string; b: number; o: number }[] = []
  // One scan's point; a D1 hiccup ("internal error") gets one more try, and
  // anything else unreadable is a missing point, not a failed chart — logged,
  // since a silently absent point looks like a gap in the data.
  const point = async (date: string, tries = 2): Promise<{ date: string; b: number; o: number } | null> => {
    try {
      const a = await readRootAgg(env, { date, path })
      return a ? { date, ...a } : null
    } catch (e) {
      const msg = (e as Error).message
      if (tries > 1 && /internal error/i.test(msg)) return point(date, tries - 1)
      console.log(`series ${path || '/'}: no point for ${date}: ${msg}`)
      return null
    }
  }
  // Scans are independent reads (each a few D1 round trips and one range
  // GET); a dozen at a time keeps a 60-scan history to ~5 rounds.
  for (let i = 0; i < dates.length; i += 12) {
    const got = await Promise.all(dates.slice(i, i + 12).map(d => point(d)))
    for (const g of got) if (g) points.push(g)
  }
  const res = json({ path, points }, 200, { 'cache-control': 'private, max-age=300' })
  await cache.put(cacheKey, res.clone())
  return res
}

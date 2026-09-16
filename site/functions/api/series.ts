/** Bytes under a path per scan — the size-over-time chart, following the
 * drill like the map (specs/view-serving.md §1 "series.json" → this).
 *
 *   GET /api/series?path=<P>
 *
 * One point per scan the index knows: P's own row in that scan's coarsest
 * tier that holds it (or the floor-free tier). Nothing is precomputed per
 * prefix and nothing is floored: a path below every tier falls through to
 * the floor-free tier. Scans predating the index have no per-prefix point —
 * but for the whole-bucket series (empty path) their `meta.json` total is as
 * good as an index row, so those scans are filled in from the snapshot dir:
 * the chart keeps the history that never got (or can't get) tiers, e.g. the
 * 2026-08-15..19 CoreWeave scans whose listings were transient.
 * gcs's endpoint minus its lens/owner axes.
 */
import { type Ctx, json, requireViewer } from '../_lib/auth.js'
import { makeStore } from '../_lib/index.js'
import { readRootAgg } from '../_lib/view.js'

// Snapshot dirs for this deployment's store (`snapshots/<store>/`; gcs's default
// store is the bare `snapshots/`), and the scan-id shape they're named by.
const SNAPSHOTS_PREFIX = 'snapshots/cw/'
const DATE_RE = /^\d{4}-\d{2}-\d{2}(T\d{4})?$/

/** Scans present as snapshot dirs but absent from the index (oldest first). */
async function unindexedScans(env: Ctx['env'], indexed: Set<string>): Promise<string[]> {
  const store = makeStore(env)
  const out: string[] = []
  let cursor: string | undefined
  do {
    const page = await store.list(SNAPSHOTS_PREFIX, { cursor })
    for (const e of page.entries) {
      const d = e.key.slice(SNAPSHOTS_PREFIX.length).replace(/\/$/, '')
      if (e.isDir && DATE_RE.test(d) && !indexed.has(d)) out.push(d)
    }
    cursor = page.cursor
  } while (cursor)
  return out.sort()
}

/** A whole-bucket point from a scan's `meta.json` (no tiers needed). */
async function metaPoint(env: Ctx['env'], date: string): Promise<{ date: string; b: number; o: number } | null> {
  try {
    const { bytes } = await makeStore(env).get(`${SNAPSHOTS_PREFIX}${date}/meta.json`)
    const m = JSON.parse(new TextDecoder().decode(bytes)) as { total_bytes?: number; total_objects?: number }
    return typeof m.total_bytes === 'number' ? { date, b: m.total_bytes, o: m.total_objects ?? 0 } : null
  } catch (e) {
    console.log(`series /: no meta.json point for ${date}: ${(e as Error).message}`)
    return null
  }
}

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
  // Whole-bucket series only: scans without tiers still have a total in meta.json.
  const extra = path === '' ? await unindexedScans(env, new Set(dates)) : []
  const cacheKey = new Request(`https://series.cache/${encodeURIComponent(path)}?d=${dates.join(',')}&x=${extra.join(',')}`)
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
  for (let i = 0; i < extra.length; i += 12) {
    const got = await Promise.all(extra.slice(i, i + 12).map(d => metaPoint(env, d)))
    for (const g of got) if (g) points.push(g)
  }
  points.sort((a, b) => a.date.localeCompare(b.date))
  const res = json({ path, points }, 200, { 'cache-control': 'private, max-age=300' })
  await cache.put(cacheKey, res.clone())
  return res
}

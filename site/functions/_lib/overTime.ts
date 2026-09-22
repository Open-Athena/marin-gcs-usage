// The cross-scan over-time index reader (specs/obs-axis-indexing.md Phase 1):
// one footer-pruned `(depth, path)` read of the SCD-2 interval singleton,
// expanded into a `(b, o)` point per scan via pyrmts' `seriesFor` — replacing
// `/api/series`'s one point read per scan generation.
//
// The interval→line expansion is pyrmts' generic primitive; cw supplies only
// the range IO. (pyrmts' whole-file `readMultiScan` would load the entire
// fleet index into a 128 MB isolate, so cw does the footer-pruned read and
// hands the matched rows to `seriesFor` as a partial `MultiScan`.) The index is
// a singleton pointed to in D1 as variant `over-time` under the latest scan it
// was built for; the ordered scan list rides an `over-time.scans.json` sidecar
// (the D1-footer read path rebuilds row-group stats but not KV metadata).
import type { Env } from './auth.js'
import { indexDir, indexKey, makeStore, num, openIndex, readPoint } from './index.js'
import { type Dim, type Metric, type MultiScan, type Pyramid, seriesFor } from 'pyrmts'

const COLS = ['depth', 'path', 'b', 'o', '__scan_lo', '__scan_hi']

// cw's over-time shape as a pyrmts logical schema: key `(depth, path)`, state
// `(b, o)` (mirrors `over_time_pyramid()` in the producer). `count` gives one
// state column per metric.
type Schema = Pick<Pyramid, 'binCol' | 'dims' | 'metrics'>
const SCHEMA: Schema = {
  binCol: 'depth',
  dims: [{ name: 'path', type: 'string' } as Dim],
  metrics: [{ name: 'b', monoid: 'count' } as Metric, { name: 'o', monoid: 'count' } as Metric],
}

/** A path's `scan → {b,o}` line from over-time interval rows via pyrmts'
 * `seriesFor`, dropping absent (monoid-identity `0/0`) scans so the chart keeps
 * gap semantics (a path missing from a scan has no point, not `b=0`). */
export function seriesToMap(rows: Record<string, unknown>[], scans: string[], depth: number, path: string): Map<string, { b: number; o: number }> {
  const ms: MultiScan = { rows: rows as MultiScan['rows'], scans, encoder: 'interval' }
  const out = new Map<string, { b: number; o: number }>()
  for (const p of seriesFor(ms, SCHEMA, { depth, path })) {
    const b = num(p.state.b)
    const o = num(p.state.o)
    if (b !== 0 || o !== 0) out.set(p.scan, { b, o })
  }
  return out
}

/** The latest scan date carrying an `over-time` pointer; null when none synced. */
export async function latestOverTimeDate(env: Env): Promise<string | null> {
  if (!env.DB) return null
  const r = await env.DB.prepare("SELECT date FROM index_schema WHERE variant = 'over-time' ORDER BY date DESC LIMIT 1").first<{ date: string }>()
  return r?.date ?? null
}

const scansCache = new Map<string, Promise<string[]>>()

/** The ordered scan list from the sidecar beside the index (cached per dir). */
async function overTimeScans(env: Env, dir: string): Promise<string[]> {
  let p = scansCache.get(dir)
  if (!p) {
    p = (async () => {
      const key = indexKey(dir, 'over-time').replace(/over-time\.parquet$/, 'over-time.scans.json')
      const { bytes } = await makeStore(env).get(key)
      return JSON.parse(new TextDecoder().decode(bytes)) as string[]
    })()
    scansCache.set(dir, p)
  }
  try {
    return await p
  } catch (e) {
    scansCache.delete(dir)
    throw e
  }
}

/** A path's `scan → {b,o}` line from the over-time index, or null when the
 * index isn't synced or doesn't hold the path (caller falls back to per-scan
 * reads). One footer-pruned point read + a cached sidecar fetch. */
export async function readOverTime(env: Env, path: string): Promise<Map<string, { b: number; o: number }> | null> {
  const date = await latestOverTimeDate(env)
  if (!date) return null
  const dir = await indexDir(env, date, 'over-time')
  if (!dir) return null
  const depth = path === '' ? 0 : path.split('/').length
  let raw: Record<string, unknown>[]
  try {
    raw = await readPoint(await openIndex(env, date, 'over-time'), depth, path, COLS)
  } catch (e) {
    if (/not synced/.test((e as Error).message)) return null
    throw e
  }
  if (!raw.length) return null
  const scans = await overTimeScans(env, dir)
  return seriesToMap(raw, scans, depth, path)
}

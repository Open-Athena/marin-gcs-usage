/** `/api/name-summary` from the static suffix shards (`staticNames.ts`), dev only (`NAME_SUMMARY_STATIC=1`
 *  with the `INDEX_R2` binding): a below-catalog literal on a consolidated scan is answered by the Worker
 *  from R2, in the body shape the query box returns for its bounded postings plan, labeled
 *  `STATIC_SOURCE`. Everything else — catalog literals, frozen and daily scans, literals under three
 *  characters — still goes to the box, unchanged.
 *
 *  "Below the catalog" is decided without the catalog: a scan's registry admits a literal when at least
 *  `threshold_paths` live paths have a name containing it, and every such path is at least one suffix row
 *  in the literal's range, so a range under the threshold (counted in whole row groups, an upper bound) is
 *  never a registered literal on any date. Bucket names (depth 0) are not suffix rows, so a literal one of
 *  them contains goes to the box too. The box is still asked for the registry (which scans exist, their
 *  identities) and, once per preorder scan, its bucket geometry (a catalog answer's `pre`/`post`). */
import { datedNameRequest, parseName, STATIC_SOURCE } from '../../src/nameModel.js'
import { HOT_SCOPE } from '../../src/hotModel.js'
import { json } from './auth.js'
import { privateHeaders } from './hotL1.js'
import { cacheIndexes, type Io, r2Blobs, STATIC_GEN, StaticNames } from './staticNames.js'

export type StaticNameEnv = { NAME_SUMMARY_STATIC?: string; INDEX_R2?: R2Bucket; STATIC_MAX_ROWS?: string }
/** Ask the box (`nameSummary.ts`' backend): the registry without params, else a summary. */
export type Backend = (params?: URLSearchParams) => Promise<Response>

/** Above this many suffix rows the box answers, whatever the threshold (the decode budget). */
const MAX_ROWS = 400_000
const TTL_MS = 5 * 60_000
const CAPABILITIES = { bucket_drill: false, child_drill: false, fallback: false }
const VALIDATION = {
  description: `exact first-hit totals over the static suffix shards (generation ${STATIC_GEN}, rebuilt from the scans and checked against the consolidated store offline); no per-request source oracle`,
  source_prefix_proofs_checked: true, independent_full_catalog_source_oracle: false,
}

interface RegistryRow { date: string; kind: string; plans: string[]; source?: Record<string, unknown>; registry?: Record<string, unknown> }
interface Registry { logical_store: string; bucket_paths: string[]; dates: RegistryRow[] }
type Geometry = Map<string, [number, number]>

let reader: { r2: R2Bucket; names: StaticNames } | undefined
let registry: { at: number; value: Promise<Registry> } | undefined
let scans: Promise<Set<string>> | undefined
const geometry = new Map<string, Promise<Geometry>>()

// The Workers clock only advances across I/O; a cache miss pins "now" after CPU-bound work (decode).
const tick = async () => { await caches.default.match('https://static-names.invalid/tick'); return Date.now() }

function names(r2: R2Bucket): StaticNames {
  if (reader?.r2 !== r2) reader = { r2, names: new StaticNames(r2Blobs(r2), cacheIndexes(caches.default), tick) }
  return reader.names
}
async function boxJson<T>(response: Response): Promise<T> {
  if (response.status !== 200) { await response.body?.cancel(); throw new Error(`box ${response.status}`) }
  return response.json() as Promise<T>
}
function getRegistry(box: Backend): Promise<Registry> {
  if (!registry || Date.now() - registry.at > TTL_MS) {
    const value = box().then(r => boxJson<Registry>(r))
    registry = { at: Date.now(), value }
    value.catch(() => { if (registry?.value === value) registry = undefined })
  }
  return registry.value
}
function getScans(r2: R2Bucket): Promise<Set<string>> {
  scans ??= r2Blobs(r2).json<{ scans: { id: string }[] }>('scans.json').then(d => new Set(d.scans.map(s => s.id)))
  scans.catch(() => { scans = undefined })
  return scans
}
/** A scan's bucket bounds: one ordinal position per bucket, or (preorder) a catalog answer's. */
function getGeometry(box: Backend, reg: Registry, row: RegistryRow): Promise<Geometry> {
  if (row.source?.geometry === 'ordinal') return Promise.resolve(new Map(reg.bucket_paths.map((p, i) => [p, [i + 1, i + 1]])))
  let held = geometry.get(row.date)
  if (!held) {
    // Every literal of ≤ 2 characters is registered (`short_chars`), so this is a catalog answer.
    held = box(new URLSearchParams({ date: row.date, name: 'zz' })).then(r => boxJson<{ buckets: { path: string; pre: number; post: number }[] }>(r))
      .then(b => new Map(b.buckets.map(x => [x.path, [x.pre, x.post] as [number, number]])))
    geometry.set(row.date, held)
    held.catch(() => geometry.delete(row.date))
  }
  return held
}

const safe = (v: bigint): number => { const n = Number(v); if (!Number.isSafeInteger(n)) throw new Error('static names: total exceeds 2^53'); return n }

/** The static answer, or null when the box should answer (with why, for the `x-static-skip` header). */
export async function askStatic(env: StaticNameEnv, params: URLSearchParams, box: Backend): Promise<Response | { skip: string }> {
  const r2 = env.INDEX_R2!
  const request = datedNameRequest(params)
  const key = request.name
  if ([...key].length < 3) return { skip: 'short' }
  const [reg, have] = await Promise.all([getRegistry(box), getScans(r2)])
  const days = request.from ? [request.from, request.date] : [request.date]
  const rows = days.map(d => reg.dates.find(r => r.date === d))
  if (rows.some(r => !r || r.kind !== 'consolidated-store-v1' || !r.plans.includes('bounded-name-postings'))) return { skip: 'scan-kind' }
  if (days.some(d => !have.has(d))) return { skip: 'scan-not-in-generation' }
  if (reg.bucket_paths.some(p => p.toLowerCase().includes(key))) return { skip: 'bucket-name' }
  let budget = Number(env.STATIC_MAX_ROWS ?? MAX_ROWS)
  for (const r of rows) {
    if (!r!.registry) continue
    const t = r!.registry.threshold_paths
    if (typeof t !== 'number') return { skip: 'registry-weight' }
    budget = Math.min(budget, t - 1)
  }
  const t0 = Date.now()
  const { io, answer } = await names(r2).answer(key, days, budget)
  if (!answer) return { skip: `rows>${budget}` }
  const geoms = await Promise.all(rows.map(r => getGeometry(box, reg, r!)))
  const sides = rows.map((r, i) => {
    const totals = answer.answers[r!.date], geo = geoms[i]
    if (Object.keys(totals).some(b => !geo.has(b))) throw new Error('static names: a bucket outside the scan geometry')
    const buckets = reg.bucket_paths.map(path => {
      const [pre, post] = geo.get(path)!, [b, o] = totals[path] ?? [0n, 0n]
      return { path, pre, post, b: safe(b), o: safe(o) }
    })
    const source_identity: Record<string, unknown> = { kind: 'consolidated-store-v1', ...r!.source }
    return {
      schema: 'dated-name-summary-v1', logical_store: reg.logical_store, target: source_identity.target, date: r!.date, pattern: key,
      path: '', exact: true, incremental: false, levels: 1, scope: HOT_SCOPE, plan: 'bounded-name-postings', source: STATIC_SOURCE,
      source_identity, validation: { ...VALIDATION }, capabilities: { ...CAPABILITIES },
      root: { b: buckets.reduce((s, x) => s + x.b, 0), o: buckets.reduce((s, x) => s + x.o, 0) }, buckets,
      ...(r!.registry ? { registry: r!.registry } : {}),
    }
  })
  let body: unknown = sides[0]
  if (request.from) {
    const [before, after] = sides
    const d = (a: { b: number; o: number }, b: { b: number; o: number }) => ({ b: b.b - a.b, o: b.o - a.o })
    body = {
      schema: 'dated-name-summary-diff-v1', logical_store: reg.logical_store, from: request.from, date: request.date, pattern: key, path: '',
      exact: true, incremental: false, levels: 1, scope: HOT_SCOPE, before, after, delta: d(before.root, after.root), capabilities: { ...CAPABILITIES },
      buckets: before.buckets.map((a, i) => { const b = after.buckets[i]; return { path: a.path, before: { pre: a.pre, post: a.post, b: a.b, o: a.o }, after: { pre: b.pre, post: b.post, b: b.b, o: b.o }, delta: d(a, b) } }),
    }
  }
  parseName(body, request) // the page's own contract check, before it leaves the Worker
  const total = Date.now() - t0
  return json(body, 200, {
    ...privateHeaders, 'x-query-engine': 'name-summary-static', 'x-static-io': JSON.stringify(ioHeader(io)),
    'server-timing': [...Object.entries(io.ms).map(([k, v]) => `${k};dur=${v}`), `static;dur=${total}`].join(', '),
  })
}

const ioHeader = (io: Io) => ({ gen: STATIC_GEN, ...io })

/** Dispatch: the static reader when enabled and it applies, else the box; a static failure falls back to the box. */
export async function withStatic(env: StaticNameEnv, params: URLSearchParams, box: Backend): Promise<Response> {
  if (env.NAME_SUMMARY_STATIC !== '1' || !env.INDEX_R2) return box(params)
  let skip: string
  try {
    const out = await askStatic(env, params, box)
    if (out instanceof Response) return out
    skip = out.skip
  } catch (error) {
    console.error('static name summary failed; asking the box', error)
    skip = 'error'
  }
  const response = await box(params)
  const headers = new Headers(response.headers)
  headers.set('x-static-skip', skip)
  return new Response(response.body, { status: response.status, headers })
}

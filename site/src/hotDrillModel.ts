import { HOT_DATES, HOT_SCOPE, HotUnavailable, hotRequest, type HotRequest, type HotWeights } from './hotModel'

export interface HotDrillRequest extends HotRequest { path: string }
export interface HotChild extends HotWeights { path: string; name: string; pre: number; post: number }
export interface HotDrillView { date: string; pattern: string; path: string; root: HotWeights; children: HotChild[]; other: HotWeights }
export interface HotDrillResult { after: HotDrillView; before?: HotDrillView; threshold: number; budget: number }
const fail = (): never => { throw new Error('Bucket detail returned an unsupported or inconsistent exact partition.') }
const record = (v: unknown): Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v) ? v as Record<string, unknown> : fail()
const ordinal = (v: unknown): number => typeof v === 'number' && Number.isSafeInteger(v) && v >= 0 ? v : fail()
function decimal(v: unknown, signed = false): number {
  if (typeof v !== 'string' || !(signed ? /^(0|-?[1-9]\d*)$/ : /^(0|[1-9]\d*)$/).test(v)) fail()
  const n = BigInt(v as string), limit = BigInt(Number.MAX_SAFE_INTEGER)
  return n >= -limit && n <= limit ? Number(n) : fail()
}
const weights = (v: unknown, signed = false): HotWeights => { const r = record(v); return { b: decimal(r.b, signed), o: decimal(r.o, signed) } }
const same = (a: HotWeights, b: HotWeights): boolean => a.b === b.b && a.o === b.o
function dated(v: unknown, side?: 'before' | 'after'): HotWeights {
  const r = record(v), value = weights(side ? r[side] : r)
  if (side) {
    const before = weights(r.before), after = weights(r.after)
    if (!same(weights(r.delta, true), { b: after.b - before.b, o: after.o - before.o })) fail()
  }
  return value
}
export function hotDrillRequest(params: URLSearchParams): HotDrillRequest {
  const paths = params.getAll('path'), root = new URLSearchParams(params)
  root.delete('path')
  if (paths.length !== 1 || !paths[0] || /[\/\0]/.test(paths[0]) || [...paths[0]].length > 512) throw new Error('Choose one bucket; deeper paths are not available.')
  return { ...hotRequest(root), path: paths[0] }
}
export function hotBucketParams(request: HotRequest, path?: string): URLSearchParams {
  return new URLSearchParams({ name: request.name, date: request.date, ...(request.from ? { from: request.from } : {}), ...(path ? { path } : {}) })
}
export function hotBucketHref(request: HotRequest, path?: string): string { return `/hot?${hotBucketParams(request, path)}` }
function validation(value: unknown, pattern: string): void {
  const v = record(value), c = record(v.covered_control), q = record(v.query_oracle)
  const covered = 'complete ordinary recursive bucket/frame rollups on both dates'
  if (typeof v.artifact_sha256 !== 'string' || !/^[0-9a-f]{64}$/.test(v.artifact_sha256) || ordinal(v.artifact_bytes) < 1 ||
      v.prefix_proofs_checked !== true || v.full_catalog_source_oracle !== false || c.pattern !== 'm' || c.validation !== covered ||
      ordinal(c.frames_checked) < 1 || ordinal(c.buckets_checked) < 1) fail()
  ordinal(c.heavy_cells_checked)
  if (pattern === 'm') {
    if (['pattern', 'validation', 'frames_checked', 'heavy_cells_checked', 'buckets_checked'].some(key => q[key] !== c[key])) fail()
  } else {
    if (q.pattern !== pattern || ordinal(q.predicate_id) < 1) fail()
    if (q.checked === true) {
      if (ordinal(q.frame_id) < 1 || ordinal(q.interval_span) < 1 || q.validation !== 'complete scoped first-hit full-path frontier on both dates') fail()
    } else if (q.checked !== false || !['selected-cell budget exhausted', 'no emitted cell within interval cap'].includes(q.reason as string)) fail()
  }
}
export function parseHotDrill(value: unknown, request: HotDrillRequest): HotDrillResult {
  const v = record(value), geometry = record(v.geometry), cutoff = record(v.cutoff), caps = record(v.capabilities)
  const pre = ordinal(geometry.pre), post = ordinal(geometry.post), budget = ordinal(cutoff.budget), threshold = decimal(cutoff.threshold_bytes)
  const diff = !!request.from
  if (v.schema !== (diff ? 'hot-l2-pair-catalog-diff-v1' : 'hot-l2-pair-catalog-v1') || v.exact !== true || v.incremental !== false || v.levels !== 2 ||
      v.scope !== HOT_SCOPE || v.source !== 'registered accepted paired sparse artifact' || typeof v.target !== 'string' || !/^[a-z][a-z0-9_]*$/.test(v.target) ||
      v.pattern !== request.name || v.path !== request.path || pre < 1 || post < pre || budget < 1 || budget > 4096 || threshold < 1 ||
      cutoff.scope !== 'fixed per query and bucket root across all depth-2 children; not arbitrary-prefix refinement' ||
      JSON.stringify(cutoff.dates) !== JSON.stringify(HOT_DATES) || caps.bucket_roots_only !== true || caps.child_drill !== false || caps.filters !== false ||
      !Array.isArray(v.children) || v.children.length > 2 * budget ||
      (diff ? JSON.stringify(v.dates) !== JSON.stringify([request.from, request.date]) : v.date !== request.date)) fail()
  validation(v.validation, request.name)
  const rawChildren = v.children as unknown[]
  let end = pre
  const identities = new Set<string>()
  for (const child of rawChildren) {
    const r = record(child), a = ordinal(r.pre), b = ordinal(r.post)
    if (typeof r.name !== 'string' || !r.name || /[\/\0]/.test(r.name) || r.path !== `${request.path}/${r.name}` ||
        identities.has(r.name) || a <= end || b < a || b > post || r.drill !== false) fail()
    identities.add(r.name as string); end = b
  }
  if (record(v.other).drill !== false) fail()
  const view = (date: string, side?: 'before' | 'after'): HotDrillView => {
    const root = dated(v.root, side), other = dated(v.other, side)
    const children = rawChildren.map(child => { const r = record(child); return {
      path: r.path as string, name: r.name as string, pre: ordinal(r.pre), post: ordinal(r.post), ...dated(r, side),
    } })
    const sum = children.reduce((a, r) => ({ b: a.b + BigInt(r.b), o: a.o + BigInt(r.o) }), { b: BigInt(other.b), o: BigInt(other.o) })
    if (sum.b !== BigInt(root.b) || sum.o !== BigInt(root.o)) fail()
    return { date, pattern: request.name, path: request.path, root, children, other }
  }
  const after = view(request.date, diff ? 'after' : undefined), before = diff ? view(request.from!, 'before') : undefined
  if (before && after.children.some((r, i) => Math.max(r.b, before.children[i].b) < threshold)) fail()
  return { after, ...(before ? { before } : {}), threshold, budget }
}
export async function loadHotDrill(request: HotDrillRequest, signal?: AbortSignal): Promise<HotDrillResult> {
  const checked = hotDrillRequest(hotBucketParams(request, request.path))
  const response = await fetch(`/api/hot-l2?${hotBucketParams(checked, checked.path)}`, { signal })
  if (!response.ok) {
    if ([400, 404, 409, 422].includes(response.status)) throw new HotUnavailable('Bucket detail is unavailable for this literal/date. It is not a zero-match result; return to All buckets for the root summary.')
    throw new Error(response.status === 401 ? 'Sign in to view bucket detail.' : `Bucket-detail request failed (${response.status}). Try again.`)
  }
  return parseHotDrill(await response.json(), checked)
}

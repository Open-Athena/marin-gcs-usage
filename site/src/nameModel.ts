import { HOT_DATES, HOT_SCOPE, hotRequest, parseRootSummary, type HotBucket, type HotRequest, type HotResult, type HotView, type HotWeights } from './hotModel'

export type NamePlan = 'catalog' | 'bounded-name-postings'
export interface NameQualification { qualification_dates: string[]; target: string; patterns: number; selection_contract: string; threshold_paths?: number; threshold_rows?: number; name_rows?: number; max_chars?: number | null; short_chars?: number }
export interface NameExecution {
  plan: NamePlan
  source: string
  validation: Record<string, unknown> & { description: string }
  source_identity: { generation?: string; snapshot_db?: string; history_manifest_sha256?: string; kind?: NameScanKind; target?: string; artifact_sha256?: string; artifact_bytes?: number; source_manifest_sha256?: string; source_prefix_proofs_checked?: true; postings?: string; through?: string; geometry?: 'preorder' | 'ordinal'; catalog?: string }
  registry?: NameQualification
}
export interface NameResult extends HotResult { execution: { after: NameExecution; before?: NameExecution }; logical_store?: string; capabilities?: { bucket_drill: false; child_drill: false; fallback: false } }
/** `consolidated-store-v1`: a scan only the store holds. With the store's consolidated catalog (`catalog` in its identity),
 * literals registered on the scan itself answer from it and the rest on demand from the store's one consolidated name
 * index; without one, every literal answers on demand. Its bucket bounds are path preorder, or (a scan recording no
 * descendant counts) one `ordinal` position per bucket. */
export type NameScanKind = 'frozen-history' | 'daily-scalar-source-v1' | 'consolidated-store-v1'
export interface NameScan { date: string; plans: NamePlan[]; kind: NameScanKind; qualification_dates?: string[]; source_identity?: NameExecution['source_identity']; registry?: NameQualification }
export interface NameRegistry { dated: boolean; dates: NameScan[]; logical_store?: string; bucket_paths?: string[] }
export function namePageParams(params: URLSearchParams): URLSearchParams {
  const next = new URLSearchParams(params)
  if (!next.has('name')) next.set('name', 'datakit')
  return next
}
export function nameRequest(params: URLSearchParams, availableDates?: readonly string[]): HotRequest {
  if (!availableDates) return hotRequest(namePageParams(params))
  const request = datedNameRequest(params)
  if (!availableDates.includes(request.date) || (request.from !== undefined && !availableDates.includes(request.from))) throw new Error('This scan is unavailable in the name-summary registry; it is not a zero-match result.')
  return request
}
export function datedNameRequest(params: URLSearchParams): HotRequest {
  const values: Record<string, string> = {}
  namePageParams(params).forEach((value, key) => {
    if (!['date', 'name', 'from'].includes(key) || Object.hasOwn(values, key)) throw new Error('Use only one date, name and optional from parameter.')
    values[key] = value
  })
  const date = values.date ?? HOT_DATES[1], name = values.name?.toLowerCase(), from = values.from
  if (!iso(date) || (from !== undefined && !iso(from))) throw new Error('Use valid ISO scan dates.')
  if (from !== undefined && from >= date) throw new Error('Baseline scan must precede the selected scan.')
  if (!name || name.includes('/') || name.includes('\0') || [...name].length > 512) throw new Error('Enter one nonempty name literal without slashes (at most 512 characters).')
  return { date, name, ...(from === undefined ? {} : { from }) }
}
const record = (value: unknown): Record<string, unknown> => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Name summary returned invalid execution metadata.')
  return value as Record<string, unknown>
}
function execution(value: unknown): NameExecution {
  const body = record(value), validation = record(body.validation), identity = record(body.source_identity)
  if ((body.plan !== 'catalog' && body.plan !== 'bounded-name-postings') || typeof body.source !== 'string' || !body.source.trim() ||
      typeof validation.description !== 'string' || !validation.description.trim() || validation.source_prefix_proofs_checked !== true || validation.independent_query_source_oracle !== false ||
      typeof identity.generation !== 'string' || !identity.generation || typeof identity.snapshot_db !== 'string' || !/^[a-z][a-z0-9_]*$/.test(identity.snapshot_db) ||
      typeof identity.history_manifest_sha256 !== 'string' || !/^[0-9a-f]{64}$/.test(identity.history_manifest_sha256)) throw new Error('Name summary returned invalid execution metadata.')
  if (body.plan === 'catalog') record(validation.catalog)
  else if (Object.hasOwn(validation, 'catalog')) throw new Error('Name summary returned invalid execution metadata.')
  return { plan: body.plan, source: body.source, validation: { ...validation, description: validation.description },
    source_identity: { generation: identity.generation, snapshot_db: identity.snapshot_db, history_manifest_sha256: identity.history_manifest_sha256 } }
}
const iso = (value: unknown): value is string => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value) && value.slice(0, 4) !== '0000' && Number.isFinite(Date.parse(value)) && new Date(value).toISOString().slice(0, 10) === value
const id = (value: unknown): value is string => typeof value === 'string' && /^[a-z][a-z0-9_]*$/.test(value)
const hash = (value: unknown): value is string => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value)
const generation = (value: unknown): value is string => typeof value === 'string' && /^[a-f0-9]{32}$/.test(value)
function fail(): never { throw new Error('Name summary returned an invalid dated contract.') }
const integer = (value: unknown, signed = false): number => typeof value === 'number' && Number.isSafeInteger(value) && (signed || value >= 0) ? value : fail()
const keys = (body: Record<string, unknown>, expected: string[]) => Object.keys(body).sort().join(',') === [...expected].sort().join(',')
const weights = (value: unknown, signed = false): HotWeights => {
  const body = record(value)
  if (!keys(body, ['b', 'o'])) fail()
  return { b: integer(body.b, signed), o: integer(body.o, signed) }
}
const same = (a: HotWeights, b: HotWeights) => a.b === b.b && a.o === b.o
const delta = (a: HotWeights, b: HotWeights): HotWeights => ({ b: integer(b.b - a.b, true), o: integer(b.o - a.o, true) })
function capabilities(value: unknown) {
  const body = record(value)
  if (!keys(body, ['bucket_drill', 'child_drill', 'fallback']) || Object.values(body).some(value => value !== false)) fail()
  return { bucket_drill: false, child_drill: false, fallback: false } as const
}
/** A daily scan's plans: its registered catalog, or bounded discovery over its own name index or the consolidated store's. */
const CONSOLIDATED_SOURCE = 'bounded name postings over the consolidated store; directory rollups are atomic'
const CONSOLIDATED_CATALOG_SOURCE = "the consolidated catalog: every scan's registered literals precomputed in the store"
/** A consolidated scan's below-catalog literal answered by the Worker from the static suffix shards on R2
 *  (`functions/_lib/staticNames.ts`, dev `NAME_SUMMARY_STATIC=1`), not the query box. */
export const STATIC_SOURCE = 'static suffix postings on R2, one ranged read by the Worker; directory rollups are atomic'
const DAILY_SOURCES: Record<NamePlan, string[]> = {
  catalog: ['published dated precomputed batch artifact'],
  'bounded-name-postings': ["bounded dated name postings over the scan's own name index; directory rollups are atomic", CONSOLIDATED_SOURCE],
}
function consolidatedIdentity(value: unknown) {
  const body = record(value), cataloged = 'catalog' in body
  if (!keys(body, ['kind', 'target', 'postings', 'through', 'geometry', ...(cataloged ? ['catalog'] : [])]) || body.kind !== 'consolidated-store-v1' || !id(body.target) || !id(body.postings) || !iso(body.through) ||
      (body.geometry !== 'preorder' && body.geometry !== 'ordinal') || (cataloged && !id(body.catalog))) fail()
  return { kind: 'consolidated-store-v1' as const, target: body.target as string, postings: body.postings as string, through: body.through as string, geometry: body.geometry as 'preorder' | 'ordinal',
    ...(cataloged ? { catalog: body.catalog as string } : {}) }
}
/** A consolidated catalog's registry is the scan's own census: qualified on that scan alone, named by the catalog. */
function catalogRegistry(value: unknown, date: unknown, catalog: string): NameQualification {
  const registry = registryBinding(value, true)
  if (JSON.stringify(registry.qualification_dates) !== JSON.stringify([date]) || registry.target !== catalog) fail()
  return registry
}
const validationKeys = ['description', 'source_prefix_proofs_checked', 'independent_full_catalog_source_oracle']
function dailyIdentity(value: unknown) {
  const body = record(value)
  if (!keys(body, ['target', 'snapshot_db', 'generation', 'artifact_sha256', 'artifact_bytes', 'source_manifest_sha256', 'source_prefix_proofs_checked', 'kind']) ||
      body.kind !== 'daily-scalar-source-v1' || !id(body.target) || !id(body.snapshot_db) || body.target !== body.snapshot_db || !generation(body.generation) ||
      !hash(body.artifact_sha256) || integer(body.artifact_bytes) < 1 || !hash(body.source_manifest_sha256) || body.source_prefix_proofs_checked !== true) fail()
  return { kind: 'daily-scalar-source-v1' as const, target: body.target, snapshot_db: body.snapshot_db, generation: body.generation,
    artifact_sha256: body.artifact_sha256, artifact_bytes: integer(body.artifact_bytes), source_manifest_sha256: body.source_manifest_sha256,
    source_prefix_proofs_checked: true as const }
}
function datedExecution(body: Record<string, unknown>): NameExecution {
  const identity = record(body.source_identity)
  if (identity.kind === 'frozen-history') {
    if (!keys(identity, ['kind', 'generation', 'snapshot_db', 'history_manifest_sha256']) || !generation(identity.generation)) fail()
    const checked = execution(body)
    return { ...checked, source_identity: { ...checked.source_identity, kind: 'frozen-history' } }
  }
  const validation = record(body.validation)
  if (identity.kind === 'consolidated-store-v1') {
    const checked = consolidatedIdentity(identity), catalog = checked.catalog
    const plan = body.plan === 'catalog' && catalog ? 'catalog' : body.plan === 'bounded-name-postings' ? 'bounded-name-postings' : fail()
    if (!keys(body, ['schema', 'logical_store', 'target', 'date', 'pattern', 'path', 'exact', 'incremental', 'levels', 'scope', 'plan', 'source', 'source_identity', 'validation', 'capabilities', 'root', 'buckets', ...(catalog ? ['registry'] : [])]) ||
        (plan === 'catalog' ? body.source !== CONSOLIDATED_CATALOG_SOURCE : body.source !== CONSOLIDATED_SOURCE && body.source !== STATIC_SOURCE) || body.target !== checked.target || !keys(validation, validationKeys) ||
        typeof validation.description !== 'string' || !validation.description.trim() || validation.source_prefix_proofs_checked !== true || validation.independent_full_catalog_source_oracle !== false) fail()
    return { plan, source: body.source as string, validation: { description: validation.description, source_prefix_proofs_checked: true, independent_full_catalog_source_oracle: false }, source_identity: checked,
      ...(catalog ? { registry: catalogRegistry(body.registry, body.date, catalog) } : {}) }
  }
  const checked = dailyIdentity(identity)
  const plan = body.plan === 'catalog' || body.plan === 'bounded-name-postings' ? body.plan : fail()
  if (!keys(body, ['schema', 'logical_store', 'target', 'date', 'pattern', 'path', 'exact', 'incremental', 'levels', 'scope', 'plan', 'source', 'source_identity', 'registry', 'validation', 'capabilities', 'root', 'buckets']) || !DAILY_SOURCES[plan].includes(body.source as string) ||
      !keys(validation, validationKeys) ||
      typeof validation.description !== 'string' || !validation.description.trim() || validation.source_prefix_proofs_checked !== true || validation.independent_full_catalog_source_oracle !== false ||
      body.target !== checked.target) fail()
  const registry = registryBinding(body.registry, true)
  return { plan, source: body.source as string, validation: { description: validation.description, source_prefix_proofs_checked: true, independent_full_catalog_source_oracle: false }, source_identity: checked, registry }
}
function datedView(value: unknown): { view: HotView; execution: NameExecution; store: string } {
  const body = record(value)
  if (body.schema !== 'dated-name-summary-v1' || body.path !== '' || body.exact !== true || body.incremental !== false || body.levels !== 1 || body.scope !== HOT_SCOPE || !id(body.target) || !id(body.logical_store) || !iso(body.date) || typeof body.pattern !== 'string' ||
      datedNameRequest(new URLSearchParams({ name: body.pattern, date: body.date })).name !== body.pattern) fail()
  capabilities(body.capabilities)
  if (!Array.isArray(body.buckets) || body.buckets.length !== 6) fail()
  const buckets: HotBucket[] = (body.buckets as unknown[]).map(value => {
    const row = record(value)
    if (!keys(row, ['path', 'pre', 'post', 'b', 'o']) || typeof row.path !== 'string' || !row.path || row.path.includes('/') || row.path.includes('\0')) fail()
    return { path: row.path as string, pre: integer(row.pre), post: integer(row.post), ...weights({ b: row.b, o: row.o }) }
  })
  const order = [...buckets].sort((a, b) => a.pre - b.pre)
  if (new Set(buckets.map(row => row.path)).size !== 6 || order[0].pre !== 1 || order.some((row, i) => row.post < row.pre || (i > 0 && order[i - 1].post + 1 !== row.pre))) fail()
  const root = weights(body.root), sum = buckets.reduce((sum, row) => ({ b: integer(sum.b + row.b), o: integer(sum.o + row.o) }), { b: 0, o: 0 })
  if (!same(root, sum)) fail()
  return { view: { target: body.target as string, date: body.date as string, pattern: body.pattern as string, root, buckets: buckets.sort((a, b) => a.path < b.path ? -1 : a.path > b.path ? 1 : 0) }, execution: datedExecution(body), store: body.logical_store as string }
}
function parseDated(value: unknown, request: HotRequest): NameResult {
  const body = record(value), cap = capabilities(body.capabilities)
  const after = datedView(request.from ? body.after : body), before = request.from ? datedView(body.before) : undefined
  if (after.view.date !== request.date || after.view.pattern !== request.name || (before && (before.view.date !== request.from || before.view.pattern !== request.name || before.store !== after.store))) fail()
  if (!request.from) return { after: after.view, execution: { after: after.execution }, logical_store: after.store, capabilities: cap }
  if (!keys(body, ['schema', 'logical_store', 'from', 'date', 'pattern', 'path', 'exact', 'incremental', 'levels', 'scope', 'before', 'after', 'delta', 'capabilities', 'buckets']) || body.schema !== 'dated-name-summary-diff-v1' || body.from !== request.from || body.date !== request.date || body.logical_store !== after.store || body.pattern !== request.name || body.path !== '' || body.exact !== true || body.incremental !== false || body.levels !== 1 || body.scope !== HOT_SCOPE || !Array.isArray(body.buckets) || body.buckets.length !== 6) fail()
  const change = delta(before!.view.root, after.view.root)
  if (!same(weights(body.delta, true), change)) fail()
  const rows = new Map((body.buckets as unknown[]).map(value => { const row = record(value); return [row.path, row] }))
  if (rows.size !== 6) fail()
  before!.view.buckets.forEach((a, i) => {
    const b = after.view.buckets[i], row = rows.get(a.path)
    if (!row || a.path !== b.path || !keys(row, ['path', 'before', 'after', 'delta'])) fail()
    for (const [value, side] of [[row!.before, a], [row!.after, b]] as const) {
      const pair = record(value)
      if (!keys(pair, ['pre', 'post', 'b', 'o']) || integer(pair.pre) !== side.pre || integer(pair.post) !== side.post || !same(weights({ b: pair.b, o: pair.o }), side)) fail()
    }
    if (!same(weights(row!.delta, true), delta(a, b))) fail()
  })
  return { before: before!.view, after: after.view, delta: change, execution: { before: before!.execution, after: after.execution }, logical_store: after.store, capabilities: cap }
}
export function parseName(value: unknown, request: HotRequest): NameResult {
  if (record(value).schema === 'dated-name-summary-v1' || record(value).schema === 'dated-name-summary-diff-v1') return parseDated(value, request)
  const result = parseRootSummary(value, request, [{ view: 'name-summary-v1', diff: 'name-summary-diff-v1' }]), body = record(value)
  const after = execution(request.from ? body.after : body), before = request.from ? execution(body.before) : undefined
  if (before && (before.source_identity.generation !== after.source_identity.generation || before.source_identity.history_manifest_sha256 !== after.source_identity.history_manifest_sha256)) throw new Error('Name summary comparison has inconsistent frozen source identities.')
  return { ...result, execution: { after, ...(before ? { before } : {}) } }
}
export function nameHasDetail(result: NameResult): boolean {
  if (result.capabilities) return false
  return result.execution.after.plan === 'catalog' && (!result.before || result.execution.before?.plan === 'catalog')
}
export async function loadName(
  request: HotRequest,
  signal?: AbortSignal,
  availableDates?: readonly string[],
): Promise<NameResult> {
  const params = new URLSearchParams({ date: request.date, name: request.name, ...(request.from ? { from: request.from } : {}) })
  const checked = nameRequest(params, availableDates)
  params.set('name', checked.name)
  const response = await fetch(`/api/name-summary?${params}`, { signal })
  if (!response.ok) {
    throw new Error(response.status === 401 ? 'Sign in to view name summaries.' : response.status === 400
      ? availableDates ? 'This literal or scan is unavailable for the selected name-summary plan; it is not a zero-match result.' : 'Invalid name-summary request. Check the literal and frozen scan dates; this is not a zero-match result.'
      : 'Name summary is unavailable, busy or exceeded its work budget. This is not a zero-match result. Try again.')
  }
  return parseName(await response.json(), checked)
}

function registryBinding(value: unknown, daily: boolean): NameQualification {
  const body = record(value)
  // A daily registry may declare a short-literal domain: every literal of at most `short_chars` characters is registered whatever its frequency.
  const short = daily && 'short_chars' in body
  // Its threshold counts direct matching paths, or (`threshold_rows`, with `name_rows` charged per name) the consolidated name index's rows a literal costs to answer on demand.
  const rows = daily && 'threshold_rows' in body
  const threshold = rows ? ['threshold_rows', 'name_rows'] : ['threshold_paths']
  if (!keys(body, ['qualification_dates', 'target', 'patterns', 'selection_contract', ...(daily ? [...threshold, 'max_chars'] : []), ...(short ? ['short_chars'] : [])]) || !id(body.target) || integer(body.patterns) < 1 || !Array.isArray(body.qualification_dates) || !body.qualification_dates.length || body.qualification_dates.some(day => !iso(day)) || new Set(body.qualification_dates).size !== body.qualification_dates.length || body.selection_contract !== 'membership on declared qualification dates; no current-scan frequency claim' || (daily && (integer(rows ? body.threshold_rows : body.threshold_paths) < 1 || (rows && integer(body.name_rows) < 0) || (body.max_chars !== null && (integer(body.max_chars) < 1 || integer(body.max_chars) > 512))))) fail()
  return { qualification_dates: body.qualification_dates.map(day => iso(day) ? day : fail()), target: body.target, patterns: integer(body.patterns), selection_contract: body.selection_contract,
    ...(daily ? { ...(rows ? { threshold_rows: integer(body.threshold_rows), name_rows: integer(body.name_rows) } : { threshold_paths: integer(body.threshold_paths) }), max_chars: body.max_chars === null ? null : integer(body.max_chars) } : {}),
    ...(short ? { short_chars: integer(body.short_chars) < 1 ? fail() : integer(body.short_chars) } : {}) }
}
export function parseNameRegistry(value: unknown): NameRegistry {
  const body = record(value)
  if (body.schema === 'name-summary-registry-v1') {
    if (!keys(body, ['schema', 'target', 'dates', 'levels', 'scope', 'catalog_patterns', 'source_prefix_proofs_checked', 'cold_slots', 'catalog_slots', 'compute_seconds', 'work_bounds', 'cold_quarantined']) || !id(body.target) || body.levels !== 1 || body.scope !== HOT_SCOPE || JSON.stringify(body.dates) !== JSON.stringify(HOT_DATES) || body.source_prefix_proofs_checked !== true || body.cold_slots !== 1 || body.catalog_slots !== 2 || typeof body.compute_seconds !== 'number' || !Number.isFinite(body.compute_seconds) || body.compute_seconds <= 0 || typeof body.cold_quarantined !== 'boolean') fail()
    const bounds = record(body.work_bounds)
    if (!keys(bounds, ['max_names', 'max_postings', 'max_roots']) || Object.values(bounds).some(bound => integer(bound) < 1)) fail()
    const counts = record(body.catalog_patterns)
    if (Object.keys(counts).sort().join() !== [...body.dates as string[]].sort().join() || Object.values(counts).some(count => integer(count) < 1)) fail()
    return { dated: false, dates: (body.dates as string[]).map(date => ({ date, plans: ['catalog', 'bounded-name-postings'], kind: 'frozen-history' })) }
  }
  if (body.schema !== 'dated-name-summary-registry-v1' || !keys(body, ['schema', 'logical_store', 'bucket_paths', 'dates', 'levels', 'scope', 'daily_catalog_slots', 'legacy', 'capabilities']) || !id(body.logical_store) || body.levels !== 1 || body.scope !== HOT_SCOPE || body.daily_catalog_slots !== 2 || !Array.isArray(body.bucket_paths) || body.bucket_paths.length !== 6 || new Set(body.bucket_paths).size !== 6 || body.bucket_paths.some(path => typeof path !== 'string' || !path || path.includes('/') || path.includes('\0')) || !Array.isArray(body.dates) || !body.dates.length || body.dates.length > 400) fail()
  capabilities(body.capabilities)
  if (record(body.legacy).schema !== 'name-summary-registry-v1') fail()
  const legacy = parseNameRegistry(body.legacy), dates: NameScan[] = (body.dates as unknown[]).map(value => {
    const row = record(value)
    if (!iso(row.date)) fail()
    if (row.kind === 'consolidated-store-v1') {
      const source_identity = consolidatedIdentity({ ...record(row.source), kind: row.kind }), catalog = source_identity.catalog
      const plans: NamePlan[] = catalog ? ['catalog', 'bounded-name-postings'] : ['bounded-name-postings']
      if (!keys(row, ['date', 'plans', 'kind', 'source', ...(catalog ? ['registry'] : [])]) || JSON.stringify(row.plans) !== JSON.stringify(plans) || legacy.dates.some(day => day.date === row.date)) fail()
      if (source_identity.through < (row.date as string)) fail()
      if (!catalog) return { date: row.date as string, plans, kind: 'consolidated-store-v1' as const, source_identity }
      const registry = catalogRegistry(row.registry, row.date, catalog)
      return { date: row.date as string, plans, kind: 'consolidated-store-v1' as const, qualification_dates: [...registry.qualification_dates], source_identity, registry }
    }
    const registry = registryBinding(row.registry, row.kind === 'daily-scalar-source-v1')
    let source_identity: NameExecution['source_identity'] | undefined
    if (row.kind === 'frozen-history') {
      const original = record(body.legacy)
      if (!keys(row, ['date', 'plans', 'kind', 'registry']) || JSON.stringify(row.plans) !== JSON.stringify(['catalog', 'bounded-name-postings']) || !legacy.dates.some(day => day.date === row.date) || registry.target !== original.target || registry.patterns !== record(original.catalog_patterns)[row.date as string]) fail()
    } else {
      if (row.kind !== 'daily-scalar-source-v1' || !keys(row, ['date', 'plans', 'kind', 'registry', 'source', 'generation']) || !['["catalog"]', '["catalog","bounded-name-postings"]'].includes(JSON.stringify(row.plans)) || legacy.dates.some(day => day.date === row.date)) fail()
      source_identity = dailyIdentity({ ...record(row.source), kind: row.kind, generation: row.generation })
    }
    return { date: row.date as string, plans: row.plans as NamePlan[], kind: row.kind as NameScan['kind'], qualification_dates: [...registry.qualification_dates], ...(source_identity ? { source_identity, registry } : {}) }
  })
  if (new Set(dates.map(row => row.date)).size !== dates.length || legacy.dates.some(day => !dates.some(row => row.date === day.date))) fail()
  if (legacy.dated || dates.some((row, i) => i > 0 && dates[i - 1].date >= row.date)) fail()
  return { dated: true, dates, logical_store: body.logical_store as string, bucket_paths: [...body.bucket_paths as string[]] }
}
/** Bind even cached responses to current declared availability before display. */
export function nameResultForRegistry(result: NameResult, registry: NameRegistry): NameResult {
  if (registry.bucket_paths && JSON.stringify([...result.after.buckets.map(row => row.path)].sort()) !== JSON.stringify([...registry.bucket_paths].sort())) throw new Error('Name summary returned a different logical store or bucket set from the available-scan registry.')
  if (result.capabilities && result.logical_store !== registry.logical_store) throw new Error('Name summary returned a different logical store or bucket set from the available-scan registry.')
  const sides = [{ view: result.after, execution: result.execution.after }, ...(result.before ? [{ view: result.before, execution: result.execution.before! }] : [])]
  for (const { view, execution } of sides) {
    const scan = registry.dates.find(scan => scan.date === view.date)
    if (!scan || !scan.plans.includes(execution.plan) || scan.kind !== (execution.source_identity.kind ?? 'frozen-history')) throw new Error('Name summary returned a different scan or execution plan from the available-scan registry.')
    if (scan.source_identity && Object.entries(scan.source_identity).some(([key, value]) => record(execution.source_identity)[key] !== value)) throw new Error('Name summary returned a different pinned daily source from the available-scan registry.')
    if (scan.registry && JSON.stringify(scan.registry) !== JSON.stringify(execution.registry)) throw new Error('Name summary returned different registry qualification from the available-scan registry.')
  }
  return result
}
export async function loadNameRegistry(signal?: AbortSignal): Promise<NameRegistry> {
  const response = await fetch('/api/name-summary-registry', { signal })
  if (!response.ok) throw new Error(response.status === 401 ? 'Sign in to view available name-summary scans.' : 'Available name-summary scans could not be loaded. Try again; no dates or zero results have been inferred.')
  return parseNameRegistry(await response.json())
}

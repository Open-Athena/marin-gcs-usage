/** Isolated bounded reads of the published root-only hot catalog. No scan fallback. */
import { type Env, json } from './auth.js'
import { isScanId } from '../../src/scanSlug.js'

export type HotL1Env = Env & {
  QUERY_BOX_HOT_L1?: string
  QUERY_BOX_HOT_L1_URL?: string
  QUERY_BOX_TOKEN?: string
}
export const HOT_MAX_BYTES = 64 << 10
export const HOT_TIMEOUT_MS = 5000
export const privateHeaders = { 'cache-control': 'private, no-store' }

export class HotQueryError extends Error {}


/** URLSearchParams tolerates malformed UTF-8; decode strictly before constructing it. */
export function hotParams(url: URL): URLSearchParams {
  const parts = url.search.slice(1).split('&')
  if (!url.search || parts.length > 3) throw new HotQueryError('Use date, name and optional from only.')
  const params = new URLSearchParams()
  for (const part of parts) {
    const equals = part.indexOf('=')
    if (equals < 0) throw new HotQueryError('Invalid query parameters.')
    let key: string, value: string
    try {
      key = decodeURIComponent(part.slice(0, equals).replace(/\+/g, ' '))
      value = decodeURIComponent(part.slice(equals + 1).replace(/\+/g, ' '))
    } catch {
      throw new HotQueryError('Invalid query parameters.')
    }
    if (!['date', 'name', 'from'].includes(key)) throw new HotQueryError('Use date, name and optional from only.')
    if (params.has(key)) throw new HotQueryError('Duplicate query parameter.')
    params.set(key, value)
  }
  const date = params.get('date') ?? '', name = params.get('name') ?? '', from = params.get('from')
  if (!isScanId(date)) throw new HotQueryError('A valid scan date is required.')
  if (!name || name.includes('/') || name.includes('\0') || Array.from(name).length > 512) {
    throw new HotQueryError('Use one nonempty NUL/slash-free literal of at most 512 characters.')
  }
  if (from !== null && (!isScanId(from) || from >= date)) throw new HotQueryError('from must be a valid earlier scan date.')
  return params
}

const record = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === 'object' && !Array.isArray(value)
const scalar = (value: unknown): boolean => typeof value === 'number' && Number.isFinite(value) && Number.isInteger(value) && value >= 0
const weights = (value: unknown): boolean => record(value) && scalar(value.b) && scalar(value.o)
const signedWeights = (value: unknown): boolean => record(value) && [value.b, value.o].every(v => typeof v === 'number' && Number.isFinite(v) && Number.isInteger(v))
const scope = 'case-insensitive substring within names; directory hits cover descendants; bytes/objects only'

function complete(body: unknown, params: URLSearchParams): boolean {
  if (!record(body) || body.exact !== true || body.incremental !== false || body.levels !== 1 || body.path !== '' ||
      typeof body.target !== 'string' || typeof body.pattern !== 'string' || body.scope !== scope || !Array.isArray(body.buckets) || body.buckets.length < 1 || body.buckets.length > 6) return false
  const from = params.get('from')
  let next = 1
  const paths = new Set<string>()
  for (const row of body.buckets) {
    if (!record(row) || row.pre !== next || !Number.isSafeInteger(row.post) || (row.post as number) < next ||
        typeof row.path !== 'string' || !row.path || row.path.includes('/') || paths.has(row.path)) return false
    paths.add(row.path)
    next = (row.post as number) + 1
    if (from === null ? !weights(row) : !weights(row.before) || !weights(row.after) || !signedWeights(row.delta)) return false
  }
  if (from === null) return body.schema === 'hot-l1-batch-catalog-v1' && body.date === params.get('date') && weights(body.root)
  return body.schema === 'hot-l1-batch-catalog-diff-v1' && record(body.before) && record(body.after) &&
    body.before.date === from && body.after.date === params.get('date') && weights(body.before.root) && weights(body.after.root) && signedWeights(body.delta)
}

const unavailable = (retry = '1'): Response => json({ error: 'Hot L1 catalog is unavailable. Retry shortly; no scan fallback.' }, 503,
  { ...privateHeaders, 'retry-after': retry })

/** Operator-supplied VM/tunnel endpoint, not a Worker/service binding. */
export async function askHotL1(env: HotL1Env, params: URLSearchParams): Promise<Response> {
  if (!env.QUERY_BOX_HOT_L1_URL || !env.QUERY_BOX_TOKEN) return unavailable()
  let endpoint: URL
  try {
    endpoint = new URL(env.QUERY_BOX_HOT_L1_URL)
    if (!['http:', 'https:'].includes(endpoint.protocol) || endpoint.username || endpoint.password || endpoint.search || endpoint.hash) return unavailable()
    endpoint.pathname = endpoint.pathname.replace(/\/+$/, '') + '/api/hot-l1'
    endpoint.search = params.toString()
  } catch {
    return unavailable()
  }
  const controller = new AbortController()
  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined
  let timer: ReturnType<typeof setTimeout> | undefined
  const deadline = new Promise<never>((_, reject) => {
    timer = setTimeout(() => { controller.abort(); reject(new Error('deadline')); }, HOT_TIMEOUT_MS)
  })
  const work = async (): Promise<Response> => {
    const response = await fetch(endpoint.toString(), {
      headers: { authorization: `Bearer ${env.QUERY_BOX_TOKEN}`, accept: 'application/json' },
      signal: controller.signal, redirect: 'manual',
    })
    if (response.status !== 200) {
      const retry = response.headers.get('retry-after') ?? ''
      await response.body?.cancel()
      if (response.status === 400 || response.status === 404) return json({ error: 'Name or scan is not registered in the root-only hot catalog.' }, 400, privateHeaders)
      return unavailable(/^\d{1,3}$/.test(retry) && Number(retry) > 0 ? String(Math.min(Number(retry), 60)) : '1')
    }
    if (!response.body || Number(response.headers.get('content-length')) > HOT_MAX_BYTES) {
      await response.body?.cancel()
      return unavailable()
    }
    reader = response.body.getReader()
    const chunks: Uint8Array[] = []
    let size = 0
    try {
      for (;;) {
        const { value, done } = await reader.read()
        if (done) break
        size += value.byteLength
        if (size > HOT_MAX_BYTES) { await reader.cancel(); return unavailable() }
        chunks.push(value)
      }
      const bytes = new Uint8Array(size)
      let offset = 0
      for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength }
      const text = new TextDecoder('utf-8', { fatal: true, ignoreBOM: false }).decode(bytes)
      const body: unknown = JSON.parse(text)
      if (!complete(body, params)) return unavailable()
      // Preserve original JSON integers; do not round-trip fleet byte totals through JS numbers.
      return new Response(bytes, { headers: { 'content-type': 'application/json; charset=utf-8', ...privateHeaders, 'x-query-engine': 'hot-l1-catalog' } })
    } finally {
      reader.releaseLock()
      reader = undefined
    }
  }
  try {
    return await Promise.race([work(), deadline])
  } catch {
    return unavailable()
  } finally {
    if (timer !== undefined) clearTimeout(timer)
    controller.abort()
    if (reader) {
      // An aborted/failed network stream may reject cancellation; this cleanup
      // never performs follow-up work and must not extend the response deadline.
      void reader.cancel().catch(() => {})
    }
  }
}

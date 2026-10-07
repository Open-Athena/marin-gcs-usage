import { json } from './auth.js'
import { HotQueryError, hotParams, privateHeaders, type HotL1Env } from './hotL1.js'
import { datedNameRequest, nameRequest, parseName, parseNameRegistry } from '../../src/nameModel.js'

export type NameSummaryEnv = HotL1Env & { QUERY_BOX_NAME_SUMMARY?: string; QUERY_BOX_DATED_NAMES?: string }
export const NAME_MAX_BYTES = 64 << 10
export const NAME_TIMEOUT_MS = 8000
export function nameSummaryParams(url: URL, dated = false): URLSearchParams {
  const params = hotParams(url)
  try { if (dated) datedNameRequest(params); else nameRequest(params) } catch (error) { throw new HotQueryError((error as Error).message) }
  return params
}
export function nameRegistryParams(url: URL): void {
  if (url.search) throw new HotQueryError('The name-summary scan registry accepts no query parameters.')
}
const unavailable = (retry = '1'): Response => json({ error: 'Name summary is unavailable, busy or exceeded its work budget. This is not a zero-match result. Try again.' }, 503, { ...privateHeaders, 'retry-after': retry })
export async function askNameSummary(env: NameSummaryEnv, params: URLSearchParams): Promise<Response> {
  return askNameBackend(env, params)
}
export async function askNameSummaryRegistry(env: NameSummaryEnv): Promise<Response> {
  return askNameBackend(env)
}
async function askNameBackend(env: NameSummaryEnv, params?: URLSearchParams): Promise<Response> {
  if (!env.QUERY_BOX_HOT_L1_URL || !env.QUERY_BOX_TOKEN) return unavailable()
  let endpoint: URL
  try {
    endpoint = new URL(env.QUERY_BOX_HOT_L1_URL)
    if (!['http:', 'https:'].includes(endpoint.protocol) || endpoint.username || endpoint.password || endpoint.search || endpoint.hash) return unavailable()
    endpoint.pathname = endpoint.pathname.replace(/\/+$/, '') + (params ? '/api/name-summary' : '/api/name-summary-registry')
    endpoint.search = params?.toString() ?? ''
  } catch { return unavailable() }
  const controller = new AbortController()
  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined, timer: ReturnType<typeof setTimeout> | undefined
  const deadline = new Promise<never>((_, reject) => { timer = setTimeout(() => { controller.abort(); reject(new Error('deadline')) }, NAME_TIMEOUT_MS) })
  const work = async (): Promise<Response> => {
    const response = await fetch(endpoint.toString(), { headers: { authorization: `Bearer ${env.QUERY_BOX_TOKEN}`, accept: 'application/json' }, signal: controller.signal, redirect: 'manual' })
    if (response.status !== 200) {
      const retry = response.headers.get('retry-after') ?? ''
      await response.body?.cancel()
      if (response.status === 400 && params) return json({ error: env.QUERY_BOX_DATED_NAMES === '1'
        ? 'This literal or scan is unavailable in the current preview. New scans currently have precomputed answers only. This is not a zero-match result.'
        : 'Invalid name-summary request. Check the literal and frozen scan dates; this is not a zero-match result.' }, 400, privateHeaders)
      return unavailable(/^\d{1,3}$/.test(retry) && Number(retry) > 0 ? String(Math.min(Number(retry), 60)) : '1')
    }
    if (!response.body || Number(response.headers.get('content-length')) > NAME_MAX_BYTES) { await response.body?.cancel(); return unavailable() }
    reader = response.body.getReader()
    try {
      const chunks: Uint8Array[] = []
      let size = 0
      for (;;) {
        const { value, done } = await reader.read()
        if (done) break
        size += value.byteLength
        if (size > NAME_MAX_BYTES) { await reader.cancel(); return unavailable() }
        chunks.push(value)
      }
      const bytes = new Uint8Array(size)
      let offset = 0
      for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength }
      const body: unknown = JSON.parse(new TextDecoder('utf-8', { fatal: true, ignoreBOM: false }).decode(bytes))
      if (params) {
        if (env.QUERY_BOX_DATED_NAMES !== '1' && typeof body === 'object' && body !== null && 'schema' in body &&
          (body.schema === 'dated-name-summary-v1' || body.schema === 'dated-name-summary-diff-v1')) return unavailable()
        parseName(body, env.QUERY_BOX_DATED_NAMES === '1' ? datedNameRequest(params) : nameRequest(params))
      }
      else {
        const registry = parseNameRegistry(body)
        if (registry.dated && env.QUERY_BOX_DATED_NAMES !== '1') return unavailable()
      }
      return new Response(bytes, { headers: { 'content-type': 'application/json; charset=utf-8', ...privateHeaders, 'x-query-engine': params ? 'name-summary' : 'name-summary-registry' } })
    } finally { reader.releaseLock(); reader = undefined }
  }
  try { return await Promise.race([work(), deadline]) } catch { return unavailable() } finally {
    if (timer !== undefined) clearTimeout(timer)
    controller.abort()
    // Abort cleanup cannot extend the response deadline if a network stream stalls.
    if (reader) void reader.cancel().catch(() => {})
  }
}

import { json } from './auth.js'
import { HotQueryError, hotParams, privateHeaders, type HotL1Env } from './hotL1.js'
import { hotDrillRequest, parseHotDrill } from '../../src/hotDrillModel.js'

export type HotL2Env = HotL1Env & { QUERY_BOX_HOT_L2?: string }
export const HOT_L2_MAX_BYTES = 256 << 10
export const HOT_L2_TIMEOUT_MS = 5000
export function hotL2Params(url: URL): URLSearchParams {
  const parts = url.search.slice(1).split('&'), root = new URL(url), paths: string[] = [], retained: string[] = []
  if (!url.search || parts.length > 4) throw new HotQueryError('Use date, name, path and optional from only.')
  for (const part of parts) {
    const i = part.indexOf('=')
    if (i < 0) throw new HotQueryError('Invalid query parameters.')
    try {
      if (decodeURIComponent(part.slice(0, i).replace(/\+/g, ' ')) === 'path') paths.push(decodeURIComponent(part.slice(i + 1).replace(/\+/g, ' ')))
      else retained.push(part)
    } catch { throw new HotQueryError('Invalid query parameters.') }
  }
  if (paths.length !== 1 || !paths[0] || /[\/\0]/.test(paths[0]) || [...paths[0]].length > 512) throw new HotQueryError('Choose one bucket; deeper paths are not available.')
  root.search = retained.join('&')
  let params: URLSearchParams
  try { params = hotParams(root) } catch (error) {
    if (error instanceof HotQueryError && error.message === 'Use date, name and optional from only.') throw new HotQueryError('Use date, name, path and optional from only.')
    throw error
  }
  params.set('path', paths[0])
  return params
}
const unavailable = (retry = '1'): Response => json({ error: 'Hot L2 catalog is unavailable. Retry shortly; no scan fallback.' }, 503, { ...privateHeaders, 'retry-after': retry })
export async function askHotL2(env: HotL2Env, params: URLSearchParams): Promise<Response> {
  if (!env.QUERY_BOX_HOT_L1_URL || !env.QUERY_BOX_TOKEN) return unavailable()
  let endpoint: URL
  try {
    endpoint = new URL(env.QUERY_BOX_HOT_L1_URL)
    if (!['http:', 'https:'].includes(endpoint.protocol) || endpoint.username || endpoint.password || endpoint.search || endpoint.hash) return unavailable()
    endpoint.pathname = endpoint.pathname.replace(/\/+$/, '') + '/api/hot-l2'
    endpoint.search = params.toString()
  } catch { return unavailable() }
  const controller = new AbortController()
  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined, timer: ReturnType<typeof setTimeout> | undefined
  const deadline = new Promise<never>((_, reject) => { timer = setTimeout(() => { controller.abort(); reject(new Error('deadline')) }, HOT_L2_TIMEOUT_MS) })
  const work = async (): Promise<Response> => {
    const response = await fetch(endpoint.toString(), { headers: { authorization: `Bearer ${env.QUERY_BOX_TOKEN}`, accept: 'application/json' }, signal: controller.signal, redirect: 'manual' })
    if (response.status !== 200) {
      const retry = response.headers.get('retry-after') ?? ''
      await response.body?.cancel()
      if ([400, 404].includes(response.status)) return json({ error: 'Bucket detail is not registered for this name or scan.' }, 400, privateHeaders)
      return unavailable(/^\d{1,3}$/.test(retry) && Number(retry) > 0 ? String(Math.min(Number(retry), 60)) : '1')
    }
    if (!response.body || Number(response.headers.get('content-length')) > HOT_L2_MAX_BYTES) { await response.body?.cancel(); return unavailable() }
    reader = response.body.getReader()
    try {
      const chunks: Uint8Array[] = []
      let size = 0
      for (;;) {
        const { value, done } = await reader.read()
        if (done) break
        size += value.byteLength
        if (size > HOT_L2_MAX_BYTES) { await reader.cancel(); return unavailable() }
        chunks.push(value)
      }
      const bytes = new Uint8Array(size)
      let offset = 0
      for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength }
      parseHotDrill(JSON.parse(new TextDecoder('utf-8', { fatal: true, ignoreBOM: false }).decode(bytes)), hotDrillRequest(params))
      return new Response(bytes, { headers: { 'content-type': 'application/json; charset=utf-8', ...privateHeaders, 'x-query-engine': 'hot-l2-pair-catalog' } })
    } finally { reader.releaseLock(); reader = undefined }
  }
  try { return await Promise.race([work(), deadline]) } catch { return unavailable() } finally {
    if (timer !== undefined) clearTimeout(timer)
    controller.abort()
    // Cleanup cannot extend the deadline if an aborted network stream rejects or stalls.
    if (reader) void reader.cancel().catch(() => {})
  }
}

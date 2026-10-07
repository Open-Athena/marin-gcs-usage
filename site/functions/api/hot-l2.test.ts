import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { requireViewer, type Identity } from '../_lib/auth.js'
import { HOT_L2_MAX_BYTES, HOT_L2_TIMEOUT_MS, type HotL2Env } from '../_lib/hotL2.js'
import { onRequest } from './hot-l2.js'

vi.mock('../_lib/auth.js', async importOriginal => ({ ...await importOriginal<typeof import('../_lib/auth.js')>(), requireViewer: vi.fn() }))
const viewer: Identity = { email: null, name: null, scopes: ['gcs'], admin: false, via: 'session', subject: null }
const env: HotL2Env = { BASE_SCOPE: 'gcs', QUERY_BOX_HOT_L2: '1', QUERY_BOX_HOT_L1_URL: 'https://catalog.example', QUERY_BOX_TOKEN: 'fake-secret' }
const query = 'date=2026-10-05&name=.JSON&path=bucket-a'
const body = {
  schema: 'hot-l2-pair-catalog-v1', target: 'fixture', date: '2026-10-05', pattern: '.json', path: 'bucket-a',
  exact: true, incremental: false, levels: 2, scope: 'case-insensitive substring within names; directory hits cover descendants; bytes/objects only',
  source: 'registered accepted paired sparse artifact', geometry: { pre: 1, post: 10 },
  cutoff: { scope: 'fixed per query and bucket root across all depth-2 children; not arbitrary-prefix refinement', dates: ['2026-10-04', '2026-10-05'], budget: 64, threshold_bytes: '5' },
  validation: { artifact_sha256: 'a'.repeat(64), artifact_bytes: 123, prefix_proofs_checked: true, full_catalog_source_oracle: false,
    covered_control: { pattern: 'm', validation: 'complete ordinary recursive bucket/frame rollups on both dates', frames_checked: 3, heavy_cells_checked: 2, buckets_checked: 6 },
    query_oracle: { predicate_id: 1, pattern: '.json', checked: false, reason: 'no emitted cell within interval cap' } },
  capabilities: { bucket_roots_only: true, child_drill: false, filters: false }, root: { b: '8', o: '5' },
  children: [{ path: 'bucket-a/folder', name: 'folder', pre: 2, post: 4, b: '6', o: '2', drill: false }], other: { b: '2', o: '3', drill: false },
}
const unavailable = 'Hot L2 catalog is unavailable. Retry shortly; no scan fallback.'
let fetcher: ReturnType<typeof vi.fn<typeof fetch>>
const request = (search = query, vars: HotL2Env = env, method = 'GET') => onRequest({ request: new Request(`https://dev.example/api/hot-l2?${search}`, { method }), env: vars })
async function refused(response: Response, status: number, error: string) {
  expect(response.status).toBe(status)
  expect(await response.json()).toEqual({ error })
  expect(response.headers.get('cache-control')).toBe('private, no-store')
}
beforeEach(() => { vi.mocked(requireViewer).mockReset().mockResolvedValue(viewer); fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify(body))); vi.stubGlobal('fetch', fetcher) })
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })
describe('private opt-in bucket-only proxy', () => {
  it('authenticates first and marks refusals private', async () => {
    vi.mocked(requireViewer).mockResolvedValue(new Response('{"error":"Unauthorized"}', { status: 401 }))
    await refused(await request('name=%FF', {}), 401, 'Unauthorized')
    expect(fetcher.mock.calls).toEqual([])
    expect(vi.mocked(requireViewer).mock.calls).toEqual([[{ request: expect.any(Request), env: { PUBLIC_READ: undefined } }]])
  })
  it.each([undefined, '0', 'true'])('requires flag exactly 1 (%s)', flag => {
    return request(query, { ...env, QUERY_BOX_HOT_L2: flag }).then(async response => { await refused(response, 404, 'Hot L2 is not enabled.'); expect(fetcher.mock.calls).toEqual([]) })
  })
  it.each([{ PUBLIC_READ: '1' }, { STORE_KEY: '' }, { STORE_SCOPE: 'other' }])('rejects public/overlay %j', async override => {
    await refused(await request(query, { ...env, ...override }), override.PUBLIC_READ ? 403 : 409,
      override.PUBLIC_READ ? 'Hot L2 requires a private viewer-protected deployment.' : 'Hot L2 serves the primary frozen index only.')
    expect(fetcher.mock.calls).toEqual([])
  })
  it('rejects public identity and non-GET', async () => {
    vi.mocked(requireViewer).mockResolvedValue({ ...viewer, via: 'public' })
    await refused(await request(), 403, 'Hot L2 requires a private viewer-protected deployment.')
    vi.mocked(requireViewer).mockResolvedValue(viewer)
    const response = await request(query, env, 'POST')
    await refused(response, 405, 'Only GET is supported.')
    expect(response.headers.get('allow')).toBe('GET'); expect(fetcher.mock.calls).toEqual([])
  })
  it.each([
    ['date=2026-10-05&name=x', 'Choose one bucket; deeper paths are not available.'],
    ['date=2026-10-05&name=x&path=a/b', 'Choose one bucket; deeper paths are not available.'],
    ['date=2026-10-05&name=x&path=a&%70ath=b', 'Choose one bucket; deeper paths are not available.'],
    ['date=2026-10-05&name=x&path=%FF', 'Invalid query parameters.'],
    ['date=2026-10-05&name=x&path=a&owner=y', 'Use date, name, path and optional from only.'],
    ['date=2026-10-05&name=x&path=a&from=2026-10-05', 'from must be a valid earlier scan date.'],
  ])('refuses malformed %s before IO', async (search, error) => {
    await refused(await request(search), 400, error); expect(fetcher.mock.calls).toEqual([])
  })
  it('preserves accepted raw bytes and strips upstream cache/cookie headers', async () => {
    const raw = JSON.stringify(body) + '\n'; fetcher.mockResolvedValue(new Response(raw, { headers: { 'set-cookie': 'private', 'cache-control': 'public' } }))
    const response = await request()
    expect(response.status).toBe(200); expect(await response.text()).toBe(raw)
    expect([...response.headers]).toEqual([['cache-control', 'private, no-store'], ['content-type', 'application/json; charset=utf-8'], ['x-query-engine', 'hot-l2-pair-catalog']])
    expect(fetcher.mock.calls).toEqual([[`https://catalog.example/api/hot-l2?${query}`, { headers: { authorization: 'Bearer fake-secret', accept: 'application/json' }, signal: expect.any(AbortSignal), redirect: 'manual' }]])
  })
  it('accepts the complete comparison contract with canonical negative deltas', async () => {
    const pair = (b: string, o: string, after: { b: string; o: string }) => ({ before: { b, o }, after: { b: after.b, o: after.o }, delta: { b: String(BigInt(after.b) - BigInt(b)), o: String(BigInt(after.o) - BigInt(o)) } })
    const diff = { ...body, schema: 'hot-l2-pair-catalog-diff-v1', dates: ['2026-10-04', '2026-10-05'],
      root: pair('14', '7', body.root), children: [{ ...body.children[0], ...pair('12', '2', body.children[0]) }],
      other: { drill: false, ...pair('2', '5', body.other) } }
    const raw = JSON.stringify(diff) + '\n'; fetcher.mockResolvedValue(new Response(raw))
    const response = await request(query + '&from=2026-10-04')
    expect(response.status).toBe(200); expect(await response.text()).toBe(raw)
    expect(fetcher.mock.calls[0][0]).toBe('https://catalog.example/api/hot-l2?date=2026-10-05&name=.JSON&from=2026-10-04&path=bucket-a')
  })
})
describe('bounded exact response acceptance', () => {
  it.each([{ QUERY_BOX_HOT_L1_URL: undefined }, { QUERY_BOX_TOKEN: undefined }, { QUERY_BOX_HOT_L1_URL: 'https://user:password@catalog.example' },
    { QUERY_BOX_HOT_L1_URL: 'https://catalog.example?leaked=secret' }, { QUERY_BOX_HOT_L1_URL: 'ftp://catalog.example' }])('refuses invalid operator config %j', async override => {
    await refused(await request(query, { ...env, ...override }), 503, unavailable); expect(fetcher.mock.calls).toEqual([])
  })
  it('sanitizes unreachable backend exceptions without retry/fallback', async () => {
    fetcher.mockRejectedValue(new Error('private-secret host'))
    await refused(await request(), 503, unavailable); expect(fetcher.mock.calls.length).toBe(1)
  })
  it('sanitizes overloaded backend and clamps retry-after', async () => {
    fetcher.mockResolvedValue(new Response('private data', { status: 503, headers: { 'retry-after': '999' } }))
    const response = await request(); await refused(response, 503, unavailable); expect(response.headers.get('retry-after')).toBe('60')
  })
  it.each([400, 404])('does not reinterpret unavailable %s as zero', async status => {
    fetcher.mockResolvedValue(new Response('secret', { status })); await refused(await request(), 400, 'Bucket detail is not registered for this name or scan.'); expect(fetcher.mock.calls.length).toBe(1)
  })
  it('refuses redirects without forwarding the token', async () => {
    const cancel = vi.fn(); fetcher.mockResolvedValue(new Response(new ReadableStream({ cancel }), { status: 302, headers: { location: 'https://untrusted.example' } }))
    await refused(await request(), 503, unavailable); expect(fetcher.mock.calls.length).toBe(1); expect(cancel.mock.calls).toEqual([[undefined]])
    expect(fetcher.mock.calls[0][1]?.redirect).toBe('manual')
  })
  it.each([true, false])('cancels oversized declared/streamed body %s', async declared => {
    const cancel = vi.fn(); fetcher.mockResolvedValue(new Response(new ReadableStream({ start(c) { c.enqueue(new Uint8Array(HOT_L2_MAX_BYTES + 1)) }, cancel }), { headers: declared ? { 'content-length': String(HOT_L2_MAX_BYTES + 1) } : {} }))
    await refused(await request(), 503, unavailable); expect(cancel.mock.calls).toEqual([[undefined]])
  })
  it('accepts exactly 256 KiB with exact canonical string totals', async () => {
    const raw = JSON.stringify(body).padEnd(HOT_L2_MAX_BYTES, ' '); fetcher.mockResolvedValue(new Response(raw))
    const response = await request(); expect(response.status).toBe(200); expect(await response.text()).toBe(raw)
  })
  it.each(['invalid utf8', 'invalid JSON', 'unsafe integer', 'wrong partition'])('rejects %s safely', async issue => {
    const v = structuredClone(body)
    if (issue === 'unsafe integer') v.root.b = '9007199254740992'
    if (issue === 'wrong partition') v.other.o = '4'
    fetcher.mockResolvedValue(new Response(issue === 'invalid utf8' ? new Uint8Array([255]) : issue === 'invalid JSON' ? '{' : JSON.stringify(v)))
    await refused(await request(), 503, unavailable); expect(fetcher.mock.calls.length).toBe(1)
  })
  it.each(['fetch', 'body'])('bounds whole %s phase even if abort is ignored', async phase => {
    vi.useFakeTimers(); const cancel = vi.fn()
    if (phase === 'fetch') fetcher.mockImplementation(() => new Promise(() => {}))
    else fetcher.mockResolvedValue(new Response(new ReadableStream({ cancel })))
    const pending = request(); await vi.advanceTimersByTimeAsync(HOT_L2_TIMEOUT_MS)
    await refused(await pending, 503, unavailable)
    expect(fetcher.mock.calls.length).toBe(1); expect(fetcher.mock.calls[0][1]?.signal?.aborted).toBe(true)
    expect(cancel.mock.calls).toEqual(phase === 'body' ? [[undefined]] : [])
  })
})

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { requireViewer, type Identity } from '../_lib/auth.js'
import { HOT_MAX_BYTES, HOT_TIMEOUT_MS, type HotL1Env } from '../_lib/hotL1.js'
import { onRequest } from './hot-l1.js'

vi.mock('../_lib/auth.js', async importOriginal => ({
  ...await importOriginal<typeof import('../_lib/auth.js')>(), requireViewer: vi.fn(),
}))
const viewer: Identity = { email: null, name: null, scopes: ['gcs'], admin: false, via: 'session', subject: null }
const env: HotL1Env = { BASE_SCOPE: 'gcs', QUERY_BOX_HOT_L1: '1', QUERY_BOX_HOT_L1_URL: 'https://catalog.example', QUERY_BOX_TOKEN: 'fake-backend-secret' }
const scope = 'case-insensitive substring within names; directory hits cover descendants; bytes/objects only'
const body = {
  schema: 'hot-l1-batch-catalog-v1', target: 'frozen-fixture', pattern: '.json', path: '',
  date: '2026-10-05', exact: true, incremental: false, levels: 1, scope,
  root: { b: 8, o: 5 }, buckets: [
    { pre: 1, post: 3, path: 'a', b: 8, o: 3 },
    { pre: 4, post: 7, path: 'b', b: 0, o: 2 },
  ],
}
const query = 'date=2026-10-05&name=.json'
const unavailable = { error: 'Hot L1 catalog is unavailable. Retry shortly; no scan fallback.' }
let fetcher: ReturnType<typeof vi.fn<typeof fetch>>
const request = (search = query, vars: HotL1Env = env, method = 'GET') => onRequest({
  request: new Request(`https://dev.gcs.oa.dev/api/hot-l1?${search}`, { method }), env: vars,
})
async function refused(response: Response, status: number, error: string) {
  expect(response.status).toBe(status)
  expect(await response.json()).toEqual({ error })
  expect(response.headers.get('cache-control')).toBe('private, no-store')
}
beforeEach(() => {
  vi.mocked(requireViewer).mockReset().mockResolvedValue(viewer)
  fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify(body)))
  vi.stubGlobal('fetch', fetcher)
})
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

describe('private opt-in root-only hot catalog route', () => {
  it('authenticates before feature flags, malformed params or backend access', async () => {
    vi.mocked(requireViewer).mockResolvedValue(new Response('{"error":"Unauthorized"}\n', { status: 401 }))
    await refused(await request('name=%FF', {}), 401, 'Unauthorized')
    expect(fetcher.mock.calls).toEqual([])
    expect(vi.mocked(requireViewer).mock.calls).toEqual([{ request: expect.any(Request), env: { PUBLIC_READ: undefined } }].map(ctx => [ctx]))
  })
  it.each([undefined, '0', 'true'])('is disabled unless exactly 1 (%s)', async flag => {
    await refused(await request(query, { ...env, QUERY_BOX_HOT_L1: flag }), 404, 'Hot L1 is not enabled.')
    expect(fetcher.mock.calls).toEqual([])
  })
  it('suppresses PUBLIC_READ at the auth gate and refuses signed-in public deployments', async () => {
    await refused(await request(query, { ...env, PUBLIC_READ: '1' }), 403, 'Hot L1 requires a private viewer-protected deployment.')
    expect(vi.mocked(requireViewer).mock.calls).toEqual([[{ request: expect.any(Request), env: { ...env, PUBLIC_READ: undefined } }]])
    expect(fetcher.mock.calls).toEqual([])
  })
  it('does not admit a public identity', async () => {
    vi.mocked(requireViewer).mockResolvedValue({ ...viewer, via: 'public' })
    await refused(await request(), 403, 'Hot L1 requires a private viewer-protected deployment.')
    expect(fetcher.mock.calls).toEqual([])
  })
  it('real auth cannot inherit anonymous PUBLIC_READ on a private preview URL', async () => {
    const actual = await vi.importActual<typeof import('../_lib/auth.js')>('../_lib/auth.js')
    vi.mocked(requireViewer).mockImplementation(actual.requireViewer)
    const response = await request(query, { ...env, PUBLIC_READ: '1' })
    expect(response.status).toBe(401)
    expect(fetcher.mock.calls).toEqual([])
    expect(response.headers.get('cache-control')).toBe('private, no-store')
  })
  it.each([{ STORE_KEY: 'secondary' }, { STORE_KEY: '' }, { STORE_SCOPE: 'secondary' }])('refuses store overlays %j', async overlay => {
    await refused(await request(query, { ...env, ...overlay }), 409, 'Hot L1 serves the primary frozen index only.')
    expect(fetcher.mock.calls).toEqual([])
  })
  it('only serves GET', async () => {
    const response = await request(query, env, 'POST')
    await refused(response, 405, 'Only GET is supported.')
    expect(response.headers.get('allow')).toBe('GET')
    expect(fetcher.mock.calls).toEqual([])
  })
  it.each([
    ['date=2026-10-05&name=.json&path=', 'Use date, name and optional from only.'],
    ['date=2026-10-05&name=.json&store=gcs', 'Use date, name and optional from only.'],
    ['date=2026-10-05&name=.json&lens=age', 'Use date, name and optional from only.'],
    ['date=2026-10-05&name=x&%6eame=y', 'Duplicate query parameter.'],
    ['date=2026-10-05&date=2026-10-04&name=x', 'Duplicate query parameter.'],
    ['date=2026-10-05&name=%FF', 'Invalid query parameters.'],
    ['date=2026-10-05&name=%XY', 'Invalid query parameters.'],
    ['date=2026-10-05&name=%ED%A0%80', 'Invalid query parameters.'],
    ['date=2026-10-05&name', 'Invalid query parameters.'],
    ['date=2026-02-30&name=x', 'A valid scan date is required.'],
    ['name=x', 'A valid scan date is required.'],
    ['date=2026-10-05&name=', 'Use one nonempty NUL/slash-free literal of at most 512 characters.'],
    ['date=2026-10-05&name=a%2Fb', 'Use one nonempty NUL/slash-free literal of at most 512 characters.'],
    ['date=2026-10-05&name=%00', 'Use one nonempty NUL/slash-free literal of at most 512 characters.'],
    [`date=2026-10-05&name=${'x'.repeat(513)}`, 'Use one nonempty NUL/slash-free literal of at most 512 characters.'],
    ['date=2026-10-05&name=x&from=', 'from must be a valid earlier scan date.'],
    ['date=2026-10-05&name=x&from=2026-10-05', 'from must be a valid earlier scan date.'],
    ['date=2026-10-05&name=x&from=2026-10-06', 'from must be a valid earlier scan date.'],
  ])('strictly rejects %s before fetching', async (search, error) => {
    await refused(await request(search), 400, error)
    expect(fetcher.mock.calls).toEqual([])
  })
  it('forwards literal Unicode and punctuation without interpreting search syntax or client tokens', async () => {
    const name = 'Å😀\n%+_\\'
    const params = new URLSearchParams({ date: '2026-10-05', name })
    const raw = JSON.stringify({ ...body, pattern: name.toLowerCase() }) + '\n'
    fetcher.mockResolvedValue(new Response(raw, { headers: { 'cache-control': 'public', 'set-cookie': 'private-backend-cookie' } }))
    const response = await request(params.toString())
    expect(response.status).toBe(200)
    expect(await response.text()).toBe(raw)
    expect([...response.headers]).toEqual([
      ['cache-control', 'private, no-store'], ['content-type', 'application/json; charset=utf-8'], ['x-query-engine', 'hot-l1-catalog'],
    ])
    expect(fetcher.mock.calls).toEqual([[`https://catalog.example/api/hot-l1?${params}`, {
      headers: { authorization: 'Bearer fake-backend-secret', accept: 'application/json' }, signal: expect.any(AbortSignal), redirect: 'manual',
    }]])
  })
  it('forwards a complete two-date negative diff', async () => {
    const before = { ...body, date: '2026-10-04', root: { b: 14, o: 5 } }
    const diff = { ...body, schema: 'hot-l1-batch-catalog-diff-v1', before, after: body, delta: { b: -6, o: 0 }, buckets: [
      { pre: 1, post: 3, path: 'a', before: { b: 14, o: 3 }, after: { b: 8, o: 3 }, delta: { b: -6, o: 0 } },
      { pre: 4, post: 7, path: 'b', before: { b: 0, o: 2 }, after: { b: 0, o: 2 }, delta: { b: 0, o: 0 } },
    ] }
    fetcher.mockResolvedValue(new Response(JSON.stringify(diff)))
    const response = await request(query + '&from=2026-10-04')
    expect(response.status).toBe(200)
    expect(await response.json()).toEqual(diff)
    expect(fetcher.mock.calls[0][0]).toBe('https://catalog.example/api/hot-l1?date=2026-10-05&name=.json&from=2026-10-04')
  })
})

describe('bounded backend response and controlled failure', () => {
  it.each([
    { QUERY_BOX_HOT_L1_URL: undefined }, { QUERY_BOX_TOKEN: undefined },
    { QUERY_BOX_HOT_L1_URL: 'https://user:password@catalog.example' }, { QUERY_BOX_HOT_L1_URL: 'ftp://catalog.example' },
    { QUERY_BOX_HOT_L1_URL: 'https://catalog.example?leaked=secret' },
  ])('refuses missing/invalid operator configuration %j', async override => {
    await refused(await request(query, { ...env, ...override }), 503, unavailable.error)
    expect(fetcher.mock.calls).toEqual([])
  })
  it.each([400, 404])('sanitizes unregistered scan/name (%s)', async status => {
    fetcher.mockResolvedValue(new Response('secret/internal/artifact/path', { status }))
    await refused(await request(), 400, 'Name or scan is not registered in the root-only hot catalog.')
    expect(fetcher.mock.calls.length).toBe(1)
  })
  it.each([[503, '2', '2'], [503, '999', '60'], [503, '0', '1'], [503, 'Tue, 06 Oct 2026 00:00:00 GMT', '1'], [401, '', '1'], [501, '', '1'], [500, '', '1']])('sanitizes failure %s with safe retry %s', async (status, retry, expected) => {
    fetcher.mockResolvedValue(new Response('private stacktrace', { status: Number(status), headers: { 'retry-after': String(retry) } }))
    const response = await request()
    await refused(response, 503, unavailable.error)
    expect(response.headers.get('retry-after')).toBe(expected)
    expect(fetcher.mock.calls.length).toBe(1)
  })
  it('sanitizes unreachable backend errors with no retry/fallback fetch', async () => {
    fetcher.mockRejectedValue(new Error('fake-backend-secret private network address'))
    await refused(await request(), 503, unavailable.error)
    expect(fetcher.mock.calls.length).toBe(1)
  })
  it('uses workerd-compatible manual redirects and never fetches a redirect destination with the token', async () => {
    const cancel = vi.fn()
    const stream = new ReadableStream<Uint8Array>({ cancel })
    fetcher.mockResolvedValue(new Response(stream, { status: 302, headers: { location: 'https://untrusted.example/steal-token' } }))
    await refused(await request(), 503, unavailable.error)
    expect(fetcher.mock.calls).toEqual([['https://catalog.example/api/hot-l1?date=2026-10-05&name=.json', {
      headers: { authorization: 'Bearer fake-backend-secret', accept: 'application/json' }, signal: expect.any(AbortSignal), redirect: 'manual',
    }]])
    expect(cancel.mock.calls).toEqual([[undefined]])
  })
  it('accepts exactly 64 KiB and preserves original large integer JSON', async () => {
    const raw = JSON.stringify(body).replace('"b":8', '"b":9007199254740993').padEnd(HOT_MAX_BYTES, ' ')
    fetcher.mockResolvedValue(new Response(raw))
    const response = await request()
    expect(response.status).toBe(200)
    expect(await response.text()).toBe(raw)
  })
  it.each([true, false])('cancels an oversized body (declared length=%s)', async declared => {
    const cancel = vi.fn()
    const stream = new ReadableStream<Uint8Array>({ start(controller) { controller.enqueue(new Uint8Array(HOT_MAX_BYTES + 1)) }, cancel })
    fetcher.mockResolvedValue(new Response(stream, { headers: declared ? { 'content-length': String(HOT_MAX_BYTES + 1) } : {} }))
    await refused(await request(), 503, unavailable.error)
    expect(cancel.mock.calls).toEqual([[undefined]])
  })
  it.each([
    'not JSON', JSON.stringify({ ...body, exact: false }), JSON.stringify({ ...body, incremental: true }),
    JSON.stringify({ ...body, date: '2026-10-04' }), JSON.stringify({ ...body, scope: 'fullpath regex' }),
    JSON.stringify({ ...body, buckets: [] }), JSON.stringify({ ...body, buckets: [{ ...body.buckets[0], pre: 2 }] }),
    JSON.stringify({ ...body, buckets: [{ ...body.buckets[0], b: -1 }] }),
  ])('refuses malformed or incomplete results %s', async raw => {
    fetcher.mockResolvedValue(new Response(raw))
    await refused(await request(), 503, unavailable.error)
  })
  it('rejects invalid UTF-8 response bytes', async () => {
    fetcher.mockResolvedValue(new Response(new Uint8Array([255])))
    await refused(await request(), 503, unavailable.error)
  })
  it.each(['fetch', 'body'])('bounds the whole %s phase to five seconds even if a mock ignores abort', async phase => {
    vi.useFakeTimers()
    const cancel = vi.fn()
    if (phase === 'fetch') fetcher.mockImplementation(() => new Promise<Response>(() => {}))
    else fetcher.mockResolvedValue(new Response(new ReadableStream<Uint8Array>({ cancel })))
    const pending = request()
    await vi.advanceTimersByTimeAsync(HOT_TIMEOUT_MS - 1)
    let completed = false
    void pending.then(() => { completed = true })
    expect(completed).toBe(false)
    await vi.advanceTimersByTimeAsync(1)
    await refused(await pending, 503, unavailable.error)
    expect(fetcher.mock.calls.length).toBe(1)
    expect(fetcher.mock.calls[0][1]?.signal?.aborted).toBe(true)
    expect(cancel.mock.calls).toEqual(phase === 'body' ? [[undefined]] : [])
  })
})

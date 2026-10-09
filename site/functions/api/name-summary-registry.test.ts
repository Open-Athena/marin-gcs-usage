import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { requireViewer, type Identity } from '../_lib/auth.js'
import { NAME_MAX_BYTES, NAME_TIMEOUT_MS, type NameSummaryEnv } from '../_lib/nameSummary.js'
import { dailyNameFixture, datedNameRegistry, legacyNameRegistry, mixedDatedNameDiff } from '../../src/datedNameTestFixtures.js'
import { onRequest as summary } from './name-summary.js'
import { onRequest as registry } from './name-summary-registry.js'

vi.mock('../_lib/auth.js', async original => ({ ...await original<typeof import('../_lib/auth.js')>(), requireViewer: vi.fn() }))
const viewer: Identity = { email: null, name: null, scopes: ['gcs'], admin: false, via: 'session', subject: null }
const env: NameSummaryEnv = { BASE_SCOPE: 'gcs', QUERY_BOX_NAME_SUMMARY: '1', QUERY_BOX_DATED_NAMES: '1', QUERY_BOX_HOT_L1_URL: 'https://catalog.example', QUERY_BOX_TOKEN: 'fake-secret' }
const unavailable = 'Name summary is unavailable, busy or exceeded its work budget. This is not a zero-match result. Try again.'
let fetcher: ReturnType<typeof vi.fn<typeof fetch>>
const scans = (vars: NameSummaryEnv = env, search = '', method = 'GET') => registry({ request: new Request(`https://dev.example/api/name-summary-registry${search}`, { method }), env: vars })
const query = (search: string, vars: NameSummaryEnv = env) => summary({ request: new Request(`https://dev.example/api/name-summary?${search}`), env: vars })
async function refused(response: Response, status: number, error: string) {
  expect(response.status).toBe(status)
  expect(await response.json()).toEqual({ error })
  expect(response.headers.get('cache-control')).toBe('private, no-store')
}
beforeEach(() => { vi.mocked(requireViewer).mockReset().mockResolvedValue(viewer); fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify(datedNameRegistry()))); vi.stubGlobal('fetch', fetcher) })
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })
describe('private dated scan registry', () => {
  it('authenticates before flags, query validation and backend access', async () => {
    vi.mocked(requireViewer).mockResolvedValue(new Response('{"error":"Unauthorized"}', { status: 401 }))
    await refused(await scans({}, '?x=y'), 401, 'Unauthorized')
    expect(fetcher.mock.calls).toEqual([])
    expect(vi.mocked(requireViewer).mock.calls).toEqual([[{ request: expect.any(Request), env: { PUBLIC_READ: undefined } }]])
  })
  it.each([
    [{ QUERY_BOX_NAME_SUMMARY: '0' }, '', 'GET', 404, 'Name summaries are not enabled.'],
    [{ PUBLIC_READ: '1' }, '', 'GET', 403, 'Name summaries require a private viewer-protected deployment.'],
    [{ STORE_KEY: 'secondary' }, '', 'GET', 409, 'Name summaries serve the primary frozen index only.'],
    [{}, '?date=2026-10-06', 'GET', 400, 'The name-summary scan registry accepts no query parameters.'],
    [{}, '', 'POST', 405, 'Only GET is supported.'],
  ] as const)('rejects unsafe/unsupported registry request %j %s %s', async (override, search, method, status, error) => {
    await refused(await scans({ ...env, ...override }, search, method), status, error)
    expect(fetcher.mock.calls).toEqual([])
  })
  it('forwards complete validated metadata without upstream cookies or public caching', async () => {
    const raw = JSON.stringify(datedNameRegistry()) + '\n'
    fetcher.mockResolvedValue(new Response(raw, { headers: { 'set-cookie': 'upstream-cookie', 'cache-control': 'public' } }))
    const response = await scans()
    expect(response.status).toBe(200); expect(await response.text()).toBe(raw)
    expect([...response.headers]).toEqual([['cache-control', 'private, no-store'], ['content-type', 'application/json; charset=utf-8'], ['x-query-engine', 'name-summary-registry']])
    expect(fetcher.mock.calls).toEqual([['https://catalog.example/api/name-summary-registry', { headers: { authorization: 'Bearer fake-secret', accept: 'application/json' }, signal: expect.any(AbortSignal), redirect: 'manual' }]])
  })
  it('accepts old metadata but refuses dated metadata while its explicit flag is disabled', async () => {
    await refused(await scans({ ...env, QUERY_BOX_DATED_NAMES: '0' }), 503, unavailable)
    const raw = JSON.stringify(legacyNameRegistry())
    fetcher.mockResolvedValue(new Response(raw))
    const response = await scans({ ...env, QUERY_BOX_DATED_NAMES: '0' })
    expect(response.status).toBe(200); expect(await response.text()).toBe(raw)
  })
  it.each(['bad identity', 'duplicate buckets', 'unbounded dates', 'invalid utf8', 'JSON'])('refuses %s metadata', async issue => {
    const body = datedNameRegistry()
    const daily = body.dates[2]
    if (issue === 'bad identity' && 'source' in daily) daily.source = { ...daily.source, artifact_sha256: 'invalid' }
    if (issue === 'duplicate buckets') body.bucket_paths.push(body.bucket_paths[0])
    if (issue === 'unbounded dates') body.dates = Array.from({ length: 67 }, () => body.dates[2])
    fetcher.mockResolvedValue(new Response(issue === 'invalid utf8' ? new Uint8Array([255]) : issue === 'JSON' ? '{' : JSON.stringify(body)))
    await refused(await scans(), 503, unavailable)
  })
  it.each([400, 302, 401, 500])('sanitizes metadata backend status %s without fallback or redirect', async status => {
    fetcher.mockResolvedValue(new Response('private backend detail', { status, headers: { location: 'https://untrusted.example' } }))
    await refused(await scans(), 503, unavailable)
    expect(fetcher.mock.calls.length).toBe(1)
    expect(fetcher.mock.calls[0][1]?.redirect).toBe('manual')
  })
  it('enforces the same streamed byte limit for metadata', async () => {
    const cancel = vi.fn()
    fetcher.mockResolvedValue(new Response(new ReadableStream({ start(c) { c.enqueue(new Uint8Array(NAME_MAX_BYTES + 1)) }, cancel })))
    await refused(await scans(), 503, unavailable)
    expect(cancel.mock.calls).toEqual([[undefined]])
  })
  it('bounds stalled metadata to eight seconds', async () => {
    vi.useFakeTimers(); fetcher.mockImplementation(() => new Promise(() => {}))
    const pending = scans(); await vi.advanceTimersByTimeAsync(NAME_TIMEOUT_MS)
    await refused(await pending, 503, unavailable)
    expect(fetcher.mock.calls[0][1]?.signal?.aborted).toBe(true)
  })
})
describe('explicitly enabled dated whole responses', () => {
  it.each([false, true])('preserves complete single/mixed body %s and explicit baseline', async diff => {
    const raw = JSON.stringify(diff ? mixedDatedNameDiff() : dailyNameFixture()) + '\n'
    fetcher.mockResolvedValue(new Response(raw))
    const params = 'date=2026-10-06&name=DATAKIT' + (diff ? '&from=2026-10-05' : '')
    const response = await query(params)
    expect(response.status).toBe(200); expect(await response.text()).toBe(raw)
    expect(fetcher.mock.calls[0][0]).toBe(`https://catalog.example/api/name-summary?${params}`)
  })
  it('refuses new dates before IO when dated serving is disabled', async () => {
    await refused(await query('date=2026-10-06&name=datakit', { ...env, QUERY_BOX_DATED_NAMES: '0' }), 400, 'Only the frozen 2026-10-04 and 2026-10-05 scans are available.')
    expect(fetcher.mock.calls).toEqual([])
  })
  it('refuses dated schema at an old date when dated serving is disabled', async () => {
    fetcher.mockResolvedValue(new Response(JSON.stringify(dailyNameFixture('2026-10-05'))))
    await refused(await query('date=2026-10-05&name=datakit', { ...env, QUERY_BOX_DATED_NAMES: '0' }), 503, unavailable)
  })
  it('never turns an unavailable date/literal into zero or an old-date fallback', async () => {
    fetcher.mockResolvedValue(new Response('private unavailable detail', { status: 400 }))
    await refused(await query('date=2026-10-06&name=unregistered'), 400, 'This literal or scan is unavailable in the current preview. New scans currently have precomputed answers only. This is not a zero-match result.')
    expect(fetcher.mock.calls.length).toBe(1)
  })
})

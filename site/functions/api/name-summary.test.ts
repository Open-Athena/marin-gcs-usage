import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { requireViewer, type Identity } from '../_lib/auth.js'
import { NAME_MAX_BYTES, NAME_TIMEOUT_MS, type NameSummaryEnv } from '../_lib/nameSummary.js'
import { nameDiff, nameFixture } from '../../src/nameTestFixtures.js'
import { onRequest } from './name-summary.js'

vi.mock('../_lib/auth.js', async original => ({ ...await original<typeof import('../_lib/auth.js')>(), requireViewer: vi.fn() }))
const viewer: Identity = { email: null, name: null, scopes: ['gcs'], admin: false, via: 'session', subject: null }
const env: NameSummaryEnv = { BASE_SCOPE: 'gcs', QUERY_BOX_NAME_SUMMARY: '1', QUERY_BOX_HOT_L1_URL: 'https://catalog.example', QUERY_BOX_TOKEN: 'fake-secret' }
const query = 'date=2026-10-05&name=DATAKIT'
const unavailable = 'Name summary is unavailable, busy or exceeded its work budget. This is not a zero-match result. Try again.'
let fetcher: ReturnType<typeof vi.fn<typeof fetch>>
const request = (search = query, vars: NameSummaryEnv = env, method = 'GET') => onRequest({ request: new Request(`https://dev.example/api/name-summary?${search}`, { method }), env: vars })
async function refused(response: Response, status: number, error: string) {
  expect(response.status).toBe(status); expect(await response.json()).toEqual({ error }); expect(response.headers.get('cache-control')).toBe('private, no-store')
}
beforeEach(() => { vi.mocked(requireViewer).mockReset().mockResolvedValue(viewer); fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify(nameFixture()))); vi.stubGlobal('fetch', fetcher) })
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })
describe('private explicitly enabled summary route', () => {
  it('authenticates before feature flag, validation or backend access', async () => {
    vi.mocked(requireViewer).mockResolvedValue(new Response('{"error":"Unauthorized"}', { status: 401 }))
    await refused(await request('name=%FF', {}), 401, 'Unauthorized')
    expect(fetcher.mock.calls).toEqual([])
    expect(vi.mocked(requireViewer).mock.calls).toEqual([[{ request: expect.any(Request), env: { PUBLIC_READ: undefined } }]])
  })
  it.each([undefined, '0', 'true'])('refuses flag %s', async flag => {
    await refused(await request(query, { ...env, QUERY_BOX_NAME_SUMMARY: flag }), 404, 'Name summaries are not enabled.'); expect(fetcher.mock.calls).toEqual([])
  })
  it.each([{ PUBLIC_READ: '1' }, { STORE_KEY: '' }, { STORE_SCOPE: 'secondary' }])('refuses public/secondary deployment %j', async override => {
    await refused(await request(query, { ...env, ...override }), override.PUBLIC_READ ? 403 : 409,
      override.PUBLIC_READ ? 'Name summaries require a private viewer-protected deployment.' : 'Name summaries serve the primary frozen index only.')
    expect(fetcher.mock.calls).toEqual([])
  })
  it('refuses anonymous identity and non-GET', async () => {
    vi.mocked(requireViewer).mockResolvedValue({ ...viewer, via: 'public' })
    await refused(await request(), 403, 'Name summaries require a private viewer-protected deployment.')
    vi.mocked(requireViewer).mockResolvedValue(viewer)
    const response = await request(query, env, 'POST'); await refused(response, 405, 'Only GET is supported.')
    expect(response.headers.get('allow')).toBe('GET'); expect(fetcher.mock.calls).toEqual([])
  })
  it('real auth cannot inherit the anonymous PUBLIC_READ bypass', async () => {
    const actual = await vi.importActual<typeof import('../_lib/auth.js')>('../_lib/auth.js'); vi.mocked(requireViewer).mockImplementation(actual.requireViewer)
    const response = await request(query, { ...env, PUBLIC_READ: '1' })
    expect(response.status).toBe(401); expect(response.headers.get('cache-control')).toBe('private, no-store'); expect(fetcher.mock.calls).toEqual([])
  })
  it.each([
    ['date=2026-10-05&name=x&path=', 'Use date, name and optional from only.'],
    ['date=2026-10-05&name=x&owner=y', 'Use date, name and optional from only.'],
    ['date=2026-10-05&name=x&%6eame=y', 'Duplicate query parameter.'],
    ['date=2026-10-05&name=%FF', 'Invalid query parameters.'],
    ['date=2026-02-30&name=x', 'A valid scan date is required.'],
    ['date=2026-10-06&name=x', 'Only the frozen 2026-10-04 and 2026-10-05 scans are available.'],
    ['date=2026-10-05&name=a/b', 'Use one nonempty NUL/slash-free literal of at most 512 characters.'],
    ['date=2026-10-05&name=%00', 'Use one nonempty NUL/slash-free literal of at most 512 characters.'],
    ['date=2026-10-05&name=x&from=2026-10-05', 'from must be a valid earlier scan date.'],
  ])('refuses invalid %s before IO', async (search, error) => { await refused(await request(search), 400, error); expect(fetcher.mock.calls).toEqual([]) })
})
describe('whole exact body and bounded failure', () => {
  it.each(['catalog', 'bounded-name-postings'] as const)('preserves full %s JSON and strips upstream cookies/cache headers', async plan => {
    const raw = JSON.stringify(nameFixture(plan)) + '\n'; fetcher.mockResolvedValue(new Response(raw, { headers: { 'set-cookie': 'private-backend-cookie', 'cache-control': 'public' } }))
    const response = await request(); expect(response.status).toBe(200); expect(await response.text()).toBe(raw)
    expect([...response.headers]).toEqual([['cache-control', 'private, no-store'], ['content-type', 'application/json; charset=utf-8'], ['x-query-engine', 'name-summary']])
    expect(fetcher.mock.calls).toEqual([[`https://catalog.example/api/name-summary?${query}`, { headers: { authorization: 'Bearer fake-secret', accept: 'application/json' }, signal: expect.any(AbortSignal), redirect: 'manual' }]])
  })
  it('accepts complete mixed comparisons without relabeling cold metadata as precomputed', async () => {
    const raw = JSON.stringify(nameDiff('catalog', 'bounded-name-postings')) + '\n'; fetcher.mockResolvedValue(new Response(raw))
    const response = await request(query + '&from=2026-10-04'); expect(response.status).toBe(200); expect(await response.text()).toBe(raw)
    expect(fetcher.mock.calls[0][0]).toBe(`https://catalog.example/api/name-summary?${query}&from=2026-10-04`)
  })
  it('forwards literal Unicode/punctuation exactly, without syntax interpretation', async () => {
    const name = 'Å😀\n%+_\\', params = new URLSearchParams({ date: '2026-10-05', name }), raw = JSON.stringify(nameFixture('bounded-name-postings', '2026-10-05', name.toLowerCase()))
    fetcher.mockResolvedValue(new Response(raw))
    const response = await request(params.toString()); expect(response.status).toBe(200); expect(await response.text()).toBe(raw)
    expect(fetcher.mock.calls[0][0]).toBe(`https://catalog.example/api/name-summary?${params}`)
  })
  it.each([{ QUERY_BOX_TOKEN: undefined }, { QUERY_BOX_HOT_L1_URL: undefined }, { QUERY_BOX_HOT_L1_URL: 'https://user:secret@catalog.example' }, { QUERY_BOX_HOT_L1_URL: 'ftp://catalog.example' }])('refuses invalid operator config %j', async override => {
    await refused(await request(query, { ...env, ...override }), 503, unavailable); expect(fetcher.mock.calls).toEqual([])
  })
  it('refuses redirect without forwarding credentials', async () => {
    const cancel = vi.fn(); fetcher.mockResolvedValue(new Response(new ReadableStream({ cancel }), { status: 302, headers: { location: 'https://untrusted.example' } }))
    await refused(await request(), 503, unavailable); expect(fetcher.mock.calls.length).toBe(1); expect(fetcher.mock.calls[0][1]?.redirect).toBe('manual'); expect(cancel.mock.calls).toEqual([[undefined]])
  })
  it.each([400, 503, 500, 401, 404])('sanitizes backend failure %s, never registered-only or zero', async status => {
    fetcher.mockResolvedValue(new Response('private host and source artifact path', { status, headers: { 'retry-after': '999' } }))
    const response = await request(); await refused(response, status === 400 ? 400 : 503, status === 400 ? 'Invalid name-summary request. Check the literal and frozen scan dates; this is not a zero-match result.' : unavailable)
    expect(fetcher.mock.calls.length).toBe(1); expect(response.headers.get('retry-after')).toBe(status === 400 ? null : '60')
  })
  it('sanitizes backend exception without retry/fallback', async () => {
    fetcher.mockRejectedValue(new Error('private-secret host')); await refused(await request(), 503, unavailable); expect(fetcher.mock.calls.length).toBe(1)
  })
  it('accepts exactly 64 KiB of validated JSON without serialization changes', async () => {
    const raw = JSON.stringify(nameFixture()).padEnd(NAME_MAX_BYTES, ' '); fetcher.mockResolvedValue(new Response(raw))
    const response = await request(); expect(response.status).toBe(200); expect(await response.text()).toBe(raw)
  })
  it.each([true, false])('cancels oversized declared/streamed response %s', async declared => {
    const cancel = vi.fn(); fetcher.mockResolvedValue(new Response(new ReadableStream({ start(c) { c.enqueue(new Uint8Array(NAME_MAX_BYTES + 1)) }, cancel }), { headers: declared ? { 'content-length': String(NAME_MAX_BYTES + 1) } : {} }))
    await refused(await request(), 503, unavailable); expect(cancel.mock.calls).toEqual([[undefined]])
  })
  it.each(['invalid utf8', 'JSON', 'partial', 'wrong plan', 'unsafe number', 'delta'])('refuses %s body instead of accepting partial results', async issue => {
    const body = nameDiff()
    if (issue === 'partial') body.buckets.pop()
    if (issue === 'wrong plan') Object.assign(body.after, { plan: 'fullscan' })
    if (issue === 'unsafe number') body.after.root.b = 9007199254740992
    if (issue === 'delta') body.delta.o = 0
    fetcher.mockResolvedValue(new Response(issue === 'invalid utf8' ? new Uint8Array([255]) : issue === 'JSON' ? '{' : JSON.stringify(body)))
    await refused(await request(query + '&from=2026-10-04'), 503, unavailable)
  })
  it.each(['fetch', 'body'])('bounds whole %s phase to eight seconds even if abort is ignored', async phase => {
    vi.useFakeTimers(); const cancel = vi.fn()
    if (phase === 'fetch') fetcher.mockImplementation(() => new Promise(() => {}))
    else fetcher.mockResolvedValue(new Response(new ReadableStream({ cancel })))
    const pending = request(); await vi.advanceTimersByTimeAsync(NAME_TIMEOUT_MS - 1)
    let done = false; void pending.then(() => { done = true }); expect(done).toBe(false)
    await vi.advanceTimersByTimeAsync(1); await refused(await pending, 503, unavailable)
    expect(fetcher.mock.calls.length).toBe(1); expect(fetcher.mock.calls[0][1]?.signal?.aborted).toBe(true); expect(cancel.mock.calls).toEqual(phase === 'body' ? [[undefined]] : [])
  })
})

const UNSET_SLASH = { error: 'Use one nonempty NUL/slash-free literal of at most 512 characters.' }
describe('an indexed-only deployment (`FILTER_INDEXED_ONLY=1`)', () => {
  it('refuses a name with `/` with its reason code; unset, the validator\'s own message (no code)', async () => {
    const flagged = await request('date=2026-10-05&name=a%2Fb', { ...env, FILTER_INDEXED_ONLY: '1' } as NameSummaryEnv)
    expect([flagged.status, await flagged.json(), fetcher.mock.calls]).toEqual([400, { error: 'Only plain text search (a substring of a file or folder name) is supported here; a term can’t contain “/” (it matches within one name).', code: 'unsupported-slash' }, []])
    const unset = await request('date=2026-10-05&name=a%2Fb')
    expect([unset.status, await unset.json()]).toEqual([400, UNSET_SLASH])
  })
})

describe('an anchored name (`^q`, `q$`): /names reads literals only', () => {
  const ANCHOR = { error: 'Starts-with (^) and ends-with ($) search isn’t indexed on this deployment yet; search for a plain substring instead.', code: 'anchor-not-indexed' }
  it('is `anchor-not-indexed` (flag set or not), never forwarded; a lone `^`/`$` or an inner one stays a literal', async () => {
    const got = await Promise.all(['^qwen', 'qwen$', '^qwen$'].flatMap(name => [env, { ...env, FILTER_INDEXED_ONLY: '1' } as NameSummaryEnv].map(async vars => {
      const r = await request(new URLSearchParams({ date: '2026-10-05', name }).toString(), vars)
      return [r.status, await r.json()]
    })))
    expect([got, fetcher.mock.calls]).toEqual([Array(6).fill([400, ANCHOR]), []])
    const literal = []
    for (const name of ['^', '$', 'a^b$c']) {
      fetcher.mockResolvedValueOnce(new Response(JSON.stringify(nameFixture('bounded-name-postings', '2026-10-05', name))))
      literal.push((await request(new URLSearchParams({ date: '2026-10-05', name }).toString())).status)
    }
    expect([literal, fetcher.mock.calls.length]).toEqual([[200, 200, 200], 3])
  })
  it('`\^q`, `q\$`, `\\` and a quoted `"^q"` are literals: forwarded unescaped, answered; a bare `^foo` / `foo$` still refused', async () => {
    const got = []
    for (const [name, literal] of [['foo\\$', 'foo$'], ['\\^foo', '^foo'], ['"^foo"', '^foo'], ['"foo$"', 'foo$'], ['^foo\\$', null], ['a\\\\b', 'a\\b'], ['^foo', null], ['foo$', null], ['^$', '^$']]) {
      if (literal) fetcher.mockResolvedValueOnce(new Response(JSON.stringify(nameFixture('bounded-name-postings', '2026-10-05', literal))))
      const before = fetcher.mock.calls.length
      const r = await request(new URLSearchParams({ date: '2026-10-05', name: name! }).toString())
      const sent = fetcher.mock.calls.slice(before).map(c => new URL(String(c[0])).searchParams.get('name'))
      got.push([name, r.status, r.status === 200 ? sent : await r.json()])
    }
    expect(got).toEqual([
      ['foo\\$', 200, ['foo$']],
      ['\\^foo', 200, ['^foo']],
      ['"^foo"', 200, ['^foo']],
      ['"foo$"', 200, ['foo$']],
      ['^foo\\$', 400, ANCHOR],
      ['a\\\\b', 200, ['a\\b']],
      ['^foo', 400, ANCHOR],
      ['foo$', 400, ANCHOR],
      ['^$', 200, ['^$']],
    ])
  })
})

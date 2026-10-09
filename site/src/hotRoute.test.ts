import { afterEach, describe, expect, it, vi } from 'vitest'
import { HOT_SCOPE, HotUnavailable, loadHot } from './hotModel'

const body = { schema: 'hot-l1-batch-catalog-v1', exact: true, incremental: false, levels: 1, scope: HOT_SCOPE,
  target: 'fixture', date: '2026-10-05', pattern: '.json', path: '', root: { b: 3, o: 4 },
  buckets: Array.from({ length: 6 }, (_, i) => ({ path: `bucket-${i}`, pre: i + 1, post: i + 1, b: i === 0 ? 3 : 0, o: i === 0 ? 4 : 0 })) }

afterEach(() => vi.unstubAllGlobals())
describe('client-only root summary request', () => {
  it('forwards only the frozen literal request and abort signal', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => Response.json(body)))
    const signal = new AbortController().signal
    const result = await loadHot({ date: '2026-10-05', name: '.JSON' }, signal)
    expect(vi.mocked(fetch).mock.calls).toEqual([['/api/hot-l1?date=2026-10-05&name=.json', { signal }]])
    expect(result).toEqual({ after: { target: 'fixture', date: '2026-10-05', pattern: '.json', root: { b: 3, o: 4 }, buckets: body.buckets } })
  })
  it('preserves the explicit earlier date without alternate endpoints or options', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('', { status: 400 })))
    await expect(loadHot({ date: '2026-10-05', name: '.json', from: '2026-10-04' })).rejects.toBeInstanceOf(HotUnavailable)
    expect(vi.mocked(fetch).mock.calls).toEqual([['/api/hot-l1?date=2026-10-05&name=.json&from=2026-10-04', { signal: undefined }]])
  })
  it('refuses unsupported dates before fetching', async () => {
    vi.stubGlobal('fetch', vi.fn())
    await expect(loadHot({ date: '2026-10-06', name: '.json' })).rejects.toEqual(new Error('Only the frozen 2026-10-04 and 2026-10-05 scans are available.'))
    expect(vi.mocked(fetch).mock.calls).toEqual([])
  })
  it.each([400, 404, 409, 422])('reports %s as unavailable, never zero, without rendering backend content', async status => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('<html>private backend detail</html>', { status })))
    await expect(loadHot({ date: '2026-10-05', name: '.json' })).rejects.toEqual(new HotUnavailable(
      'This pattern/date is unavailable in the frozen preview. It is not a zero-match result. Try .json or another registered literal.',
    ))
    expect(vi.mocked(fetch).mock.calls).toEqual([['/api/hot-l1?date=2026-10-05&name=.json', { signal: undefined }]])
  })
  it.each([[401, 'Sign in to view the frozen preview.'], [503, 'Root-summary request failed (503). Try again.']] as const)('reports status %s safely', async (status, message) => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('', { status })))
    await expect(loadHot({ date: '2026-10-05', name: '.json' })).rejects.toEqual(new Error(message))
  })
  it('rejects successful replies that echo a different date', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => Response.json({ ...body, date: '2026-10-04' })))
    await expect(loadHot({ date: '2026-10-05', name: '.json' })).rejects.toEqual(new Error('Preview does not match the requested scan and literal.'))
  })
})

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { onRequestGet } from '../functions/api/coarse'
import { requireViewer } from '../functions/_lib/auth'

vi.mock('../functions/_lib/auth.js', async importOriginal => ({
  ...await importOriginal<typeof import('../functions/_lib/auth')>(),
  requireViewer: vi.fn(async () => ({ email: null, name: 'test', scopes: ['gcs'], admin: false, via: 'session', subject: null })),
}))

const context = (query = 'date=2026-10-04&name=zarr.json') => ({
  request: new Request(`https://example.test/api/coarse?${query}`),
  env: { QUERY_BOX_COARSE: '1', QUERY_BOX_URL: 'https://box.example.test', QUERY_BOX_TOKEN: 'fixture-token' },
})

describe('dev-only coarse route', () => {
  beforeEach(() => vi.stubGlobal('fetch', vi.fn(async () => new Response('{"schema":"coarse-v1"}', { headers: { 'x-query-engine': 'box;dur=12' } }))))
  afterEach(() => { vi.unstubAllGlobals(); vi.clearAllMocks() })
  it('requires the explicit preview flag', async () => {
    const ctx = context()
    ctx.env.QUERY_BOX_COARSE = '0'
    const res = await onRequestGet(ctx)
    expect([res.status, await res.json()]).toEqual([404, { error: 'Coarse preview is not enabled.' }])
    expect(fetch).toHaveBeenCalledTimes(0)
  })
  it('keeps the existing viewer gate ahead of the backend', async () => {
    vi.mocked(requireViewer).mockResolvedValueOnce(Response.json({ error: 'Sign in' }, { status: 401 }))
    const res = await onRequestGet(context())
    expect([res.status, await res.json()]).toEqual([401, { error: 'Sign in' }])
    expect(fetch).toHaveBeenCalledTimes(0)
  })
  it('refuses full-search options instead of silently ignoring them', async () => {
    const res = await onRequestGet(context('date=2026-10-04&name=zarr.json&q=ignored'))
    expect([res.status, await res.json()]).toEqual([400, { error: 'Use a literal basename pattern without full-search or owner/class options.' }])
    expect(fetch).toHaveBeenCalledTimes(0)
  })
  it('forwards only the bounded read and keeps the response private', async () => {
    const res = await onRequestGet(context())
    expect([res.status, await res.json(), res.headers.get('cache-control'), res.headers.get('x-query-engine')])
      .toEqual([200, { schema: 'coarse-v1' }, 'private, no-store', 'box;dur=12'])
    expect(vi.mocked(fetch).mock.calls.map(call => [String(call[0]), call[1]?.headers]))
      .toEqual([['https://box.example.test/api/coarse?date=2026-10-04&name=zarr.json', { authorization: 'Bearer fixture-token' }]])
  })
  it('uses a coarse-only endpoint without needing canonical box routing', async () => {
    const ctx = {
      ...context(),
      env: { QUERY_BOX_COARSE: '1', QUERY_BOX_COARSE_URL: 'https://coarse.example.test/', QUERY_BOX_TOKEN: 'fixture-token' },
    }
    const res = await onRequestGet(ctx)
    expect([res.status, await res.json(), res.headers.get('cache-control')])
      .toEqual([200, { schema: 'coarse-v1' }, 'private, no-store'])
    expect(vi.mocked(fetch).mock.calls.map(call => [String(call[0]), call[1]?.headers]))
      .toEqual([['https://coarse.example.test/api/coarse?date=2026-10-04&name=zarr.json', { authorization: 'Bearer fixture-token' }]])
  })
  it('prefers the isolated endpoint when both endpoints are configured', async () => {
    await onRequestGet({ ...context(), env: { ...context().env, QUERY_BOX_COARSE_URL: 'https://coarse.example.test' } })
    expect(vi.mocked(fetch).mock.calls.map(call => String(call[0])))
      .toEqual(['https://coarse.example.test/api/coarse?date=2026-10-04&name=zarr.json'])
  })
  it('preserves both frozen dates for a bounded diff', async () => {
    await onRequestGet(context('date=2026-10-05&date0=2026-10-04&name=zarr.json&budget=64'))
    expect(vi.mocked(fetch).mock.calls.map(call => String(call[0])))
      .toEqual(['https://box.example.test/api/coarse?date=2026-10-05&date0=2026-10-04&name=zarr.json&budget=64'])
  })
  it('does not drop the requested predicate mode', async () => {
    await onRequestGet(context('date=2026-10-05&name=.npy&mode=contains'))
    expect(vi.mocked(fetch).mock.calls.map(call => String(call[0])))
      .toEqual(['https://box.example.test/api/coarse?date=2026-10-05&name=.npy&mode=contains'])
  })
  it('forwards the bounded refinement depth', async () => {
    await onRequestGet(context('date=2026-10-05&name=.npy&mode=contains&levels=3'))
    expect(vi.mocked(fetch).mock.calls.map(call => String(call[0])))
      .toEqual(['https://box.example.test/api/coarse?date=2026-10-05&name=.npy&mode=contains&levels=3'])
  })
  it('preserves the explicit full-path coverage mode and both dates', async () => {
    await onRequestGet(context('date=2026-10-05&date0=2026-10-04&name=datakit&mode=coverage&levels=1'))
    expect(vi.mocked(fetch).mock.calls.map(call => String(call[0])))
      .toEqual(['https://box.example.test/api/coarse?date=2026-10-05&date0=2026-10-04&name=datakit&mode=coverage&levels=1'])
  })
  it('reports unsupported directory patterns without a canonical fallback', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(new Response('matching directories', { status: 501 }))
    const res = await onRequestGet(context('date=2026-10-05&name=datakit&mode=contains'))
    expect([res.status, await res.json(), res.headers.get('cache-control')])
      .toEqual([422, { error: 'Patterns matching directory names are not supported by this preview.' }, 'private, no-store'])
    expect(vi.mocked(fetch).mock.calls.map(call => String(call[0])))
      .toEqual(['https://box.example.test/api/coarse?date=2026-10-05&name=datakit&mode=contains'])
  })
  it('wraps a backend validation message in a private JSON response', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(new Response('budget invalid\n', { status: 400 }))
    const res = await onRequestGet(context())
    expect([res.status, await res.json(), res.headers.get('cache-control')])
      .toEqual([400, { error: 'budget invalid' }, 'private, no-store'])
  })
})

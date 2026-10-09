/** With the query box gone (specs/ch-store-retirement.md), nothing on the site calls it. The deployment's vars are
 *  gcs prod's (static name search on, no `QUERY_BOX_*` endpoint), and the box-only routes are off. Every case asserts
 *  the exact response and that `fetch` was never called: a static failure is a 503, never a box fallback, even with a
 *  box endpoint and token still configured (the dev preview's vars). */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { requireViewer, type Identity } from '../_lib/auth.js'
import type { NameSummaryEnv } from '../_lib/nameSummary.js'
import { onRequest as nameSummary } from './name-summary.js'
import { onRequest as nameRegistry } from './name-summary-registry.js'
import { onRequest as hotL1 } from './hot-l1.js'
import { onRequest as hotL2 } from './hot-l2.js'
import { onRequestGet as coarse } from './coarse.js'

vi.mock('../_lib/auth.js', async original => ({ ...await original<typeof import('../_lib/auth.js')>(), requireViewer: vi.fn() }))
const viewer: Identity = { email: null, name: null, scopes: ['gcs'], admin: false, via: 'session', subject: null }
const r2Down = (): R2Bucket => {
  const down = async () => { throw new Error('R2 unavailable') }
  return { get: down, head: down, list: down } as unknown as R2Bucket
}
/** gcs prod's name-search vars once the box flags are dropped: the static index alone. */
const staticOnly = (): NameSummaryEnv => ({ BASE_SCOPE: 'gcs', NAME_SUMMARY_STATIC: '1', INDEX_R2: r2Down(), STORE_BUCKETS: 'marin-a,marin-b' })
/** The dev preview's: the static index, plus a box endpoint and token that must stay unused. */
const staticWithBox = (): NameSummaryEnv => ({
  ...staticOnly(), QUERY_BOX_NAME_SUMMARY: '1', QUERY_BOX_DATED_NAMES: '1', QUERY_BOX_HOT_L1_URL: 'https://box.example', QUERY_BOX_TOKEN: 'fake-secret',
})
const unavailable = { error: 'Name summary is unavailable, busy or exceeded its work budget. This is not a zero-match result. Try again.' }
const get = (path: string) => new Request(`https://gcs.example/api/${path}`)
let fetcher: ReturnType<typeof vi.fn<typeof fetch>>

async function expectJson(response: Response, status: number, body: unknown) {
  expect([response.status, await response.json(), response.headers.get('cache-control')]).toEqual([status, body, 'private, no-store'])
}
beforeEach(() => {
  vi.mocked(requireViewer).mockReset().mockResolvedValue(viewer)
  fetcher = vi.fn<typeof fetch>().mockRejectedValue(new Error('the box was called'))
  vi.stubGlobal('fetch', fetcher)
  vi.stubGlobal('caches', { default: { match: async () => undefined, put: async () => undefined } })
  vi.spyOn(console, 'error').mockImplementation(() => {})
})
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

describe('name summaries on the static index alone', () => {
  it.each([['prod (no box vars)', staticOnly], ['preview (box configured)', staticWithBox]])('%s: a failing R2 is a 503, never a box call', async (_, vars) => {
    // A scan past the frozen pair: dated mode comes from the static index, not `QUERY_BOX_DATED_NAMES`.
    const summary = await nameSummary({ request: get('name-summary?date=2026-10-09&name=datakit&from=2026-10-08'), env: vars() })
    await expectJson(summary, 503, unavailable)
    expect(summary.headers.get('retry-after')).toBe('1')
    await expectJson(await nameRegistry({ request: get('name-summary-registry'), env: vars() }), 503, unavailable)
    expect(fetcher.mock.calls).toEqual([])
  })
  it('without the R2 binding and without a box: 503, no fetch', async () => {
    const env: NameSummaryEnv = { BASE_SCOPE: 'gcs', NAME_SUMMARY_STATIC: '1', QUERY_BOX_NAME_SUMMARY: '1', QUERY_BOX_DATED_NAMES: '1' }
    await expectJson(await nameSummary({ request: get('name-summary?date=2026-10-09&name=datakit'), env }), 503, unavailable)
    await expectJson(await nameRegistry({ request: get('name-summary-registry'), env }), 503, unavailable)
    expect(fetcher.mock.calls).toEqual([])
  })
  it('neither the static index nor the box flag: the routes are off', async () => {
    const env: NameSummaryEnv = { BASE_SCOPE: 'gcs', NAME_SUMMARY_STATIC: '1' }
    await expectJson(await nameSummary({ request: get('name-summary?date=2026-10-09&name=datakit'), env }), 404, { error: 'Name summaries are not enabled.' })
    await expectJson(await nameRegistry({ request: get('name-summary-registry'), env }), 404, { error: 'Name summaries are not enabled.' })
    expect(fetcher.mock.calls).toEqual([])
  })
})

describe('box-only routes with the box flags off', () => {
  it('hot-l1, hot-l2 and coarse answer 404 without a fetch', async () => {
    const env = { ...staticOnly(), QUERY_BOX_HOT_L1_URL: 'https://box.example', QUERY_BOX_TOKEN: 'fake-secret', QUERY_BOX_URL: 'https://box.example' }
    await expectJson(await hotL1({ request: get('hot-l1?date=2026-10-05&name=x'), env }), 404, { error: 'Hot L1 is not enabled.' })
    await expectJson(await hotL2({ request: get('hot-l2?date=2026-10-05&name=x'), env }), 404, { error: 'Hot L2 is not enabled.' })
    await expectJson(await coarse({ request: get('coarse?date=2026-10-05&name=x'), env } as Parameters<typeof coarse>[0]), 404, { error: 'Coarse preview is not enabled.' })
    expect(fetcher.mock.calls).toEqual([])
  })
})

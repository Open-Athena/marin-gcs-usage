import { QueryClient, QueryObserver } from '@tanstack/react-query'
import { describe, expect, it } from 'vitest'
import { COVER_V } from '../functions/_lib/cover'
import { assignInBatches, chainOf, CoverError, coverQuery, coverRetry, coverWant, rowItems, stageInBatches } from './filterCover'
import { stageMany } from './plans'

describe('large sets go in batches, never refused', () => {
  const prefixes = Array.from({ length: 1234 }, (_, i) => `gs://b/d/${i}/`)
  it('assign: 500 actions per POST, in order, with progress', async () => {
    const posts: number[] = [], seen: string[] = [], prog: [number, number][] = []
    const n = await assignInBatches(prefixes, pattern => ({ pattern, owner: '@me' }), async a => { posts.push(a.length); seen.push(...a.map(x => x.pattern)) }, (d, t) => prog.push([d, t]))
    expect([n, posts, seen, prog]).toEqual([1234, [500, 500, 234], prefixes, [[500, 1234], [1000, 1234], [1234, 1234]]])
  })
  it('stage: 500 prefixes per POST, then the batches fold into the first', async () => {
    const calls: unknown[] = []
    let next = 40
    const r = await stageInBatches(prefixes,
      async part => { calls.push(['stage', part.length]); return { plan_id: 7, batch_id: next++ } },
      async (plan, into, ids) => { calls.push(['merge', plan, into, ids]) })
    expect([r, calls]).toEqual([{ plan_id: 7, batch_id: 40, batches: 3 }, [['stage', 500], ['stage', 500], ['stage', 234], ['merge', 7, 40, [41, 42]]]])
  })
  it('stage: one batch is one POST and no merge; a part that staged nothing (all covered) is no batch', async () => {
    const calls: unknown[] = []
    expect(await stageInBatches(prefixes.slice(0, 3), async part => { calls.push(part.length); return { plan_id: 7, batch_id: 9 } }, async () => { calls.push('merge') })).toEqual({ plan_id: 7, batch_id: 9, batches: 1 })
    let i = 0
    expect(await stageInBatches(prefixes.slice(0, 1000), async () => ({ plan_id: 7, batch_id: i++ ? null : 5 }), async () => { calls.push('merge') })).toEqual({ plan_id: 7, batch_id: 5, batches: 1 })
    expect(calls).toEqual([3])
  })
  it('the stage API client sends exactly those requests and returns one result', async () => {
    const sent: [string, string, unknown][] = []
    let b = 1
    const post = (async (url: string, method: string, body: { prefixes?: string[] }) => {
      sent.push([url, method, body.prefixes ? body.prefixes.length : body])
      return url.endsWith('/stage') ? { plan_id: 3, batch_id: b++, staged: body.prefixes, covered: [], absorbed: [], as_of: null } : {}
    }) as never
    const r = await stageMany(prefixes.slice(0, 600), ' why ', post)
    expect([sent, r.plan_id, r.batch_id, r.staged.length]).toEqual([[
      ['/api/plans/stage', 'POST', 500], ['/api/plans/stage', 'POST', 100], ['/api/plans/3/batches/merge', 'POST', { into: 1, ids: [2] }],
    ], 3, 1, 600])
  })
})

describe('a filtered row: its label and its matches', () => {
  it('a row holding one chain of single children shows the path down it, as its tile does', () => {
    expect(chainOf({ n: 'marin-eu-west4', c: [{ n: 'tomat', c: [{ n: 'a' }, { n: 'b' }] }] })).toEqual({ label: 'marin-eu-west4/tomat', segs: ['marin-eu-west4', 'tomat'] })
    expect(chainOf({ n: 'a', c: [{ n: 'b', c: [{ n: 'c', c: [{ n: 'd' }] }] }] })).toEqual({ label: 'a/…/d', segs: ['a', 'b', 'c', 'd'] })
    expect(chainOf({ n: 'a', c: [{ n: '(other)' }] })).toEqual({ label: 'a', segs: ['a'] })
    expect(chainOf({ n: 'a', c: [{ n: 'x' }, { n: 'y' }] })).toEqual({ label: 'a', segs: ['a'] })
  })
  it('a row\'s matches are the items at or under it, never a sibling sharing its name as a prefix', () => {
    const items = [{ path: 'b/x/tomat', kind: 'dir' as const, b: 1, o: 1, roots: 1 }, { path: 'b/xy/tomat', kind: 'dir' as const, b: 2, o: 1, roots: 1 }, { path: 'b/x', kind: 'dir' as const, b: 3, o: 1, roots: 1 }]
    expect(rowItems(items, 'b/x').map(i => i.path)).toEqual(['b/x/tomat', 'b/x'])
  })
})

describe('the cover is fetched on demand, and retried once on a 5xx', () => {
  const COVER = { date: 'D', path: 'p', q: 'nemotron', items: [], roots: { n: 0, b: 0, o: 0 }, complete: true, looked: 0, unchecked: 0 }
  /** A fetcher answering `statuses` in turn (then 200 with `COVER`), recording each URL. */
  const fetcher = (statuses: number[]) => {
    const urls: string[] = []
    const f = async (url: string) => {
      urls.push(url)
      const s = statuses.shift() ?? 200
      return new Response(s === 200 ? JSON.stringify(COVER) : JSON.stringify({ error: `boom ${s}` }), { status: s })
    }
    return { f, urls }
  }
  const args = { date: 'D', path: 'p', q: 'nemotron', qs: 'simple' }
  const URL = `/api/filter-cover?cv=${COVER_V}&date=D&path=p&q=nemotron&qs=simple`
  /** The query as the page observes it (`useQuery`'s observer), settled; retries without their delay. */
  const observe = async (f: (u: string) => Promise<Response>, enabled: boolean) => {
    const client = new QueryClient()
    const obs = new QueryObserver(client, { ...coverQuery(f, 'primary', { ...args, enabled }), retryDelay: 0 })
    const unsub = obs.subscribe(() => {})
    await new Promise(r => setTimeout(r, 20))
    const r = obs.getCurrentResult()
    unsub()
    return [r.status, r.fetchStatus, r.failureCount, r.error ? [r.error.constructor.name, r.error.message] : null, r.data ?? null]
  }

  it('not wanted: no request at all', async () => {
    const { f, urls } = fetcher([])
    expect([await observe(f, false), urls]).toEqual([['pending', 'idle', 0, null, null], []])
  })

  it('wanted: one request; a 503 then a 200 is two, and the answer', async () => {
    const ok = fetcher([])
    const flaky = fetcher([503])
    expect([await observe(ok.f, true), ok.urls, await observe(flaky.f, true), flaky.urls]).toEqual([
      ['success', 'idle', 0, null, COVER], [URL],
      ['success', 'idle', 0, null, COVER], [URL, URL],
    ])
  })

  it('two 5xx: gives up after the one retry; a 4xx is never retried', async () => {
    const down = fetcher([503, 500, 200])
    const bad = fetcher([400, 200])
    expect([await observe(down.f, true), down.urls.length, await observe(bad.f, true), bad.urls.length]).toEqual([
      ['error', 'idle', 2, ['CoverError', 'boom 500'], null], 2,
      ['error', 'idle', 1, ['CoverError', 'boom 400'], null], 1,
    ])
  })

  it('coverRetry / coverWant', () => {
    const e = (s: number) => new CoverError('x', s)
    expect([coverRetry(0, e(503)), coverRetry(1, e(503)), coverRetry(0, e(404)), coverRetry(0, new Error('net'))]).toEqual([true, false, false, false])
    expect([coverWant(args), coverWant({ ...args, path: 'p/q' }) === coverWant(args), coverWant({ date: null, path: '', q: undefined })]).toEqual([
      '["D","p","nemotron","simple"]', false, '[null,"","",""]',
    ])
  })
})

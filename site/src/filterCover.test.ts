import { describe, expect, it } from 'vitest'
import { assignInBatches, chainOf, rowItems, stageInBatches } from './filterCover'
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

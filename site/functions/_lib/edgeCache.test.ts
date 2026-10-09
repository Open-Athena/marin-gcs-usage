import { describe, expect, it } from 'vitest'
import { cacheKeyFor, cacheMatch, cacheStore, keepFor, PARTIAL_TTL } from './edgeCache'

// `cacheStore` keeps an answer in the colo cache and KV; a budget-cut answer (a filter view whose phase 2 its
// time budget cut short) in the colo cache only, for `PARTIAL_TTL` seconds; `false`: served, not kept.
describe('cacheStore', () => {
  const setup = () => {
    const colo: string[] = []
    const kv: string[] = []
    ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async (k: Request) => { colo.push(k.url) } } }
    const env = { CACHE_KV: { put: async (k: string) => { kv.push(k) } } as unknown as KVNamespace }
    return { colo, kv, env }
  }
  const key = cacheKeyFor('subtree', 'd/p?q=x')
  // The awaited put's duration varies: `awaited;dur=<ms>`.
  const seen = async (r: Response) => [r.headers.get('x-cache'), r.headers.get('x-cache-store')?.replace(/dur=\d+/, 'dur=<ms>'), r.headers.get('server-timing'), await r.text()]

  it('stores in both tiers by default', async () => {
    const { colo, kv, env } = setup()
    const r = await cacheStore(env, key, '{"a":1}', { 'server-timing': 'total;dur=5' })
    expect([await seen(r), colo, kv.length]).toEqual([['miss', 'awaited;dur=<ms>', 'total;dur=5', '{"a":1}'], [key.url], 1])
  })

  it('`store` false: served, in neither tier', async () => {
    const { colo, kv, env } = setup()
    const r = await cacheStore(env, key, '{"a":1}', { 'server-timing': 'total;dur=5' }, undefined, false)
    expect([await seen(r), colo, kv]).toEqual([['miss', 'skipped', 'total;dur=5', '{"a":1}'], [], []])
  })

  it('a budget-cut answer (`keepFor(interiors.late)`): colo only, `PARTIAL_TTL` s, flagged on the miss and on the hit', async () => {
    const stored: [string, string | null, string | null, string][] = []
    const held = new Map<string, Response>()
    ;(globalThis as unknown as { caches: unknown }).caches = { default: {
      match: async (k: Request) => held.get(k.url)?.clone(),
      put: async (k: Request, r: Response) => { stored.push([k.url, r.headers.get('cache-control'), r.headers.get('x-cache-partial'), await r.clone().text()]); held.set(k.url, r) },
    } }
    const kv: string[] = []
    const env = { CACHE_KV: { put: async (k: string) => { kv.push(k) }, get: async () => null } as unknown as KVNamespace }
    const keep = keepFor({ late: 3 })
    const miss = await cacheStore(env, key, '{"budgetCut":true}', {}, undefined, keep)
    const hit = (await cacheMatch(env, key))!
    const flags = async (r: Response) => [r.headers.get('x-cache'), r.headers.get('x-cache-store')?.replace(/dur=\d+/, 'dur=<ms>') ?? null, r.headers.get('x-cache-partial'), r.headers.get('cache-control'), await r.text()]
    expect([keep, PARTIAL_TTL, await flags(miss), await flags(hit), stored, kv]).toEqual([
      { partialTtl: 120 }, 120,
      ['miss', 'partial;ttl=120;awaited;dur=<ms>', 'ttl=120', 'private, max-age=120', '{"budgetCut":true}'],
      ['hit', null, 'ttl=120', 'private, max-age=120', '{"budgetCut":true}'],
      [[key.url, 'public, s-maxage=120', 'ttl=120', '{"budgetCut":true}']],
      [],
    ])
  })

  it('`keepFor`: whole answers (no interiors, or only read-budget skips) are kept in both tiers', () => {
    expect([keepFor(undefined), keepFor({}), keepFor({ late: 0 }), keepFor({ late: 1 })]).toEqual([true, true, true, { partialTtl: PARTIAL_TTL }])
  })
})

import { describe, expect, it } from 'vitest'
import { cacheKeyFor, cacheStore } from './edgeCache'

// `cacheStore` keeps an answer in the colo cache and KV — unless the caller says the answer may differ on a
// retry (a filter view whose phase 2 its time budget cut short): then it is served and not kept.
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
})

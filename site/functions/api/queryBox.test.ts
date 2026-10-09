import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import type { Env } from '../_lib/auth'
import { sqliteD1 } from '../_lib/testD1'
import { type D1Variant, fixture, FILES, readJson, seedGeneration } from '../_lib/testStore'
import { SEARCH_FILES, searchKey } from '../_lib/search'
import { BOX_MAX_BYTES, boxFor, withProvenance } from '../_lib/queryBox'
import { onRequestGet as subtree } from './subtree'
import { onRequestGet as diff } from './diff'
import { onRequestGet as series } from './series'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('../_lib/testStore')).S3Store }))

// The Worker's hand-off to the serving box (`_lib/queryBox.ts`): unset, the
// routes answer as they always did and never fetch; set, the box's body is the
// answer, and a box that declines, fails, stalls or overflows falls back to
// the Worker's own read — the same bytes as with no box — with the reason in
// `x-query-engine`.

const A = '2026-10-01T0001'
const C = '2026-10-02T0001'
const BOX = 'http://box.test'
const dirOf = (date: string) => `cw-l2/${date}/index/g`
let base: Env

beforeAll(async () => {
  ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async () => {} } }
  const { db, raw } = await sqliteD1('cw')
  for (const [date, dir] of [[A, 'v2-search'], [C, 'v2-search-b']]) {
    const v = await readJson<Record<string, D1Variant>>(`${dir}/d1.json`)
    const files = { path: { parquet: `${dir}/path-index.parquet`, groups: `${dir}/path-index.groups.json` }, bysize: { parquet: `${dir}/path-index-bysize.parquet`, groups: `${dir}/path-index-bysize.groups.json` } }
    seedGeneration(raw, { date, gen: 'g', dir: dirOf(date), variants: v, files })
    for (const role of ['rows', 'trigrams', 'rowsSearch'] as const) FILES.set(searchKey(dirOf(date), role), fixture(`${dir}/${SEARCH_FILES[role]}`))
  }
  base = { DB: db, ROOT_LABEL: 'root', GCS_HMAC_KEY_ID: 'k', GCS_HMAC_SECRET: 's', STORE_BUCKET: 'my-data' } as Env
})

const realFetch = globalThis.fetch
afterEach(() => { globalThis.fetch = realFetch })

/** A box answering `(url, init) → Response`, recording what it was asked. */
function mockBox(answer: (url: string, init?: RequestInit) => Response | Promise<Response>): string[][] {
  const asked: string[][] = []
  globalThis.fetch = vi.fn(async (u: string | URL | Request, init?: RequestInit) => {
    const url = String(u)
    asked.push([url, new Headers(init?.headers).get('authorization') ?? ''])
    return answer(url, init)
  }) as typeof fetch
  return asked
}

type Route = typeof subtree
const call = async (route: Route, qs: string, env: Env) => {
  // (The colo cache stub never hits and there's no KV tier: every call computes.)
  const r = await route({ request: new Request(`http://localhost/api/x?${qs}`), env })
  return { status: r.status, engine: r.headers.get('x-query-engine'), body: await r.text() }
}

describe('QUERY_BOX_URL unset', () => {
  it('never asks a box, and adds no header', async () => {
    const asked = mockBox(() => { throw new Error('no box') })
    const r = await call(subtree, `date=${A}&path=bk&q=ttl`, base)
    expect([r.status, r.engine, asked]).toEqual([200, null, []])
  })
  it('a coarse-only URL does not fetch or alter canonical subtree, diff or series answers', async () => {
    const asked = mockBox(() => { throw new Error('canonical requests must not fetch') })
    const env = { ...base, QUERY_BOX_COARSE: '1', QUERY_BOX_COARSE_URL: 'https://coarse.test', QUERY_BOX_TOKEN: 'tok' }
    const routes = [subtree, diff, series] as Route[]
    const queries = [`date=${A}&path=bk&q=ttl`, `from=${A}&to=${C}&path=bk`, 'path=bk']
    for (const [i, route] of routes.entries()) {
      const original = await call(route, queries[i], base)
      const isolated = await call(route, queries[i], env)
      expect(isolated).toEqual(original)
      expect(isolated.engine).toEqual(null)
    }
    expect(asked).toEqual([])
  })
})

describe('coarse endpoint isolation', () => {
  it('selects only the endpoint for the requested contract', () => {
    const url = new URL('https://site.test/api/coarse')
    const cases = [
      [base, null, null],
      [{ ...base, QUERY_BOX_COARSE_URL: 'https://coarse.test/' }, null, 'https://coarse.test'],
      [{ ...base, QUERY_BOX_URL: 'https://canonical.test/' }, 'https://canonical.test', 'https://canonical.test'],
      [{ ...base, QUERY_BOX_URL: 'https://canonical.test/', QUERY_BOX_COARSE_URL: 'https://coarse.test/' }, 'https://canonical.test', 'https://coarse.test'],
    ] as const
    expect(cases.map(([env]) => [boxFor(env, url), boxFor(env, url, 'coarse')]))
      .toEqual(cases.map(([, canonical, coarse]) => [canonical, coarse]))
  })
})

describe('QUERY_BOX_URL set', () => {
  const env = () => ({ ...base, QUERY_BOX_URL: BOX, QUERY_BOX_TOKEN: 'tok', QUERY_BOX_TIMEOUT_MS: '200' }) as Env

  it("the box's answer is the response (subtree, diff, series), its engine named", async () => {
    const asked = mockBox(url => new Response(`{"from":"box","url":${JSON.stringify(url)}}`, { headers: { 'x-query-engine': 'box;dur=7' } }))
    const got = [
      await call(subtree, `date=${A}&path=bk&q=ttl`, env()),
      await call(diff, `from=${A}&to=${C}&path=bk`, env()),
    ]
    const s = await series({ request: new Request(`http://localhost/api/series?path=bk`), env: env() } as never)
    expect([...got.map(g => [g.status, g.engine, JSON.parse(g.body)]), [s.status, s.headers.get('x-query-engine'), JSON.parse(await s.text())]]).toEqual([
      [200, 'box;dur=7', { from: 'box', url: `${BOX}/api/subtree?date=${A}&path=bk&q=ttl` }],
      [200, 'box;dur=7', { from: 'box', url: `${BOX}/api/diff?from=${A}&to=${C}&path=bk` }],
      // The series carries the scans the Worker knows.
      [200, 'box;dur=7', { from: 'box', url: `${BOX}/api/series?path=bk&n=2&first=${A}&last=${C}` }],
    ])
    expect(asked.map(a => a[1])).toEqual(['Bearer tok', 'Bearer tok', 'Bearer tok'])
  })

  it('a declining, failing, stalled or oversized box: the Worker answers, its bytes as with no box', async () => {
    const cases: [string, (url: string, init?: RequestInit) => Response | Promise<Response>][] = [
      ['409', () => new Response('scan not in the store', { status: 409 })],
      ['501', () => new Response('not supported', { status: 501 })],
      ['503', () => new Response('loading', { status: 503 })],
      ['500', () => new Response('boom', { status: 500 })],
      ['unreachable', () => { throw new TypeError('fetch failed') }],
      ['timeout', (_u, init) => new Promise<Response>((_, rej) => init?.signal?.addEventListener('abort', () => rej(init.signal!.reason)))],
      ['too-big', () => new Response('x', { headers: { 'content-length': String(BOX_MAX_BYTES + 1) } })],
    ]
    for (const route of [subtree, diff] as Route[]) {
      const qs = route === subtree ? `date=${A}&path=bk&q=ttl` : `from=${A}&to=${C}&path=bk`
      mockBox(() => { throw new Error('no box') })
      const plain = await call(route, qs, base)
      for (const [why, answer] of cases) {
        mockBox(answer)
        const r = await call(route, qs, env())
        expect([why, r.status, r.engine, r.body]).toEqual([why, 200, `worker;fallback=${why}`, plain.body])
      }
    }
  })

  it("the box's 400 / 404 are the answer; scoped requests never go to the box", async () => {
    const asked = mockBox(url => url.includes('nope') ? new Response('path not found', { status: 404, headers: { 'x-query-engine': 'box;dur=1' } }) : new Response('bad', { status: 400 }))
    const nf = await call(subtree, `date=${A}&path=nope`, env())
    const scoped = await call(subtree, `date=${A}&path=bk&o=unclaimed`, env())
    expect([[nf.status, nf.engine, nf.body], [scoped.status, scoped.engine], asked.length]).toEqual([[404, 'box;dur=1', 'path not found'], [200, 'worker;box=skip'], 1])
  })
})

describe('withProvenance', () => {
  it('puts `pv` on named nodes (not folds) whose top owner has one, before `c`', () => {
    const tree = { n: 'root', k: 'dir', b: 9, o: 3, us: [['al', 5], ['bo', 4]], m: 1, c: [
      { n: 'a', k: 'dir', b: 5, o: 2, us: [['al', 5]], c: [{ n: 'x', k: 'file', b: 5, o: 1, us: [['al', 5]] }] },
      { n: '(other)', k: 'dir', b: 4, o: 1, us: [['bo', 4]], f: 2 },
    ] }
    const pv = (p: string, u: string) => (u === 'al' && p !== 'a/x' ? { at: p } : null)
    expect(JSON.stringify(withProvenance(tree as never, '', pv))).toEqual(JSON.stringify({ n: 'root', k: 'dir', b: 9, o: 3, us: [['al', 5], ['bo', 4]], m: 1, pv: { at: '' }, c: [
      { n: 'a', k: 'dir', b: 5, o: 2, us: [['al', 5]], pv: { at: 'a' }, c: [{ n: 'x', k: 'file', b: 5, o: 1, us: [['al', 5]] }] },
      { n: '(other)', k: 'dir', b: 4, o: 1, us: [['bo', 4]], f: 2 },
    ] }))
  })
})

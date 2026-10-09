import { beforeAll, describe, expect, it } from 'vitest'
import type { Env } from './auth'
import { openIndex, pathGens, usMap, withPathStore } from './index'
import { sqliteD1 } from './testD1'
import { fixture, readJson } from './testStore'
import { buildDiff, buildView, type ViewNode } from './view'

// The change-interval path store read as of a scan (`PATH_STORE=intervals`, specs/interval-store.md):
// `fixtures/iv/` is one generation over six synthetic scans (three v1 indexes, three v2 store
// generations; owner slices, paths that come and go, moving read days), 4 rows per group, built by
// `dt_cloud.interval_store` (`fixtures/iv/gen.py`). `expected.json` holds each view computed by the
// Python per-scan reference from that scan's own rows; the Worker's view over the store must equal it.

type Tiles = Record<string, Record<string, unknown>>
type Case = { date: string; path: string; w: number; h: number; depth: number | null; v: number; tiles: Tiles }

/** `INDEX_R2` over the fixture dir, counting what is read. */
function r2(reads: string[]): R2Bucket {
  const get = async (key: string, o?: { range?: { offset: number; length: number } }) => {
    const mod = 'node:fs' // a variable: the Workers typecheck has no Node types
    const fs = (await import(/* @vite-ignore */ mod)) as { existsSync(p: string): boolean; readFileSync(p: string): Uint8Array }
    const file = fixture(`iv/${key}`)
    if (!fs.existsSync(file)) return null
    const buf = fs.readFileSync(file)
    const { offset = 0, length = buf.byteLength - offset } = o?.range ?? {}
    reads.push(key)
    const part = buf.slice(offset, offset + length)
    return {
      size: buf.byteLength,
      arrayBuffer: async () => part.buffer.slice(part.byteOffset, part.byteOffset + part.byteLength),
      json: async () => JSON.parse(new TextDecoder().decode(part)),
    }
  }
  return { get } as unknown as R2Bucket
}

/** A node without its children as canonical JSON (`interval_read.canon`: sorted keys, compact). */
function canon(n: ViewNode): string {
  const sorted = (v: unknown): unknown =>
    Array.isArray(v) ? v.map(sorted) : v && typeof v === 'object' ? Object.fromEntries(Object.keys(v).sort().map(k => [k, sorted((v as Record<string, unknown>)[k])])) : v
  const { c: _c, ...rest } = n
  return JSON.stringify(sorted(rest))
}

/** A tree flattened as `interval_read.flatten`: keyed by names from the root, siblings by bytes then
 *  name, later duplicates `#2`…; owners by bytes then name (the Worker keeps row order on ties). */
function flatten(t: ViewNode): Tiles {
  const out: Tiles = {}
  const rec = (n: ViewNode, p: string) => {
    let k = p
    for (let i = 2; k in out; i++) k = `${p}#${i}`
    const { c, ...rest } = n
    const us = rest.us as [string, number][] | undefined
    out[k] = { ...rest, ...(us ? { us: [...us].sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : 1)) } : {}) }
    const kids = [...(c ?? [])].sort((a, b) => b.b - a.b || (a.n < b.n ? -1 : a.n > b.n ? 1 : 0) || (canon(a) < canon(b) ? -1 : canon(a) > canon(b) ? 1 : 0))
    for (const k2 of kids) rec(k2, `${p}/${k2.n}`)
  }
  rec(t, '')
  return out
}

let env: Env
let reads: string[]
let cases: Case[]

beforeAll(async () => {
  ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async () => {} } }
  const { db } = await sqliteD1('cw')
  reads = []
  env = { DB: db, ROOT_LABEL: 'all buckets', PATH_STORE: 'intervals', INTERVAL_STORE_GEN: 'g1', INDEX_R2: r2(reads) } as Env
  cases = await readJson<Case[]>('iv/expected.json')
})

describe('interval store', () => {
  it('opens a held date as of its scan, and nothing else', async () => {
    const h = await openIndex(env, '2026-08-03', 'bysize')
    expect([h.mode, h.version, h.variant, h.asOf, h.gen]).toEqual(['pq', 3, 'bysize', 1785715200, 'iv:g1'])
    await expect(openIndex(env, '2026-08-01', 'path')).rejects.toThrow("index variant 'path' not synced for 2026-08-01")
    await expect(openIndex(env, '2026-08-03', 'bysize-user')).rejects.toThrow("index variant 'bysize-user' not synced for 2026-08-03")
  })

  it('opts a request in with `ps=iv` under `PATH_STORE=opt-in`, never for a secondary store', () => {
    const at = (q: string, mode = 'opt-in') => withPathStore({ request: new Request(`https://x/api/subtree?${q}`), env: { ...env, PATH_STORE: mode } }).env.PATH_STORE
    expect([at('date=d'), at('date=d&ps=iv'), at('date=d&ps=iv&store=meta'), at('date=d', 'intervals'), at('date=d&store=meta', 'intervals')])
      .toEqual([undefined, 'intervals', undefined, 'intervals', undefined])
  })

  it('keys a held date\'s answers to the interval generation', async () => {
    const off = { ...env, PATH_STORE: undefined }
    const [held, heldOff, other, otherOff] = await Promise.all([pathGens(env, ['2026-08-03']), pathGens(off, ['2026-08-03']), pathGens(env, ['2026-08-01']), pathGens(off, ['2026-08-01'])])
    expect([held === heldOff, other === otherOff]).toEqual([false, true])
  })

  it('decodes owner slices', () => {
    expect([usMap('', 5), usMap('alice', 5), usMap('[["alice",0],["bob",10]]', 10)]).toEqual([null, { alice: 5 }, { alice: 0, bob: 10 }])
  })

  it('serves every view of every scan as the per-scan reference draws it', async () => {
    expect(cases.length).toBe(84)
    for (const c of cases) {
      const v = await buildView(env, { date: c.date, path: c.path, w: c.w, h: c.h, minArea: 12, atten: 2, ...(c.depth != null ? { maxDepth: c.depth } : {}) })
      const got = flatten(v.tree)
      // A v1 scan never knew its directories' children: the reader's `(other)` count is what its read saw.
      const want = Object.fromEntries(Object.entries(c.tiles).map(([k, n]) => [k, n.f === null ? Object.fromEntries(Object.entries(n).filter(([f]) => f !== 'f')) : n]))
      if (c.v === 1) for (const n of Object.values(got)) delete n.f
      expect({ case: [c.date, c.path, c.w, c.h, c.depth], tiles: got }).toEqual({ case: [c.date, c.path, c.w, c.h, c.depth], tiles: want })
      expect([v.index, ['bysize', 'path'].includes(v.tier)]).toEqual(['iv:g1', true])
    }
  })

  it('reads only the interval store, read days from the path versions themselves', async () => {
    expect([...new Set(reads)].sort()).toEqual([
      'interval-store/g1/scans.json',
      'interval-store/g1/served/bysize.groups.parquet',
      'interval-store/g1/served/bysize.parquet',
      'interval-store/g1/served/path.groups.parquet',
      'interval-store/g1/served/path.parquet',
    ])
  })

  it('diffs two scans from the store', async () => {
    const d = await buildDiff(env, { from: '2026-08-04', to: '2026-08-05', path: 'b2/e', w: 8, h: 6, minArea: 12, atten: 2, top: 100 })
    const a = cases.find(c => c.date === '2026-08-04' && c.path === 'b2/e' && c.w === 8 && c.depth == null)!
    const b = cases.find(c => c.date === '2026-08-05' && c.path === 'b2/e' && c.w === 8 && c.depth == null)!
    expect([d.total_a, d.total_b]).toEqual([a.tiles[''].b, b.tiles[''].b])
  })
})

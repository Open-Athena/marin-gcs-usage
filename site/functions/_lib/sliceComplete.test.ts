import { beforeAll, describe, expect, it, vi } from 'vitest'
import type { Env } from './auth'
import { openIndex, readRects, readSizeRects, type Rect, type Row } from './index'
import { sqliteD1 } from './testD1'
import { type D1Variant, readJson, seedGeneration } from './testStore'
import { buildView, type ViewNode } from './view'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('./testStore')).S3Store }))

// The `bysize` read over owner slices (specs/interval-store.md §7), on
// `fixtures/v2-slices/` (`gen.py`): bucket `bk` with
//   m/big    alice 2 MiB + bob 100 KiB      (drawn by alice's slice, short of bob's)
//   m/split  alice, bob, unowned 300 KiB    (every slice under 512 KiB, total over: not drawn)
//   m/deep   alice, bob 300 KiB, and its m/deep/x the same, a level down
//   m/twin   alice: an object (50 KiB) and a dir (600 KiB) at one path
//   s/       carol alone (4 × 200 KiB)
//   m/f0000…m/f2499  unowned 1 KiB objects: `m`'s children band spans both 2048-row groups.

const DATE = '2026-10-09'
const KiB = 1024
let env: Env

beforeAll(async () => {
  ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async () => {} } }
  const { db, raw } = await sqliteD1('cw')
  const variants = await readJson<Record<string, D1Variant>>('v2-slices/d1.json')
  const files = {
    path: { parquet: 'v2-slices/path-index.parquet', groups: 'v2-slices/path-index.groups.json' },
    bysize: { parquet: 'v2-slices/path-index-bysize.parquet', groups: 'v2-slices/path-index-bysize.groups.json' },
  }
  seedGeneration(raw, { date: DATE, gen: 'g', dir: `listing/${DATE}/index/g`, variants, files })
  env = { DB: db, ROOT_LABEL: 'root', BASE_SCOPE: 'gcs', GCS_HMAC_KEY_ID: 'k', GCS_HMAC_SECRET: 's' } as Env
})

const subtree = (P: string): Rect => (P === '' ? { dLo: 1, dHi: 1e9, pLo: '', pHi: '￿' } : { dLo: P.split('/').length + 1, dHi: 1e9, pLo: `${P}/`, pHi: `${P}0` })
const order = (rows: Row[]): Row[] => [...rows].sort((a, b) =>
  a.depth - b.depth || (a.path < b.path ? -1 : a.path > b.path ? 1 : 0) || String(a.usr).localeCompare(String(b.usr)) || a.kind.localeCompare(b.kind))

/** Brute force: every row of the `path` sort in the rect, kept per path when the path's
 *  total (over all its slices) clears `thrAt(depth)`. */
async function brute(q: Rect, thrAt: (d: number) => number): Promise<Row[]> {
  const all = await readRects(await openIndex(env, DATE, 'path'), [{ ...q, dLo: 1, dHi: 1e9, pLo: '', pHi: '￿' }])
  const inRect = all.filter(r => r.depth >= q.dLo && r.depth <= q.dHi && r.path >= q.pLo && r.path < q.pHi)
  const tot = new Map<string, number>()
  for (const r of inRect) tot.set(`${r.depth}\0${r.path}`, (tot.get(`${r.depth}\0${r.path}`) ?? 0) + r.size)
  return order(inRect.filter(r => tot.get(`${r.depth}\0${r.path}`)! >= thrAt(r.depth)))
}

/** Per path: total bytes and the bytes per owner (`us`). */
const perPath = (rows: Row[]) => {
  const m = new Map<string, { b: number; us: Record<string, number> }>()
  for (const r of rows) {
    const a = m.get(r.path) ?? { b: 0, us: {} }
    a.b += r.size
    const u = r.usr ?? '(unowned)'
    a.us[u] = (a.us[u] ?? 0) + r.size
    m.set(r.path, a)
  }
  return Object.fromEntries([...m].sort(([a], [b]) => (a < b ? -1 : 1)))
}

describe('bysize over owner slices', () => {
  const read = async (q: Rect, thrAt: (d: number) => number, complete = true) =>
    order(await readSizeRects(await openIndex(env, DATE, 'bysize'), [q], thrAt, undefined, undefined, undefined, complete ? await openIndex(env, DATE, 'path') : undefined))

  it('per slice (the bug): a drawn dir is short of its small slices, and an all-small-slices dir is missing', async () => {
    const thr = () => 512 * KiB
    expect(perPath(await read(subtree('bk/m'), thr, false))).toEqual({
      'bk/m/big': { b: 2048 * KiB, us: { alice: 2048 * KiB } },
      'bk/m/big/a': { b: 2048 * KiB, us: { alice: 2048 * KiB } },
      'bk/m/big/a/f0': { b: 2048 * KiB, us: { alice: 2048 * KiB } },
      'bk/m/twin': { b: 600 * KiB, us: { alice: 600 * KiB } },
      'bk/m/twin/f0': { b: 600 * KiB, us: { alice: 600 * KiB } },
    })
  })

  it('completed: every slice of every path whose total clears the threshold, nothing else', async () => {
    const thr = () => 512 * KiB
    const got = await read(subtree('bk/m'), thr)
    expect(got).toEqual(await brute(subtree('bk/m'), thr))
    expect(perPath(got)).toEqual({
      'bk/m/big': { b: 2148 * KiB, us: { alice: 2048 * KiB, bob: 100 * KiB, '(unowned)': 0 } },
      'bk/m/big/a': { b: 2048 * KiB, us: { alice: 2048 * KiB } },
      'bk/m/big/a/f0': { b: 2048 * KiB, us: { alice: 2048 * KiB } },
      'bk/m/deep': { b: 600 * KiB, us: { alice: 300 * KiB, bob: 300 * KiB, '(unowned)': 0 } },
      'bk/m/deep/x': { b: 600 * KiB, us: { alice: 300 * KiB, bob: 300 * KiB, '(unowned)': 0 } },
      'bk/m/split': { b: 900 * KiB, us: { alice: 300 * KiB, bob: 300 * KiB, '(unowned)': 300 * KiB } },
      'bk/m/twin': { b: 650 * KiB, us: { alice: 650 * KiB } },
      'bk/m/twin/f0': { b: 600 * KiB, us: { alice: 600 * KiB } },
    })
  })

  it('equals brute force over roots, thresholds, attenuations and depth caps', async () => {
    const cases: [string, number, number, number][] = []
    for (const P of ['', 'bk', 'bk/m', 'bk/m/deep', 'bk/s']) {
      for (const t of [1, 100 * KiB, 301 * KiB, 512 * KiB, 640 * KiB, 2 * 1024 * KiB]) {
        for (const atten of [0.5, 1, 2]) for (const levels of [1, 2, 1e9]) cases.push([P, t, atten, levels])
      }
    }
    const bad: unknown[] = []
    for (const [P, t, atten, levels] of cases) {
      const q = subtree(P)
      const dP = q.dLo - 1
      const thrAt = (d: number) => t * atten ** Math.max(0, d - dP - 1)
      const rect = { ...q, dHi: levels < 1e9 ? dP + levels : 1e9 }
      const [got, want] = [await read(rect, thrAt), await brute(rect, thrAt)]
      if (JSON.stringify(got) !== JSON.stringify(want)) bad.push([P, t, atten, levels, perPath(got), perPath(want)])
    }
    expect([cases.length, bad]).toEqual([270, []])
  })

  it('the view: tile bytes and owner breakdowns are the paths\' totals', async () => {
    const view = await buildView(env, { date: DATE, path: 'bk/m', w: 64, h: 64, minArea: 12, atten: 1, threshold: 512 * KiB })
    const tiles: Record<string, number> = {}
    const walk = (n: ViewNode, at: string) => {
      for (const c of n.c ?? []) {
        const p = `${at}/${c.n}`
        if (!c.n.startsWith('(')) tiles[p] = c.b
        walk(c, p)
      }
    }
    walk(view.tree, 'bk/m')
    expect([view.tier, tiles]).toEqual(['bysize', { 'bk/m/big': 2148 * KiB, 'bk/m/big/a': 2048 * KiB, 'bk/m/big/a/f0': 2048 * KiB, 'bk/m/deep': 600 * KiB, 'bk/m/deep/x': 600 * KiB, 'bk/m/split': 900 * KiB, 'bk/m/twin': 650 * KiB, 'bk/m/twin/f0': 600 * KiB }])
  })
})

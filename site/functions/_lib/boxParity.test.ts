import { beforeAll, describe, expect, it, vi } from 'vitest'
import type { Env } from './auth'
import { parseQuery as parseWith } from './scope'
import { makeSimple } from './querySyntax'
import { sqliteD1 } from './testD1'
import { type D1Variant, fixture, FILES, readJson, seedGeneration } from './testStore'
import { SEARCH_FILES, searchKey } from './search'
import { buildDiff, buildView, readRootAgg, readRootRows } from './view'
import { rootPoints, type RootRow } from './series'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('./testStore')).S3Store }))

// The Worker's `/api/subtree`, `/api/diff` and `/api/series` bodies over the
// `v2-search` fixture (search sidecars on, so every search completes), for
// the serving box's parity tests: `cloud/tests/test_box.py` (`mem`) and
// `cloud/tests/test_chserve.py` (ClickHouse) answer the same cases with
// `dt-cloud serve-query`'s code and compare, field by field
// (specs/filter-query-service.md §6.3, specs/ch-store.md §4). Scans A and B
// are the same generation; C is `v2-search-b`, the diffs with changes'
// far side. The bodies are composed as `api/subtree.ts` / `api/diff.ts` /
// `api/series.ts` compose them. Regenerate with
// `vitest run functions/_lib/boxParity.test.ts -u`.

const A = '2026-10-01T0001'
const B = '2026-10-01T0003'
const C = '2026-10-02T0001'
const dirOf = (date: string) => `cw-l2/${date}/index/g`
const files = { path: { parquet: 'v2-search/path-index.parquet', groups: 'v2-search/path-index.groups.json' }, bysize: { parquet: 'v2-search/path-index-bysize.parquet', groups: 'v2-search/path-index-bysize.groups.json' } }
const LAX = makeSimple({ minTerm: 1 })
let env: Env

beforeAll(async () => {
  ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async () => {} } }
  const { db, raw } = await sqliteD1('cw')
  const v = await readJson<Record<string, D1Variant>>('v2-search/d1.json')
  for (const date of [A, B]) {
    seedGeneration(raw, { date, gen: 'g', dir: dirOf(date), variants: v, files })
    for (const role of ['rows', 'trigrams', 'rowsSearch'] as const) FILES.set(searchKey(dirOf(date), role), fixture(`v2-search/${SEARCH_FILES[role]}`))
  }
  const vC = await readJson<Record<string, D1Variant>>('v2-search-b/d1.json')
  const filesC = Object.fromEntries(Object.entries(files).map(([k, f]) => [k, { parquet: f.parquet.replace('v2-search/', 'v2-search-b/'), groups: f.groups.replace('v2-search/', 'v2-search-b/') }]))
  seedGeneration(raw, { date: C, gen: 'g', dir: dirOf(C), variants: vC, files: filesC })
  for (const role of ['rows', 'trigrams', 'rowsSearch'] as const) FILES.set(searchKey(dirOf(C), role), fixture(`v2-search-b/${SEARCH_FILES[role]}`))
  env = { DB: db, ROOT_LABEL: 'root', GCS_HMAC_KEY_ID: 'k', GCS_HMAC_SECRET: 's' } as Env
})

/** One case: a query on a view, with the canvas params that matter. */
interface Case { q: string; path: string; w?: number; h?: number; minArea?: number; atten?: number; depth?: number }

export const SUBTREE: Case[] = [
  ...['ttl', 'ckpt', 'safetensors', 'ttl -14d', 'models -llama', 'grug swarm', 'tmp/*/ckpt', 'ttl|safetensors', 'key', 'run-a/', 'f00*1']
    .flatMap(q => ['', 'bk', 'bk/tmp'].map(path => ({ q, path }))),
  // NOT-only (the view itself matches); at `bk` it would draw all 6000 fillers.
  { q: '-ttl', path: '' }, { q: '-ttl', path: 'bk/tmp' }, { q: '-fill', path: 'bk' },
  { q: 'f000', path: 'bk', minArea: 4000 },
  { q: 'ttl', path: '', depth: 1 },
  { q: 'ttl', path: 'bk', atten: 3, w: 256, h: 256 },
]
export const DIFF: Case[] = [{ q: 'ttl', path: '' }, { q: 'ckpt -final', path: 'bk' }, { q: 'safetensors', path: 'bk/models', depth: 1 }]
/** Unfiltered subtrees (on A). */
export const PLAIN: Omit<Case, 'q'>[] = [
  ...['', 'bk', 'bk/tmp', 'bk/models/llama', 'zz', 'bk/fill/f00011'].map(path => ({ path })),
  { path: 'bk/fill', minArea: 500 }, { path: '', minArea: 4000 }, { path: 'bk', w: 256, h: 256 }, { path: 'bk', atten: 3, minArea: 200 }, { path: '', depth: 1 }, { path: 'bk', depth: 2 },
]
/** Diffs with changes, A → C: plain (`q` absent) and filtered. */
interface DiffCase extends Partial<Case> { path: string; top?: number; summary?: boolean }
export const DIFF_C: DiffCase[] = [
  { path: '' }, { path: 'bk' }, { path: 'bk/tmp' }, { path: '', depth: 1 }, { path: 'bk', summary: true }, { path: 'bk', minArea: 1, top: 3 }, { path: 'yy' },
  { path: '', q: 'ttl' }, { path: 'bk', q: 'ckpt' }, { path: 'bk', q: 'safetensors' }, { path: 'bk', q: 'swarm -final' }, { path: '', q: 'ttl', depth: 1 },
]
/** Series over A, B, C (the scans the index knows). */
interface SeriesCase { path: string; paths?: string[]; split?: boolean }
export const SERIES: SeriesCase[] = [
  { path: '' }, { path: 'bk' }, { path: 'bk/tmp/ttl=7d' }, { path: 'yy/a' }, { path: 'nope' }, { path: '', paths: ['bk/tmp', 'yy/a', 'bk/iris/notes.txt'] }, { path: '', split: true },
]

const P = (c: Case) => ({ w: c.w ?? 1280, h: c.h ?? 896, minArea: c.minArea ?? 12, atten: c.atten ?? 2 })

describe('the Worker’s filtered bodies, for the box’s parity test', () => {
  it('match the committed golden file', async () => {
    const subtree = []
    for (const c of SUBTREE) {
      const { w, h, minArea, atten } = P(c)
      const view = await buildView(env, { date: A, path: c.path, w, h, minArea, atten, query: parseWith(c.q, LAX)!, maxDepth: c.depth })
      subtree.push({
        case: c,
        body: {
          date: A, path: c.path, w, h, minArea, atten, tier: view.tier, index: view.index, threshold: Math.round(view.threshold), nodes: view.nodes, truncated: view.truncated,
          q: c.q, matches: view.matches, matched: view.matched ?? [], ...(view.excluded ? { excluded: view.excluded } : {}),
          partial: view.partial, partialReason: view.partialReason, approximate: view.approximate, approximateReason: view.approximateReason, tree: view.tree,
        },
      })
    }
    const diff = []
    for (const c of DIFF) {
      const { w, h, minArea, atten } = P(c)
      const d = await buildDiff(env, { from: A, to: B, path: c.path, w, h, minArea, atten, top: 500, query: parseWith(c.q, LAX)!, depth: c.depth })
      diff.push({ case: c, body: { prev: A, curr: B, path: c.path, q: c.q, ...d, threshold: Math.round(d.threshold) } })
    }
    const plain = []
    for (const c of PLAIN) {
      const { w, h, minArea, atten } = P({ ...c, q: '' })
      const view = await buildView(env, { date: A, path: c.path, w, h, minArea, atten, maxDepth: c.depth })
      plain.push({ case: c, body: { date: A, path: c.path, w, h, minArea, atten, tier: view.tier, index: view.index, threshold: Math.round(view.threshold), nodes: view.nodes, truncated: view.truncated, tree: view.tree } })
    }
    const diffC = []
    for (const c of DIFF_C) {
      const { w, h, minArea, atten } = P({ q: '', ...c })
      const top = c.top ?? 500
      const d = await buildDiff(env, { from: A, to: C, path: c.path, w, h, minArea, atten, top, ...(c.q ? { query: parseWith(c.q, LAX)! } : {}), depth: c.depth, summary: c.summary })
      diffC.push({ case: c, body: { prev: A, curr: C, path: c.path, ...(c.q ? { q: c.q } : {}), ...d, threshold: Math.round(d.threshold) } })
    }
    const series = []
    for (const c of SERIES) {
      const points: { date: string; b: number; o: number }[] = []
      const byDate = new Map<string, RootRow[]>()
      for (const date of [A, B, C]) {
        if (c.split) {
          const rows = await readRootRows(env, date)
          if (!rows) continue
          byDate.set(date, rows)
          points.push({ date, b: rows.reduce((n, r) => n + r.b, 0), o: rows.reduce((n, r) => n + r.o, 0) })
        } else if (c.paths) {
          const parts = await Promise.all(c.paths.map(p => readRootAgg(env, { date, path: p })))
          if (parts.some(Boolean)) points.push({ date, b: parts.reduce((n, a) => n + (a?.b ?? 0), 0), o: parts.reduce((n, a) => n + (a?.o ?? 0), 0) })
        } else {
          const a = await readRootAgg(env, { date, path: c.path })
          if (a) points.push({ date, ...a })
        }
      }
      series.push({ case: c, body: { path: c.path, ...(c.paths ? { paths: c.paths } : {}), points, ...(c.split ? { roots: rootPoints(byDate) } : {}) } })
    }
    await expect(JSON.stringify({ subtree, diff, plain, diffC, series }, null, 1) + '\n').toMatchFileSnapshot('./fixtures/v2-search/box-parity.json')
  })
})

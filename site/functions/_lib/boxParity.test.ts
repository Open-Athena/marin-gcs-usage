import { beforeAll, describe, expect, it, vi } from 'vitest'
import type { Env } from './auth'
import { parseQuery as parseWith } from './scope'
import { makeSimple } from './querySyntax'
import { sqliteD1 } from './testD1'
import { type D1Variant, fixture, FILES, readJson, seedGeneration } from './testStore'
import { SEARCH_FILES, searchKey } from './search'
import { buildDiff, buildView } from './view'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('./testStore')).S3Store }))

// The Worker's filtered `/api/subtree` and `/api/diff` bodies over the
// `v2-search` fixture (search sidecars on, so every search completes), for
// the serving box's parity test: `cloud/tests/test_box.py` answers the same
// cases with `dt-cloud serve-query`'s code and compares, field by field
// (specs/filter-query-service.md §6.3). The bodies are composed as
// `api/subtree.ts` / `api/diff.ts` compose them. Regenerate with
// `vitest run functions/_lib/boxParity.test.ts -u`.

const A = '2026-10-01T0001'
const B = '2026-10-01T0003'
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
    await expect(JSON.stringify({ subtree, diff }, null, 1) + '\n').toMatchFileSnapshot('./fixtures/v2-search/box-parity.json')
  })
})

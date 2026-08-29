/**
 * The floor-free path index (`listing/<date>/path-index.parquet`) as a
 * row-group-pruned range reader — shared by `/api/subtree` (pixel-budget
 * drill) and `/api/marks/totals` (exact keep / sweep bytes per live mark).
 *
 * The file is sorted (depth, path): the descendants of P at each depth are
 * one contiguous run, so any prefix query is a few binary-search-style
 * row-group selections on the footer stats plus ranged reads of just those
 * groups (specs/path-agnostic-serving.md §2.1).
 */
import { S3Store } from '@rdub/file-tree/stores/s3'
import { parquetMetadataAsync, parquetReadObjects } from 'hyparquet'
import type { Env } from './auth.js'

export const BUCKET = 'oa-gcs-usage-dvx'

export interface Row {
  path: string
  depth: number
  team: string | null
  usr: string | null
  b: number
  o: number
  wts: number
  wb: number
  c2: number
  c3: number
  c4: number
}

export interface GroupSpan {
  rowStart: number
  rowEnd: number
  dMin: number
  dMax: number
  pMin: string
  pMax: string
  bMax: number
}

export interface IndexHandle {
  file: { byteLength: number; slice: (s: number, e?: number) => Promise<ArrayBuffer> }
  metadata: Awaited<ReturnType<typeof parquetMetadataAsync>>
  groups: GroupSpan[]
}

export const num = (v: unknown): number => (typeof v === 'bigint' ? Number(v) : (v as number) ?? 0)
export const str = (v: unknown): string =>
  typeof v === 'string' ? v : v instanceof Uint8Array ? new TextDecoder().decode(v) : String(v ?? '')

export function makeStore(env: Env) {
  return S3Store({
    endpoint: 'https://storage.googleapis.com',
    bucket: BUCKET,
    region: 'us-east1',
    prefixes: ['listing/', 'snapshots/'],
    accessKeyId: env.GCS_HMAC_KEY_ID,
    secretAccessKey: env.GCS_HMAC_SECRET,
  })
}

// Per-isolate cache — the footer is ~MBs and immutable per date.
const handles = new Map<string, Promise<IndexHandle>>()

export async function openIndex(env: Env, date: string): Promise<IndexHandle> {
  const cached = handles.get(date)
  if (cached) return cached
  const p = (async () => {
    const store = makeStore(env)
    const key = `listing/${date}/path-index.parquet`
    // Object size via a 1-byte ranged read's Content-Range (S3Store populates totalSize).
    const probe = await store.get(key, { offset: 0, length: 1 })
    const byteLength = probe.totalSize
    if (!byteLength) throw new Error('index size unknown (no Content-Range)')
    const file = {
      byteLength,
      slice: async (s: number, e?: number) => {
        const r = await store.get(key, { offset: s, length: (e ?? byteLength) - s })
        return r.bytes.buffer.slice(r.bytes.byteOffset, r.bytes.byteOffset + r.bytes.byteLength) as ArrayBuffer
      },
    }
    const metadata = await parquetMetadataAsync(file)
    const cols = metadata.row_groups[0]?.columns.map(c => c.meta_data?.path_in_schema?.[0]) ?? []
    const di = cols.indexOf('depth')
    const pi = cols.indexOf('path')
    const bi = cols.indexOf('b')
    let at = 0
    const groups: GroupSpan[] = metadata.row_groups.map(g => {
      const n = num(g.num_rows)
      const ds = g.columns[di]?.meta_data?.statistics
      const ps = g.columns[pi]?.meta_data?.statistics
      const bs = g.columns[bi]?.meta_data?.statistics
      const span = {
        rowStart: at,
        rowEnd: at + n,
        dMin: num(ds?.min_value ?? 0),
        dMax: num(ds?.max_value ?? 1e9),
        pMin: str(ps?.min_value ?? ''),
        pMax: str(ps?.max_value ?? '￿'),
        bMax: bs?.max_value != null ? num(bs.max_value) : Number.MAX_SAFE_INTEGER,
      }
      at += n
      return span
    })
    return { file, metadata, groups }
  })()
  handles.set(date, p)
  p.catch(() => handles.delete(date))
  return p
}

const toRow = (r: Record<string, unknown>): Row => ({
  path: str(r.path),
  depth: num(r.depth),
  team: r.team == null ? null : str(r.team),
  usr: r.usr == null ? null : str(r.usr),
  b: num(r.b),
  o: num(r.o),
  wts: num(r.wts),
  wb: num(r.wb),
  c2: num(r.c2),
  c3: num(r.c3),
  c4: num(r.c4),
})

/** Read all rows in groups whose (depth, path) stats can intersect the ask. */
export async function readRows(
  h: IndexHandle,
  dLo: number,
  dHi: number,
  pLo: string,
  pHi: string,
  thrAt?: (depth: number) => number,
): Promise<Row[]> {
  const out: Row[] = []
  let selected = 0
  for (const g of h.groups) {
    if (g.dMax < dLo || g.dMin > dHi) continue
    // Path stats only discriminate within a single depth; a group spanning a
    // depth boundary resets path order, so only apply the path test then.
    if (g.dMin === g.dMax && (g.pMax < pLo || g.pMin > pHi)) continue
    // A group whose biggest row can't clear the (shallowest applicable)
    // threshold contributes nothing but fold-count noise — skip it.
    if (thrAt && g.bMax < thrAt(Math.max(g.dMin, dLo))) continue
    if (++selected > 80) throw new Error('query too wide: drill deeper or raise minArea')
    const rows = (await parquetReadObjects({
      file: h.file,
      metadata: h.metadata,
      rowStart: g.rowStart,
      rowEnd: g.rowEnd,
    })) as Record<string, unknown>[]
    for (const r of rows) {
      const depth = num(r.depth)
      const path = str(r.path)
      if (depth < dLo || depth > dHi || path < pLo || path > pHi) continue
      out.push(toRow(r))
    }
  }
  return out
}

/** A point lookup `(depth, path)` or a one-level range under a prefix. */
export type Ask = { depth: number; path: string } | { depth: number; under: string }

const groupMayHold = (g: GroupSpan, a: Ask): boolean => {
  if (g.dMax < a.depth || g.dMin > a.depth) return false
  if (g.dMin !== g.dMax) return true
  return 'path' in a
    ? !(g.pMax < a.path || g.pMin > a.path)
    : !(g.pMax < a.under + '/' || g.pMin > a.under + '0') // '0' sorts just past '/'
}

/**
 * Rows for a *set* of asks in one pass over the groups that can hold any of
 * them — each selected group is read once (only `columns`), and `keep`
 * decides which rows to return. Live marks cluster (siblings share groups),
 * so ~8k prefixes today touch ~25 of 3,358 groups.
 */
export async function readAsks(
  h: IndexHandle,
  asks: Ask[],
  keep: (r: Row) => boolean,
  { columns, maxGroups = 60 }: { columns?: string[]; maxGroups?: number } = {},
): Promise<{ rows: Row[]; groups: number }> {
  const out: Row[] = []
  const selected = h.groups.filter(g => asks.some(a => groupMayHold(g, a)))
  if (selected.length > maxGroups) throw new Error(`lookup too wide: ${selected.length} row groups (cap ${maxGroups})`)
  for (const g of selected) {
    const rows = (await parquetReadObjects({
      file: h.file,
      metadata: h.metadata,
      rowStart: g.rowStart,
      rowEnd: g.rowEnd,
      columns,
    })) as Record<string, unknown>[]
    for (const r of rows) {
      const row = toRow(r)
      if (keep(row)) out.push(row)
    }
  }
  return { rows: out, groups: selected.length }
}

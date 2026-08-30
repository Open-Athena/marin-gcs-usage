/**
 * The floor-free path index (`listing/<date>/path-index.parquet`) as a
 * row-group-pruned range reader — shared by `/api/subtree` (pixel-budget
 * drill) and `/api/marks/totals` (exact keep / sweep bytes per live mark).
 *
 * The file is sorted (depth, path): the descendants of P at each depth are
 * one contiguous run, so any prefix query is a few row-group selections on
 * the footer stats plus ranged reads of just those groups.
 *
 * Two ways to get the footer stats (specs/path-agnostic-serving.md §2.1):
 *   - **D1** (preferred): `index_schema` / `index_groups`, populated per scan
 *     by `gcs-usage index-sync`. Row-group selection is a SQL query and we
 *     fetch the metadata only for the groups a query actually reads, so the
 *     ~5 MB thrift footer is never parsed on a cold isolate (that parse scales
 *     with row-group count and blew the Worker CPU budget at 8k-row groups).
 *   - **Parsed footer** (fallback): `parquetMetadataAsync` when D1 has no rows
 *     for the date — back-compatible, works on any index.
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
  a: number | null
}

interface GroupSpan {
  rowStart: number
  rowEnd: number
  dMin: number
  dMax: number
  pMin: string
  pMax: string
  bMax: number
}

type FileSlice = { byteLength: number; slice: (s: number, e?: number) => Promise<ArrayBuffer> }

// D1-backed: metadata comes from index_schema/index_groups per query.
interface D1Handle {
  mode: 'd1'
  file: FileSlice
  env: Env
  date: string
  variant: string
  schema: unknown[]
  version: number
}

/** A user/team lens filter: `usr`/`team` column = `key`, applied on the
 * matching by-user/by-team index variant. */
export type Lens = { col: 'u' | 't'; key: string }
// Footer-parse fallback: the classic in-memory metadata + spans.
interface FooterHandle {
  mode: 'footer'
  file: FileSlice
  metadata: Awaited<ReturnType<typeof parquetMetadataAsync>>
  groups: GroupSpan[]
}
export type IndexHandle = D1Handle | FooterHandle

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

function fileFor(env: Env, date: string, variant: string): FileSlice {
  const store = makeStore(env)
  const key = `listing/${date}/path-index${variant === 'path' ? '' : `-by-${variant}`}.parquet`
  let size: Promise<number> | null = null
  const byteLengthP = () => (size ??= store.get(key, { offset: 0, length: 1 }).then(r => {
    if (!r.totalSize) throw new Error('index size unknown (no Content-Range)')
    return r.totalSize
  }))
  return {
    // byteLength is only read by the footer path (metadata parse); the D1 path
    // never needs it (offsets are absolute), so it stays lazy.
    get byteLength() { return 0 },
    slice: async (s: number, e?: number) => {
      const end = e ?? (await byteLengthP())
      const r = await store.get(key, { offset: s, length: end - s })
      return r.bytes.buffer.slice(r.bytes.byteOffset, r.bytes.byteOffset + r.bytes.byteLength) as ArrayBuffer
    },
  }
}

// Per-isolate cache — cheap (D1 schema row, or parsed footer) and immutable per date.
const handles = new Map<string, Promise<IndexHandle>>()

export async function openIndex(env: Env, date: string, variant = 'path'): Promise<IndexHandle> {
  const ck = `${date}:${variant}`
  const cached = handles.get(ck)
  if (cached) return cached
  const p = (async (): Promise<IndexHandle> => {
    // Prefer D1 (no footer parse). Only the schema row is fetched here.
    if (env.DB) {
      const s = await env.DB.prepare('SELECT version, schema_json FROM index_schema WHERE date = ? AND variant = ?').bind(date, variant).first<{ version: number; schema_json: string }>()
      if (s) return { mode: 'd1', file: fileFor(env, date, variant), env, date, variant, schema: JSON.parse(s.schema_json), version: s.version }
    }
    // Only the default 'path' variant has a parsed-footer fallback (the by-user/
    // by-team lens variants are D1-only — no footer path serves them).
    if (variant !== 'path') throw new Error(`index variant '${variant}' not synced for ${date}`)
    return openFooter(env, date)
  })()
  handles.set(ck, p)
  p.catch(() => handles.delete(ck))
  return p
}

async function openFooter(env: Env, date: string): Promise<FooterHandle> {
  const store = makeStore(env)
  const key = `listing/${date}/path-index.parquet`
  const probe = await store.get(key, { offset: 0, length: 1 })
  const byteLength = probe.totalSize
  if (!byteLength) throw new Error('index size unknown (no Content-Range)')
  const file: FileSlice = {
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
  return { mode: 'footer', file, metadata, groups }
}

// --- shared row shaping ------------------------------------------------------

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
  a: r.a == null ? null : num(r.a),
})

// --- D1 metadata: revive stored RowGroup JSON into hyparquet's shape ---------

const bi = (v: unknown): bigint | undefined => (v == null ? undefined : BigInt(v as string))
function reviveRowGroup(json: string): Record<string, unknown> {
  const g = JSON.parse(json) as { columns: { file_offset: string; meta_data: Record<string, unknown> }[]; total_byte_size: string; num_rows: string; file_offset?: string }
  return {
    num_rows: bi(g.num_rows),
    total_byte_size: bi(g.total_byte_size),
    ...(g.file_offset != null ? { file_offset: bi(g.file_offset) } : {}),
    columns: g.columns.map(c => {
      const m = c.meta_data
      return {
        file_offset: bi(c.file_offset),
        meta_data: {
          ...m,
          num_values: bi(m.num_values),
          total_uncompressed_size: bi(m.total_uncompressed_size),
          total_compressed_size: bi(m.total_compressed_size),
          data_page_offset: bi(m.data_page_offset),
          ...(m.dictionary_page_offset != null ? { dictionary_page_offset: bi(m.dictionary_page_offset) } : {}),
        },
      }
    }),
  }
}

/** Read one row group (given its stored metadata JSON) via a subset FileMetaData. */
async function readGroup(h: D1Handle, rgJson: string, columns?: string[]): Promise<Row[]> {
  const rg = reviveRowGroup(rgJson)
  const metadata = { version: h.version, schema: h.schema, num_rows: rg.num_rows, row_groups: [rg], metadata_length: 0 } as unknown as Awaited<ReturnType<typeof parquetMetadataAsync>>
  const rows = (await parquetReadObjects({ file: h.file, metadata, columns })) as Record<string, unknown>[]
  return rows.map(toRow)
}

interface Span extends GroupSpan { rg: number }

/** Candidate row groups for a set of (depth, path-range) rectangles — one SQL
 * pass (no rg_json yet), carrying each group's stats so the caller can apply a
 * finer per-ask test. A group spanning a depth boundary resets path order, so
 * the path test only applies within a single depth (`d_min = d_max`). */
async function selectSpans(h: D1Handle, rects: { dLo: number; dHi: number; pLo: string; pHi: string }[], cap = 4000, bMin = 0, lens?: Lens): Promise<Span[]> {
  const where: string[] = []
  const binds: unknown[] = []
  // The (depth, path) rect; valid within a single primary-key group only
  // (single-depth for the path index, single-user/team for a lens index).
  const rectSql = '(d_max >= ? AND d_min <= ? AND (d_min <> d_max OR (p_max >= ? AND p_min <= ?)))'
  for (const r of rects) {
    if (lens) {
      // Prune to groups whose usr/team range covers the lens key; the rect is a
      // secondary test that only holds inside a single-key group.
      const c = lens.col
      where.push(`(${c}_min <= ? AND ${c}_max >= ? AND (${c}_min <> ${c}_max OR ${rectSql}))`)
      binds.push(lens.key, lens.key, r.dLo, r.dHi, r.pLo, r.pHi)
    } else {
      where.push(rectSql)
      binds.push(r.dLo, r.dHi, r.pLo, r.pHi)
    }
  }
  // Prune groups whose biggest row can't clear the shallowest threshold — the
  // footer path prunes by b_max during its scan, so without this a large
  // subtree returns far more candidate groups than it can draw and hits `cap`.
  const bFloor = bMin > 0 ? ' AND b_max >= ?' : ''
  const sql = `SELECT rg, d_min, d_max, p_min, p_max, b_max, row_start, row_end FROM index_groups WHERE date = ? AND variant = ? AND (${where.join(' OR ')})${bFloor} ORDER BY rg LIMIT ${cap + 1}`
  if (bMin > 0) binds.push(Math.floor(bMin))
  const res = await h.env.DB!.prepare(sql).bind(h.date, h.variant, ...binds).all<{ rg: number; d_min: number; d_max: number; p_min: string; p_max: string; b_max: number; row_start: number; row_end: number }>()
  if (res.results.length > cap) throw new Error(`query too wide: >${cap} row groups (drill deeper or raise minArea)`)
  return res.results.map(r => ({ rg: r.rg, dMin: num(r.d_min), dMax: num(r.d_max), pMin: r.p_min, pMax: r.p_max, bMax: num(r.b_max), rowStart: num(r.row_start), rowEnd: num(r.row_end) }))
}

/** Fetch stored metadata JSON for a set of row groups. */
async function fetchGroupJson(h: D1Handle, rgs: number[]): Promise<Map<number, string>> {
  const out = new Map<number, string>()
  for (let i = 0; i < rgs.length; i += 80) {
    const chunk = rgs.slice(i, i + 80)
    const sql = `SELECT rg, rg_json FROM index_groups WHERE date = ? AND variant = ? AND rg IN (${chunk.map(() => '?').join(',')})`
    const res = await h.env.DB!.prepare(sql).bind(h.date, h.variant, ...chunk).all<{ rg: number; rg_json: string }>()
    for (const r of res.results) out.set(r.rg, r.rg_json)
  }
  return out
}

// --- public read API ---------------------------------------------------------

/** Rows in the (depth, path) rectangle; `thrAt(depth)` prunes groups whose
 * biggest row can't clear the threshold. Same contract in both handle modes. */
export async function readRows(
  h: IndexHandle,
  dLo: number,
  dHi: number,
  pLo: string,
  pHi: string,
  thrAt?: (depth: number) => number,
  lens?: Lens,
): Promise<Row[]> {
  // A row passes the lens iff its usr/team equals the key.
  const lensOk = (r: Row) => !lens || (lens.col === 'u' ? r.usr === lens.key : r.team === lens.key)
  if (h.mode === 'd1') {
    const spans = await selectSpans(h, [{ dLo, dHi, pLo, pHi }], 4000, thrAt ? thrAt(dLo) : 0, lens)
    const kept = thrAt ? spans.filter(s => s.bMax >= thrAt(Math.max(s.dMin, dLo))) : spans
    if (kept.length > 250) throw new Error('query too wide: drill deeper or raise minArea')
    // Bound the decode too, not just the group count — a broad lens (a big
    // team spread across the estate) can select few-enough groups but still
    // decode millions of rows and blow the Worker CPU. Error cleanly instead.
    const totalRows = kept.reduce((n, s) => n + (s.rowEnd - s.rowStart), 0)
    if (totalRows > 700_000) throw new Error('query too wide: drill deeper or raise minArea')
    const jsons = await fetchGroupJson(h, kept.map(s => s.rg))
    const out: Row[] = []
    for (const s of kept) {
      const j = jsons.get(s.rg)
      if (!j) continue
      for (const r of await readGroup(h, j)) {
        if (r.depth < dLo || r.depth > dHi || r.path < pLo || r.path > pHi || !lensOk(r)) continue
        out.push(r)
      }
    }
    return out
  }
  // footer mode (path variant only)
  const out: Row[] = []
  let selected = 0
  for (const g of h.groups) {
    if (g.dMax < dLo || g.dMin > dHi) continue
    if (g.dMin === g.dMax && (g.pMax < pLo || g.pMin > pHi)) continue
    if (thrAt && g.bMax < thrAt(Math.max(g.dMin, dLo))) continue
    if (++selected > 250) throw new Error('query too wide: drill deeper or raise minArea')
    const rows = (await parquetReadObjects({ file: h.file, metadata: h.metadata, rowStart: g.rowStart, rowEnd: g.rowEnd })) as Record<string, unknown>[]
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

const askRect = (a: Ask) =>
  'path' in a
    ? { dLo: a.depth, dHi: a.depth, pLo: a.path, pHi: a.path }
    : { dLo: a.depth, dHi: a.depth, pLo: a.under + '/', pHi: a.under + '0' } // '0' sorts just past '/'

const groupMayHold = (g: GroupSpan, a: Ask): boolean => {
  if (g.dMax < a.depth || g.dMin > a.depth) return false
  if (g.dMin !== g.dMax) return true
  return 'path' in a ? !(g.pMax < a.path || g.pMin > a.path) : !(g.pMax < a.under + '/' || g.pMin > a.under + '0')
}

/**
 * Rows for a *set* of asks, `keep`-filtered. Live marks cluster (siblings share
 * groups), so ~8k prefixes touch a couple dozen groups. One SQL pass selects
 * the candidate groups (D1 mode) or a single scan of the spans (footer mode);
 * each candidate group is read once (only `columns`).
 */
export async function readAsks(
  h: IndexHandle,
  asks: Ask[],
  keep: (r: Row) => boolean,
  { columns, maxGroups = 60 }: { columns?: string[]; maxGroups?: number } = {},
): Promise<{ rows: Row[]; groups: number }> {
  const out: Row[] = []
  if (h.mode === 'd1') {
    // Collapse asks to one rectangle per depth (min..max path) to keep the SQL
    // small; the exact ask set is enforced by `keep` after the read.
    const byDepth = new Map<number, { pLo: string; pHi: string }>()
    for (const a of asks) {
      const r = askRect(a)
      const cur = byDepth.get(a.depth)
      byDepth.set(a.depth, cur ? { pLo: cur.pLo < r.pLo ? cur.pLo : r.pLo, pHi: cur.pHi > r.pHi ? cur.pHi : r.pHi } : { pLo: r.pLo, pHi: r.pHi })
    }
    const rects = [...byDepth.entries()].map(([d, r]) => ({ dLo: d, dHi: d, pLo: r.pLo, pHi: r.pHi }))
    // A per-depth [min,max] rectangle over-selects the groups between the
    // lowest and highest ask; narrow to groups an actual ask falls in.
    const cand = await selectSpans(h, rects)
    const spans = cand.filter(s => asks.some(a => groupMayHold(s, a)))
    if (spans.length > maxGroups) throw new Error(`lookup too wide: ${spans.length} row groups (cap ${maxGroups})`)
    const jsons = await fetchGroupJson(h, spans.map(s => s.rg))
    for (const s of spans) {
      const j = jsons.get(s.rg)
      if (!j) continue
      for (const r of await readGroup(h, j, columns)) if (keep(r)) out.push(r)
    }
    return { rows: out, groups: spans.length }
  }
  // footer mode
  const selected = h.groups.filter(g => asks.some(a => groupMayHold(g, a)))
  if (selected.length > maxGroups) throw new Error(`lookup too wide: ${selected.length} row groups (cap ${maxGroups})`)
  for (const g of selected) {
    const rows = (await parquetReadObjects({ file: h.file, metadata: h.metadata, rowStart: g.rowStart, rowEnd: g.rowEnd, columns })) as Record<string, unknown>[]
    for (const r of rows) {
      const row = toRow(r)
      if (keep(row)) out.push(row)
    }
  }
  return { rows: out, groups: selected.length }
}

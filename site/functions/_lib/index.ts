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
 *     fetch the (compact, ~250 B) metadata only for the groups a query
 *     actually reads, so the ~5 MB thrift footer is never parsed on a cold
 *     isolate (that parse scales with row-group count and blew the Worker CPU
 *     budget at 8k-row groups).
 *   - **Parsed footer** (fallback): `parquetMetadataAsync` when D1 has no rows
 *     for the date — back-compatible, works on any index.
 */
import { S3Store } from '@rdub/file-tree/stores/s3'
import { parquetMetadataAsync, parquetReadObjects } from 'hyparquet'
import type { Env } from './auth.js'

/** A leaf of the stored parquet schema (`index_schema.schema_json`). */
interface SchemaElement { type: string; name: string; repetition_type: string; converted_type?: string }

export const BUCKET = 'oa-gcs-usage-dvx'

export interface Row {
  path: string
  depth: number
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
  schema: SchemaElement[]
  version: number
  /** A coarse tier's absolute byte floor (every path with subtree bytes >= floor
   * is present); null for the floor-free tier. */
  floor: number | null
}

/** A user lens filter: `usr` column = `key`, applied on the by-user index
 * variant (the only sort besides path — ownership has no group facet). */
export type Lens = { key: string }
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

/** Index variant → parquet key. Variants are `<tier>[-<sort>]`: tier `''`
 * (floor-free) or `coarse<E>`; sort `path` (default) or `user`. Mirrors
 * `INDEX_VARIANTS` in the gcs-usage CLI (specs/view-serving.md §1). */
export function indexKey(date: string, variant: string): string {
  const m = /^(?:(coarse\d+)(?:-(user))?|(path|user))$/.exec(variant)
  if (!m) throw new Error(`bad index variant '${variant}'`)
  const tier = m[1] ? `-${m[1]}` : ''
  const sort = m[2] ?? (m[3] === 'path' ? undefined : m[3])
  return `listing/${date}/path-index${tier}${sort ? `-by-${sort}` : ''}.parquet`
}

function fileFor(env: Env, date: string, variant: string): FileSlice {
  const store = makeStore(env)
  const key = indexKey(date, variant)
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
      const s = await env.DB.prepare('SELECT version, schema_json, floor_bytes FROM index_schema WHERE date = ? AND variant = ?').bind(date, variant).first<{ version: number; schema_json: string; floor_bytes: number | null }>()
      if (s) return { mode: 'd1', file: fileFor(env, date, variant), env, date, variant, schema: JSON.parse(s.schema_json), version: s.version, floor: s.floor_bytes == null ? null : num(s.floor_bytes) }
    }
    // Only the default 'path' variant has a parsed-footer fallback (the by-user
    // lens variant and the coarse tiers are D1-only — no footer path serves them).
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

/** The compact form `index-sync` stores (index_footer.py): `[num_rows, codec,
 * [[data_page_offset, total_compressed_size, dictionary_page_offset|0], …]]`,
 * one triple per leaf column in schema order — the only column-chunk fields
 * hyparquet reads (plus `type`/`path_in_schema`, taken from the schema). */
type CompactGroup = [number, string, [number, number, number][]]

export function reviveRowGroup(json: string, schema: SchemaElement[]): Record<string, unknown> {
  const [numRows, codec, cols] = JSON.parse(json) as CompactGroup
  const leaves = schema.slice(1) // [0] is the root element
  if (cols.length !== leaves.length) throw new Error(`row group has ${cols.length} columns, schema ${leaves.length}`)
  return {
    num_rows: BigInt(numRows),
    columns: cols.map(([dpo, size, dict], i) => ({
      meta_data: {
        type: leaves[i].type,
        path_in_schema: [leaves[i].name],
        codec,
        data_page_offset: BigInt(dpo),
        total_compressed_size: BigInt(size),
        ...(dict ? { dictionary_page_offset: BigInt(dict) } : {}),
      },
    })),
  }
}

/** Read one row group (given its stored metadata JSON) via a subset FileMetaData. */
async function readGroup(h: D1Handle, rgJson: string, columns?: string[]): Promise<Row[]> {
  const rg = reviveRowGroup(rgJson, h.schema)
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
  // (single-depth for the path index, single-user for the lens index).
  const rectSql = '(d_max >= ? AND d_min <= ? AND (d_min <> d_max OR (p_max >= ? AND p_min <= ?)))'
  for (const r of rects) {
    if (lens) {
      // Prune to groups whose usr range covers the lens key; the rect is a
      // secondary test that only holds inside a single-key group.
      where.push(`(u_min <= ? AND u_max >= ? AND (u_min <> u_max OR ${rectSql}))`)
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
  // A row passes the lens iff its usr equals the key.
  const lensOk = (r: Row) => !lens || r.usr === lens.key
  if (h.mode === 'd1') {
    const spans = await selectSpans(h, [{ dLo, dHi, pLo, pHi }], 4000, thrAt ? thrAt(dLo) : 0, lens)
    const kept = thrAt ? spans.filter(s => s.bMax >= thrAt(Math.max(s.dMin, dLo))) : spans
    if (kept.length > 250) throw new Error('query too wide: drill deeper or raise minArea')
    // Bound the decode too, not just the group count — a broad lens (a big
    // user spread across the estate) can select few-enough groups but still
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

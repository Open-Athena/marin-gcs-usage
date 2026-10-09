/** The heavy terms' drilldown (specs/architecture/static-name-search.md, "Drilldown for heavy terms"): the
 *  static catalog's members — the literals whose suffix range holds more than V rows, and every one- and
 *  two-character literal — answered at any view path P from their **match roots** (`static_roots.py`).
 *
 *  Layout, under `static-names/<gen>/drill/`: per kind (`long`, `short`) the roots files (`q, path, usr, vf,
 *  vt, size, n_files`, epoch seconds, sorted `(q, path, usr, vf)`) and the rollups (`q, dir, kind, child, vf,
 *  b, o`, sorted `(q, dir, kind, child, vf)`; kind 0 the header — `vf` children kept, `b` root rows under
 *  `dir`, `o` children holding roots — 1 a kept child's running totals, 2 the remainder's), each set with
 *  a two-level group index (`<kind>-<set>-index.parquet`, one entry per data row group: its exact first
 *  and last `(q, key)`, byte span, rows and column chunk offsets; and its `.top.parquet`, one entry per
 *  index row group); `aliases.parquet` maps every long member to the canonical member whose rows it shares
 *  (equal root sets); `meta.json` holds R, K and `dispatch_rows` (R + 2 row groups).
 *
 *  A view (`Drill.view`, exactly `static_roots.Drill.view`): P's lowercase path holding the literal → the
 *  plain view. Else the roots key range `[(c, P/), (c, P0))` (the whole member at the fleet root) is bounded
 *  from the indexes alone: ≤ `dispatch_rows` rows → read them (match roots, like the light path's first
 *  hits); more → `(c, P)` is heavy, and its rollup gives each child's totals on any date (K kept children
 *  plus an exact remainder). The fleet root has no rollup (heavy directories are depth ≥ 1): there the
 *  base catalog's per-bucket cells are the same thing, every bucket kept. */
import { parquetReadObjects, type FileMetaData, type RowGroup } from 'hyparquet'
import type { CatalogMeta, Member } from './staticCatalog.js'
import type { Found, HitCache, HitSource } from './staticFilter.js'
import { type Blobs, cmp, decodeFlat, type FlatSchema, type Hit, type IndexCache, scanAt } from './staticNames.js'
import { compressors } from './zstd.js'

export const DRILL_DIR = 'drill'

export type Kind = 'long' | 'short'
type Set_ = 'roots' | 'rollups'

export interface DrillMeta {
  gen: string
  R: number
  K: number
  rg: number
  idx_rg: number
  dispatch_rows: number
  /** Per set (`long_roots`, …): `row_groups` = the index's entries (the data files' row groups). */
  [set: string]: unknown
}

/** A rollup's cells: per kept child (kind 1) and the remainder (kind 2, `child` ''), the running totals of
 *  the roots at or under it, one cell per change (`vf` epoch ms; a child's value on D is its newest cell
 *  with `vf ≤ D`, zero if none). */
export interface RollupCell { kind: 1 | 2; child: string; vf: number; b: bigint; o: bigint }
export interface Rollup {
  /** The view path whose children these are (`''`: the fleet root, from the catalog). */
  dir: string
  /** Children kept by name. */
  kept: number
  /** Root rows under `dir`, every date (an upper bound on any date's match roots); at the fleet root a long
   *  member's alias entry, a short one's counted from the roots index (`GroupFile.count`). Null: unknown. */
  rows: number | null
  /** Children holding roots, every date. */
  children: number
  cells: RollupCell[]
}

/** A rollup on scan `date`: each kept child's totals (zeros left out, sorted by name) and the remainder's. */
export function rollupAt(r: Rollup, date: string): { kids: [string, bigint, bigint][]; rest: [bigint, bigint] } {
  const D = scanAt(date)
  const kids = new Map<string, [bigint, bigint]>()
  let rest: [bigint, bigint] = [0n, 0n]
  for (const c of r.cells) {
    if (!(c.vf <= D)) continue
    if (c.kind === 2) rest = [c.b, c.o]
    else kids.set(c.child, [c.b, c.o])
  }
  return {
    kids: [...kids].filter(([, [b, o]]) => b !== 0n || o !== 0n).sort(([x], [y]) => cmp(x, y)).map(([c, [b, o]]) => [c, b, o]),
    rest,
  }
}

/** A rollup's total (kept + remainder) on `date`. */
export function rollupTotal(r: Rollup, date: string): { b: number; o: number } {
  const { kids, rest } = rollupAt(r, date)
  let b = rest[0], o = rest[1]
  for (const [, kb, ko] of kids) { b += kb; o += ko }
  return { b: Number(b), o: Number(o) }
}

// --- decoding -------------------------------------------------------------------------------------

const ROOTS: FlatSchema = { columns: ['q', 'path', 'usr', 'vf', 'vt', 'size', 'n_files'], types: ['BYTE_ARRAY', 'BYTE_ARRAY', 'BYTE_ARRAY', 'INT64', 'INT64', 'INT64', 'INT64'] }
const ROLLUPS: FlatSchema = { columns: ['q', 'dir', 'kind', 'child', 'vf', 'b', 'o'], types: ['BYTE_ARRAY', 'BYTE_ARRAY', 'INT32', 'BYTE_ARRAY', 'INT64', 'INT64', 'INT64'] }
const SCHEMA: Record<Set_, FlatSchema> = { roots: ROOTS, rollups: ROLLUPS }
const KEY: Record<Set_, 'path' | 'dir'> = { roots: 'path', rollups: 'dir' }

/** An index file's columns as `roots index` writes them (`file` a nullable string: the concatenation re-sets it). */
const INDEX_FIELDS = [
  { name: 'file', type: 'BYTE_ARRAY', optional: true }, { name: 'rg', type: 'INT32' },
  { name: 'q_min', type: 'BYTE_ARRAY' }, { name: 'k_min', type: 'BYTE_ARRAY' }, { name: 'q_max', type: 'BYTE_ARRAY' }, { name: 'k_max', type: 'BYTE_ARRAY' },
  { name: 'offset', type: 'INT64' }, { name: 'length', type: 'INT64' }, { name: 'rows', type: 'INT32' }, { name: 'chunks', type: 'INT64', list: true },
] as const

/** Decode one row group of an index file from fetched bytes (no footer: the schema above and the top's chunk offsets). */
async function decodeIndexGroup(rows: number, chunks: number[], buf: ArrayBuffer, start: number): Promise<Entry[]> {
  const schema: Record<string, unknown>[] = [{ name: 'schema', repetition_type: 'REQUIRED', num_children: INDEX_FIELDS.length }]
  const columns: unknown[] = []
  INDEX_FIELDS.forEach((f, c) => {
    const list = 'list' in f && f.list
    if (list) schema.push({ name: f.name, repetition_type: 'REQUIRED', converted_type: 'LIST', num_children: 1 }, { name: 'list', repetition_type: 'REPEATED', num_children: 1 }, { name: 'element', type: f.type, repetition_type: 'OPTIONAL' })
    else schema.push({ name: f.name, type: f.type, repetition_type: 'optional' in f && f.optional ? 'OPTIONAL' : 'REQUIRED', ...(f.type === 'BYTE_ARRAY' ? { converted_type: 'UTF8' } : {}) })
    const dict = chunks[c * 3 + 2]
    columns.push({ meta_data: { type: f.type, path_in_schema: list ? [f.name, 'list', 'element'] : [f.name], codec: 'ZSTD', data_page_offset: BigInt(chunks[c * 3]), total_compressed_size: BigInt(chunks[c * 3 + 1]), ...(dict ? { dictionary_page_offset: BigInt(dict) } : {}) } })
  })
  const metadata = { version: 1, schema, num_rows: BigInt(rows), row_groups: [{ num_rows: BigInt(rows), columns } as unknown as RowGroup], metadata_length: 0 } as unknown as FileMetaData
  const end = start + buf.byteLength
  const file = {
    byteLength: end,
    slice(s: number, e?: number) {
      const to = e ?? end
      if (s < start || to > end) throw new Error(`static drill: index read outside the fetched span: ${s}-${to}`)
      return buf.slice(s - start, to - start)
    },
  }
  const got = await parquetReadObjects({ file, metadata, compressors }) as { file: string; rg: number; q_min: string; k_min: string; q_max: string; k_max: string; offset: bigint; length: bigint; rows: number; chunks: bigint[] }[]
  if (got.length !== rows) throw new Error(`static drill: an index row group decoded ${got.length} entries, expected ${rows}`)
  return got.map(r => ({ file: r.file, rg: r.rg, qMin: r.q_min, kMin: r.k_min, qMax: r.q_max, kMax: r.k_max, offset: num(r.offset), length: num(r.length), rows: num(r.rows), chunks: r.chunks.map(num) }))
}

const num = (v: unknown): number => { const n = Number(v); if (!Number.isSafeInteger(n)) throw new Error(`static drill: bad integer ${v}`); return n }

// --- the two-level group index ----------------------------------------------------------------

/** One data row group: its file, exact first and last `(q, key)`, byte span, rows, and column chunks. */
export interface Entry { file: string; rg: number; qMin: string; kMin: string; qMax: string; kMax: string; offset: number; length: number; rows: number; chunks: number[] }
/** A `.top.parquet`, column-wise: per index row group its first and last key, byte span, data rows, chunks. */
export interface Top { qMin: string[]; kMin: string[]; qMax: string[]; kMax: string[]; offset: number[]; length: number[]; rows: number[]; chunks: number[][] }

type Key = readonly [string, string]
const cmpKey = (a: Key, b: Key): number => cmp(a[0], b[0]) || cmp(a[1], b[1])
function bisect(n: number, pred: (i: number) => boolean): number {
  let lo = 0, hi = n
  while (lo < hi) { const m = (lo + hi) >>> 1; if (pred(m)) hi = m; else lo = m + 1 }
  return lo
}
/** Entries `[a, b)` (sorted, disjoint) whose key range meets `[lo, hi)` (`GroupFile._meet`). */
function meet(n: number, min: (i: number) => Key, max: (i: number) => Key, lo: Key, hi: Key): [number, number] {
  const a = bisect(n, i => cmpKey(max(i), lo) >= 0)
  const b = bisect(n, i => cmpKey(min(i), hi) >= 0)
  return [a, Math.max(a, b)]
}

/** What a drill read cost (the `x-static-io`/trace detail). */
export interface DrillIo { top: 'isolate' | 'cache' | 'file'; index_reads: number; index_bytes: number; groups: number; bytes: number; rows_read: number }
const newIo = (): DrillIo => ({ top: 'isolate', index_reads: 0, index_bytes: 0, groups: 0, bytes: 0, rows_read: 0 })

/** One roots or rollups set of one kind, through its two-level index (`static_roots.GroupFile`). */
export class GroupFile {
  private top?: Promise<Top>
  constructor(readonly blobs: Blobs, readonly kind: Kind, readonly set: Set_, readonly meta: () => Promise<DrillMeta>, readonly cache?: IndexCache<Top>) {}

  get indexFile(): string { return `${this.kind}-${this.set}-index.parquet` }
  get topFile(): string { return `${this.kind}-${this.set}-index.top.parquet` }

  /** The top: the isolate's, else the colo cache's, else the file read whole (≤ ~1 MB) and decoded. */
  private async loadTop(io: DrillIo): Promise<Top> {
    if (this.top) return this.top
    const p = (async () => {
      const cached = await this.cache?.get(this.topFile)
      if (cached) { io.top = 'cache'; return cached }
      io.top = 'file'
      const buf = await this.blobs.range(this.topFile, 0)
      const rows = await parquetReadObjects({ file: buf, compressors }) as { rg: number; q_min: string; k_min: string; q_max: string; k_max: string; offset: bigint; length: bigint; rows: bigint; chunks: bigint[] }[]
      rows.sort((x, y) => x.rg - y.rg)
      if (rows.some((r, i) => r.rg !== i)) throw new Error(`static drill: ${this.topFile} row groups are not 0..n`)
      const top: Top = { qMin: [], kMin: [], qMax: [], kMax: [], offset: [], length: [], rows: [], chunks: [] }
      for (const r of rows) {
        top.qMin.push(r.q_min); top.kMin.push(r.k_min); top.qMax.push(r.q_max); top.kMax.push(r.k_max)
        top.offset.push(num(r.offset)); top.length.push(num(r.length)); top.rows.push(num(r.rows)); top.chunks.push(r.chunks.map(num))
      }
      await this.cache?.put(this.topFile, top)
      return top
    })()
    this.top = p
    p.catch(() => { if (this.top === p) this.top = undefined })
    return p
  }

  /** Entries in index row group `g` (every one holds `idx_rg` but the last). */
  private async entriesIn(g: number, n: number): Promise<number> {
    const m = await this.meta()
    const total = (m[`${this.kind}_${this.set}`] as { row_groups?: number } | undefined)?.row_groups
    if (!Number.isSafeInteger(total)) throw new Error(`static drill: meta.json has no ${this.kind}_${this.set}.row_groups`)
    return g < n - 1 ? m.idx_rg : total! - m.idx_rg * (n - 1)
  }

  /** The data row groups meeting `[lo, hi)` and their rows' sum. With `cap`, when the index row groups strictly
   *  inside the range already hold more than `cap` rows, `groups` is null (the range is at least that big)
   *  and the index file is not read. */
  async select(lo: Key, hi: Key, io: DrillIo, cap?: number): Promise<{ groups: Entry[] | null; rows: number }> {
    const top = await this.loadTop(io)
    const n = top.qMin.length
    const [a, b] = meet(n, i => [top.qMin[i], top.kMin[i]], i => [top.qMax[i], top.kMax[i]], lo, hi)
    if (a === b) return { groups: [], rows: 0 }
    let inner = 0
    for (let i = a + 1; i < b - 1; i++) inner += top.rows[i]
    if (cap != null && inner > cap) return { groups: null, rows: inner }
    const start = top.offset[a], end = top.offset[b - 1] + top.length[b - 1]
    const buf = await this.blobs.range(this.indexFile, start, end - start)
    io.index_reads++
    io.index_bytes += buf.byteLength
    const entries: Entry[] = []
    for (let g = a; g < b; g++) entries.push(...await decodeIndexGroup(await this.entriesIn(g, n), top.chunks[g], buf, start))
    const [x, y] = meet(entries.length, i => [entries[i].qMin, entries[i].kMin], i => [entries[i].qMax, entries[i].kMax], lo, hi)
    const groups = entries.slice(x, y)
    return { groups, rows: groups.reduce((s, e) => s + e.rows, 0) }
  }

  /** The exact number of rows with `lo ≤ (q, key) < hi`, however many: the index row groups strictly inside
   *  the range by their top `rows`, the two at its edges read (their entries fully inside by `rows`), and the
   *  at most two data groups straddling an edge decoded. A short literal's root count at the fleet root
   *  (`.`: 361M rows) for ~2 small index reads. */
  async count(lo: Key, hi: Key, io: DrillIo): Promise<number> {
    const top = await this.loadTop(io)
    const n = top.qMin.length
    const [a, b] = meet(n, i => [top.qMin[i], top.kMin[i]], i => [top.qMax[i], top.kMax[i]], lo, hi)
    if (a === b) return 0
    let rows = 0
    for (let i = a + 1; i < b - 1; i++) rows += top.rows[i]
    const inside = (e: Entry) => cmpKey([e.qMin, e.kMin], lo) >= 0 && cmpKey([e.qMax, e.kMax], hi) < 0
    const partial: Entry[] = []
    for (const g of b - 1 > a ? [a, b - 1] : [a]) {
      const start = top.offset[g], buf = await this.blobs.range(this.indexFile, start, top.length[g])
      io.index_reads++
      io.index_bytes += buf.byteLength
      const entries = await decodeIndexGroup(await this.entriesIn(g, n), top.chunks[g], buf, start)
      const [x, y] = meet(entries.length, i => [entries[i].qMin, entries[i].kMin], i => [entries[i].qMax, entries[i].kMax], lo, hi)
      for (const e of entries.slice(x, y)) { if (inside(e)) rows += e.rows; else partial.push(e) }
    }
    if (partial.length) rows += (await this.read(lo, hi, partial, io, () => 1)).length
    return rows
  }

  /** The rows of `groups` with `lo ≤ (q, key) < hi`: one ranged GET per data file. `row` makes each one from
   *  the decoded columns (default: an object of every column). */
  async read<T = Record<string, unknown>>(lo: Key, hi: Key, groups: Entry[], io: DrillIo, row?: (cols: Record<string, unknown[]>, i: number) => T): Promise<T[]> {
    const spec = SCHEMA[this.set], key = KEY[this.set]
    const byFile = new Map<string, Entry[]>()
    for (const e of groups) byFile.set(e.file, [...(byFile.get(e.file) ?? []), e])
    io.groups += groups.length
    const parts = await Promise.all([...byFile].map(async ([file, gs]) => {
      const start = gs[0].offset, end = gs[gs.length - 1].offset + gs[gs.length - 1].length
      const buf = await this.blobs.range(file, start, end - start)
      io.bytes += buf.byteLength
      const out: T[] = []
      const make = row ?? ((cols: Record<string, unknown[]>, i: number) => Object.fromEntries(spec.columns.map(c => [c, cols[c][i]])) as T)
      for (const g of gs) {
        const cols = await decodeFlat(spec, start + buf.byteLength, g.rows, g.chunks, buf, start)
        io.rows_read += g.rows
        for (let i = 0; i < g.rows; i++) {
          const q = cols.q[i] as string, k = cols[key][i] as string
          if ((cmp(q, lo[0]) || cmp(k, lo[1])) < 0 || (cmp(q, hi[0]) || cmp(k, hi[1])) >= 0) continue
          out.push(make(cols, i))
        }
      }
      return out
    }))
    return parts.flat()
  }
}

// --- the drill ----------------------------------------------------------------------------------

/** A view's answer, any date: the match roots under P (≤ `dispatch_rows`), or P's rollup. */
export type DrillAnswer =
  | { source: 'plain' }
  /** A long literal that is no member (the light reader's, or nobody's). */
  | { source: 'none' }
  | { source: 'roots'; kind: Kind; c: string; upper: number; hits: Hit[]; io: DrillIo }
  | { source: 'rollup' | 'catalog'; kind: Kind; c: string; upper: number; rollup: Rollup; io: DrillIo }

/** The base catalog's per-member lookup (the fleet root's rollup). */
export interface CatalogLookup { lookup(q: string): Promise<{ member: Member | null }>; info?(): Promise<CatalogMeta> }

export class Drill {
  private meta?: Promise<DrillMeta>
  private aliasMap?: Promise<Map<string, { c: string; n: number }>>
  readonly files: Record<Kind, Record<Set_, GroupFile>>
  constructor(readonly blobs: Blobs, readonly catalog: CatalogLookup | null, readonly cache?: { top?: IndexCache<Top>; aliases?: IndexCache<[string[], string[], number[]]> }) {
    const meta = () => this.info()
    const gf = (k: Kind, s: Set_) => new GroupFile(blobs, k, s, meta, cache?.top)
    this.files = { long: { roots: gf('long', 'roots'), rollups: gf('long', 'rollups') }, short: { roots: gf('short', 'roots'), rollups: gf('short', 'rollups') } }
  }

  /** `meta.json` (isolate-held). */
  info(): Promise<DrillMeta> {
    this.meta ??= this.blobs.json<DrillMeta>('meta.json').then(m => {
      for (const k of ['R', 'K', 'rg', 'idx_rg', 'dispatch_rows'] as const) if (!Number.isSafeInteger(m[k]) || m[k] < 1) throw new Error(`static drill: bad meta.json ${k}`)
      return m
    })
    this.meta.catch(() => { this.meta = undefined })
    return this.meta
  }

  /** Every long member → its canonical (itself, or the member whose root set it shares) and its root rows. */
  aliases(): Promise<Map<string, { c: string; n: number }>> {
    this.aliasMap ??= (async () => {
      const cached = await this.cache?.aliases?.get('aliases.parquet')
      if (cached) return new Map(cached[0].map((q, i) => [q, { c: cached[1][i] || q, n: cached[2][i] }]))
      const rows = await parquetReadObjects({ file: await this.blobs.range('aliases.parquet', 0), columns: ['q', 'canonical', 'n'], compressors }) as { q: string; canonical: string; n: bigint }[]
      const m = new Map(rows.map(r => [r.q, { c: r.canonical, n: num(r.n) }]))
      await this.cache?.aliases?.put('aliases.parquet', [[...m.keys()], [...m].map(([q, a]) => a.c === q ? '' : a.c), [...m.values()].map(a => a.n)])
      return m
    })()
    this.aliasMap.catch(() => { this.aliasMap = undefined })
    return this.aliasMap
  }

  /** `t`'s (lowercase) view at `P`, any date. */
  async view(t: string, P: string): Promise<DrillAnswer> {
    if (P.toLowerCase().includes(t)) return { source: 'plain' }
    const kind: Kind = [...t].length <= 2 ? 'short' : 'long'
    let c = t
    let all: number | null = null
    if (kind === 'long') {
      const a = (await this.aliases()).get(t)
      if (a == null) return { source: 'none' }
      c = a.c
      all = a.n
    }
    const meta = await this.info()
    const io = newIo()
    const roots = this.files[kind].roots
    // The fleet root: the whole member, `[(c, ''), (c + U+0000, ''))`.
    const lo: Key = P === '' ? [c, ''] : [c, P + '/']
    const hi: Key = P === '' ? [c + '\0', ''] : [c, P + '0']
    const sel = await roots.select(lo, hi, io, meta.dispatch_rows)
    if (sel.groups && sel.rows <= meta.dispatch_rows) {
      // Hits straight from the columns (no row object per root: a member's ~80K roots, decoded per isolate).
      const hits = await roots.read<Hit>(lo, hi, sel.groups, io, (c, i) => {
        const path = c.path[i] as string
        let depth = 1
        for (let j = path.indexOf('/'); j !== -1; j = path.indexOf('/', j + 1)) depth++
        return { path, depth, usr: c.usr[i] as string, vf: Number(c.vf[i]) * 1000, vt: Number(c.vt[i]) * 1000, size: c.size[i] as bigint, n: c.n_files[i] as bigint }
      })
      return { source: 'roots', kind, c, upper: sel.rows, hits, io }
    }
    if (P === '') {
      // No rollup at the fleet root (heavy directories are depth ≥ 1): the catalog's per-bucket cells.
      const got = await this.catalog?.lookup(t)
      if (!got?.member) throw new Error(`static drill: ${JSON.stringify(t)} has ${sel.rows} root rows but no catalog cells`)
      const cells: RollupCell[] = got.member.cells.map(x => ({ kind: 1, child: x.bucket, vf: Number(x.vf) * 1000, b: x.b, o: x.o }))
      const buckets = new Set(cells.map(x => x.child)).size
      // A long member's root rows are its alias entry's; a short one's are counted from the roots index.
      const rows = all ?? await this.files[kind].roots.count(lo, hi, io)
      return { source: 'catalog', kind, c, upper: sel.rows, rollup: { dir: '', kept: buckets, rows, children: buckets, cells }, io }
    }
    const rlo: Key = [c, P], rhi: Key = [c, P + '\0']
    const rs = await this.files[kind].rollups.select(rlo, rhi, io)
    const rows = await this.files[kind].rollups.read(rlo, rhi, rs.groups ?? [], io)
    if (!rows.length || rows[0].kind !== 0) throw new Error(`static drill: (${JSON.stringify(t)}, ${JSON.stringify(P)}) bounds ${sel.rows} root rows but has no rollup`)
    const head = rows[0]
    const cells: RollupCell[] = rows.slice(1).map(r => ({ kind: r.kind as 1 | 2, child: r.child as string, vf: Number(r.vf) * 1000, b: r.b as bigint, o: r.o as bigint }))
    return { source: 'rollup', kind, c, upper: sel.rows, rollup: { dir: P, kept: num(head.vf), rows: num(head.b), children: num(head.o), cells }, io }
  }
}

/** Answers held per isolate, across (literal, path) views: the oldest dropped past this many hits + cells. */
const HELD = 400_000

/** The filter's heavy source over a drill: a view's match roots, or its rollup, answering the base
 *  generation's scans only (`scans`). Plain views and non-members decline (null): the caller reads as before. */
export class DrillSource implements HitSource {
  private held = new Map<string, Promise<DrillAnswer>>()
  private sizes = new Map<string, number>()
  /** `cache`: roots answers across isolates (the colo's Cache API) — decoding a member's ~80K roots from the
   *  drill's parquet is seconds of an edge isolate's CPU, each new isolate again. */
  constructor(readonly drill: Drill, readonly scans: () => Promise<string[]>, readonly cache?: HitCache) {}

  /** `t`'s view at `P` (isolate-held). */
  answer(t: string, P: string): Promise<DrillAnswer> {
    const k = `${t}\0${P}`
    let p = this.held.get(k)
    if (p) { this.held.delete(k); this.held.set(k, p); return p }
    p = this.drill.view(t, P)
    this.held.set(k, p)
    p.then(a => {
      this.sizes.set(k, a.source === 'roots' ? a.hits.length : 'rollup' in a ? a.rollup.cells.length : 0)
      let n = [...this.sizes.values()].reduce((s, x) => s + x, 0)
      for (const old of this.held.keys()) {
        if (n <= HELD || old === k) break
        n -= this.sizes.get(old) ?? 0
        this.held.delete(old); this.sizes.delete(old)
      }
    }, () => { this.held.delete(k) })
    return p
  }

  async hits(key: string, under: string): Promise<Found | null> {
    const t0 = Date.now()
    const k = `${key}\0${under}`
    if (this.cache && !this.held.has(k)) {
      const hits = await this.cache.get(k)
      if (hits) {
        const kind: Kind = [...key].length <= 2 ? 'short' : 'long'
        const p = Promise.resolve<DrillAnswer>({ source: 'roots', kind, c: key, upper: hits.length, hits, io: newIo() })
        this.held.set(k, p); this.sizes.set(k, hits.length)
        return { hits, io: { from: 'drill', source: 'roots', cache: 'colo', ms: Date.now() - t0 }, scans: await this.scans() }
      }
    }
    const fresh = !this.held.has(k)
    const a = await this.answer(key, under)
    if (fresh && a.source === 'roots' && this.cache) await this.cache.put(k, a.hits).catch(() => {})
    if (a.source === 'plain' || a.source === 'none') return null
    const scans = await this.scans()
    const io = { from: 'drill', source: a.source, kind: a.kind, c: a.c, upper: a.upper, ...a.io, ms: Date.now() - t0 }
    return a.source === 'roots' ? { hits: a.hits, io, scans } : { rollup: a.rollup, io, scans }
  }
}

/** Static name search (specs/architecture/static-name-search.md): answer a literal's per-bucket first-hit
 *  totals from the suffix shards on R2 alone — no D1, no query box, no footer parse on the hot path.
 *
 *  Layout (`dt-cloud static-names`, one generation under `static-names/<gen>/`): `sx/s####.parquet`, one
 *  row per (lowercase name suffix of ≥ 3 characters, version) — `s, depth, path, usr, vf, vt, size,
 *  n_files` — sorted by `s`, 8K-row groups, zstd; `shards.json`, the shard plan: shard `i` holds the
 *  suffixes whose first three characters are in `[lo, hi)` (a 3-character prefix is never split, so a
 *  literal's rows are one contiguous run of one file).
 *
 *  A query: `shards.json` (isolate-cached) names the shard → that shard's group index (per row group:
 *  `s` min/max and each column chunk's offsets, cut from the shard's footer once and kept in the isolate
 *  and the colo's Cache API, so warm queries never touch the footer) → the groups whose `s` range can
 *  hold suffixes starting with the key → ONE ranged GET of their bytes → hyparquet decodes them through
 *  a FileMetaData built from the index → the first-hit filter and per-bucket sums for each date.
 *
 *  Exactly `static_names.Reader.answer`: the key is the lowercased literal, untruncated; groups `[a, b)`
 *  with `a` = first group whose `s_max ≥ key`, `b` = first whose `s_min ≥ key + U+10FFFF` (code-point
 *  order, as the shards are sorted); rows whose `s` starts with the key and whose lowercase basename
 *  contains it; per date `D` (epoch ms of the scan id): live `vf ≤ D < vt`, depth ≥ 1, deduped by
 *  `(path, usr, vf)` (a name holding the key twice has two suffix rows), dropped when the lowercase
 *  parent path contains the key (an ancestor already covers it); `size`/`n_files` summed per first
 *  path segment. */
import { type FileMetaData, parquetRead, type RowGroup } from 'hyparquet'
import { compressors } from './zstd.js'

export const STATIC_GEN = '2026-10-08'
export const STATIC_PREFIX = `static-names/${STATIC_GEN}`

// --- order --------------------------------------------------------------------------------------

/** A UTF-16 code unit remapped so that comparing units compares code points (surrogates above the BMP). */
const unit = (c: number): number => c < 0xd800 ? c : c < 0xe000 ? c + 0x2000 : c - 0x800
/** Code-point order (DuckDB's and Python's string order), which plain `<` breaks only around surrogates. */
export function cmp(a: string, b: string): number {
  const n = Math.min(a.length, b.length)
  for (let i = 0; i < n; i++) {
    const x = a.charCodeAt(i), y = b.charCodeAt(i)
    if (x !== y) return unit(x) - unit(y)
  }
  return a.length - b.length
}
/** The first index in `[0, n)` whose `pred` holds (`pred` monotone). */
function bisect(n: number, pred: (i: number) => boolean): number {
  let lo = 0, hi = n
  while (lo < hi) { const mid = (lo + hi) >>> 1; if (pred(mid)) hi = mid; else lo = mid + 1 }
  return lo
}

// --- shard plan ---------------------------------------------------------------------------------

export interface Shard { i: number; lo: string; hi: string | null; rows: number }
/** The shard whose `[lo, hi)` holds the key's first three characters, or null (no suffix that low). */
export function shardFor(shards: Shard[], key: string): Shard | null {
  const p3 = [...key].slice(0, 3).join('')
  const k = bisect(shards.length, i => cmp(shards[i].lo, p3) > 0) - 1
  if (k < 0) return null
  const s = shards[k]
  return s.hi == null || cmp(p3, s.hi) < 0 ? s : null
}
export const shardFile = (i: number): string => `sx/s${String(i).padStart(4, '0')}.parquet`

// --- footer → group index -----------------------------------------------------------------------

/** The sx schema's leaves, in file order (checked against each footer). `vf`/`vt` are left as their
 *  physical INT64 (epoch ms as bigint): no timestamp conversion. */
export const SX_COLUMNS = ['s', 'depth', 'path', 'usr', 'vf', 'vt', 'size', 'n_files'] as const
const SX_TYPES = ['BYTE_ARRAY', 'INT32', 'BYTE_ARRAY', 'BYTE_ARRAY', 'INT64', 'INT64', 'INT64', 'INT64']
const SX_SCHEMA = [
  { name: 'schema', repetition_type: 'REQUIRED', num_children: SX_COLUMNS.length },
  ...SX_COLUMNS.map((name, i) => ({ name, type: SX_TYPES[i], repetition_type: 'REQUIRED', ...(SX_TYPES[i] === 'BYTE_ARRAY' ? { converted_type: 'UTF8' } : {}) })),
]
const NC = SX_COLUMNS.length
const ZSTD = 6

/** One shard's row groups: `s` min/max (exact: the writer's statistics are untruncated, `is_*_exact`),
 *  rows, and per column chunk `[data_page_offset, total_compressed_size, dictionary_page_offset | 0]`
 *  (`chunks[g * NC + c]`) — the only column metadata hyparquet reads. */
export interface GroupIndex { size: number; sMin: string[]; sMax: string[]; rows: number[]; chunks: [number, number, number][] }

/** A minimal Thrift compact-protocol walker over a parquet footer: only the fields the index needs are
 *  decoded; everything else (schema, encodings, page statistics, size statistics, kv metadata) is skipped. */
class Compact {
  pos = 0
  constructor(readonly b: Uint8Array) {}
  varint(): number {
    let x = 0, mul = 1, byte
    do { byte = this.b[this.pos++]; x += (byte & 0x7f) * mul; mul *= 128 } while (byte & 0x80)
    return x
  }
  zigzag(): number { const v = this.varint(); return v % 2 ? -(v + 1) / 2 : v / 2 }
  binary(): Uint8Array { const n = this.varint(); const out = this.b.subarray(this.pos, this.pos + n); this.pos += n; return out }
  /** Iterate a struct's fields; `on(fid, type)` returns true when it consumed the value. */
  struct(on: (fid: number, type: number) => boolean): void {
    let fid = 0
    for (;;) {
      const byte = this.b[this.pos++], type = byte & 0x0f
      if (type === 0) return
      const delta = byte >> 4
      fid = delta ? fid + delta : this.zigzag()
      if (!on(fid, type)) this.skip(type)
    }
  }
  list(each: (type: number, i: number) => void): void {
    const byte = this.b[this.pos++]
    let n = byte >> 4
    if (n === 15) n = this.varint()
    const type = byte & 0x0f
    for (let i = 0; i < n; i++) each(type, i)
  }
  skip(type: number): void {
    switch (type) {
      case 1: case 2: return
      case 3: this.pos++; return
      case 4: case 5: case 6: this.varint(); return
      case 7: this.pos += 8; return
      case 8: { const n = this.varint(); this.pos += n; return }
      case 9: case 10: this.list(t => (t === 1 || t === 2) ? this.pos++ : this.skip(t)); return
      case 11: {
        const n = this.varint()
        if (!n) return
        const kv = this.b[this.pos++]
        for (let i = 0; i < n; i++) { this.skip(kv >> 4); this.skip(kv & 0x0f) }
        return
      }
      case 12: this.struct(() => false); return
      default: throw new Error(`thrift: unknown compact type ${type}`)
    }
  }
}

const utf8 = new TextDecoder()

/** Cut a shard's group index from its footer (the FileMetaData thrift, without the trailing length/magic). */
export function groupIndex(footer: Uint8Array, size: number): GroupIndex {
  const r = new Compact(footer)
  const idx: GroupIndex = { size, sMin: [], sMax: [], rows: [], chunks: [] }
  r.struct((fid, type) => {
    if (fid !== 4 || type !== 9) return false
    r.list(() => {
      let nCols = 0
      r.struct((rf, rt) => {
        if (rf === 3 && rt === 6) { idx.rows.push(r.varint() / 2); return true } // num_rows: zigzag of a non-negative i64
        if (rf !== 1 || rt !== 9) return false
        r.list((_, c) => {
          nCols++
          let dpo = 0, total = 0, dict = 0, codec = -1, min: string | null = null, max: string | null = null
          r.struct((cf, ct) => {
            if (cf !== 3 || ct !== 12) return false
            r.struct((mf, mt) => {
              if (mf === 4) { codec = r.zigzag(); return true }
              if (mf === 7) { total = r.zigzag(); return true }
              if (mf === 9) { dpo = r.zigzag(); return true }
              if (mf === 11) { dict = r.zigzag(); return true }
              if (mf === 12 && c === 0) {
                r.struct((sf, st) => {
                  if (sf === 5 && st === 8) { max = utf8.decode(r.binary()); return true }
                  if (sf === 6 && st === 8) { min = utf8.decode(r.binary()); return true }
                  return false
                })
                return true
              }
              return false
            })
            return true
          })
          if (codec !== ZSTD) throw new Error(`sx footer: column ${c} codec ${codec}, expected ZSTD`)
          if (c === 0) {
            if (min == null || max == null) throw new Error('sx footer: a row group has no exact `s` statistics')
            idx.sMin.push(min); idx.sMax.push(max)
          }
          idx.chunks.push([dpo, total, dict])
        })
        if (nCols !== NC) throw new Error(`sx footer: ${nCols} columns, expected ${NC}`)
        return true
      })
    })
    return true
  })
  if (idx.rows.length !== idx.sMin.length) throw new Error('sx footer: row group count mismatch')
  return idx
}

/** The groups `[a, b)` whose `s` range can hold suffixes starting with `key` (`Reader.rows`' bisects). */
export function selectGroups(idx: GroupIndex, key: string): [number, number] {
  const n = idx.sMin.length, top = key + '\u{10FFFF}'
  const a = bisect(n, i => cmp(idx.sMax[i], key) >= 0)
  const b = bisect(n, i => cmp(idx.sMin[i], top) >= 0)
  return [a, Math.max(a, b)]
}

/** The byte span `[start, end)` of groups `[a, b)` (their column chunks are contiguous). */
export function groupSpan(idx: GroupIndex, a: number, b: number): [number, number] {
  let start = Infinity, end = 0
  for (let g = a; g < b; g++) for (let c = 0; c < NC; c++) {
    const [dpo, total, dict] = idx.chunks[g * NC + c]
    const s = dict > 0 ? Math.min(dict, dpo) : dpo
    start = Math.min(start, s); end = Math.max(end, s + total)
  }
  return [start, end]
}

// --- decode + answer ----------------------------------------------------------------------------

export interface SxColumns { s: string[]; depth: number[]; path: string[]; usr: string[]; vf: bigint[]; vt: bigint[]; size: bigint[]; n_files: bigint[] }

/** Decode groups `[a, b)` from their bytes (`buf` holds the file's `[start, start + buf.byteLength)`). */
export async function decodeGroups(idx: GroupIndex, a: number, b: number, buf: ArrayBuffer, start: number): Promise<SxColumns> {
  const row_groups = [] as RowGroup[]
  let n = 0
  for (let g = a; g < b; g++) {
    n += idx.rows[g]
    row_groups.push({
      num_rows: BigInt(idx.rows[g]),
      columns: SX_COLUMNS.map((name, c) => {
        const [dpo, total, dict] = idx.chunks[g * NC + c]
        return { meta_data: { type: SX_TYPES[c], path_in_schema: [name], codec: 'ZSTD', data_page_offset: BigInt(dpo), total_compressed_size: BigInt(total), ...(dict ? { dictionary_page_offset: BigInt(dict) } : {}) } }
      }),
    } as unknown as RowGroup)
  }
  const metadata = { version: 1, schema: SX_SCHEMA, num_rows: BigInt(n), row_groups, metadata_length: 0 } as unknown as FileMetaData
  const file = {
    byteLength: idx.size,
    slice(s: number, e?: number) {
      const end = e ?? idx.size
      if (s < start || end > start + buf.byteLength) throw new Error(`sx read outside the fetched span: ${s}-${end}`)
      return buf.slice(s - start, end - start)
    },
  }
  const out = Object.fromEntries(SX_COLUMNS.map(c => [c, new Array(n)])) as unknown as Record<string, unknown[]>
  await parquetRead({
    file, metadata, columns: [...SX_COLUMNS], compressors,
    onChunk: ({ columnName, columnData, rowStart }) => {
      const arr = out[columnName]
      for (let i = 0; i < columnData.length; i++) arr[rowStart + i] = columnData[i]
    },
  })
  return out as unknown as SxColumns
}

/** A scan id (`2026-10-01`, or `2026-10-01T0003`, UTC) as epoch ms (`static_names.scan_epoch`). */
export function scanMs(id: string): number {
  const m = /^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2})(\d{2}))?$/.exec(id)
  if (!m) throw new Error(`bad scan id ${id}`)
  return Date.UTC(+m[1], +m[2] - 1, +m[3], +(m[4] ?? 0), +(m[5] ?? 0))
}

export type Totals = Record<string, [bigint, bigint]>
export interface Answer { rows_read: number; rows_matching: number; answers: Record<string, Totals> }

/** The first-hit filter and per-bucket sums over decoded suffix rows, per date (`Reader.answer`). */
export function answerRows(cols: SxColumns, key: string, dates: string[]): Answer {
  // Everything but liveness is date-independent, so it is decided once per row: the name match, the
  // `(path, usr, vf)` dedup (one version = one `vt`, so a kept duplicate is live exactly when the first
  // is), depth, and the parent test. Each date then only checks `vf ≤ D < vt` over the first hits.
  let matching = 0
  const seen = new Set<string>(), first: number[] = []
  for (let i = 0; i < cols.s.length; i++) {
    if (!cols.s[i].startsWith(key)) continue
    const p = cols.path[i], slash = p.lastIndexOf('/')
    if (!p.slice(slash + 1).toLowerCase().includes(key)) continue
    matching++
    if (cols.depth[i] < 1) continue
    const k = `${p}\0${cols.usr[i]}\0${cols.vf[i]}`
    if (seen.has(k)) continue
    seen.add(k)
    if ((slash < 0 ? '' : p.slice(0, slash)).toLowerCase().includes(key)) continue
    first.push(i)
  }
  const bucket = first.map(i => { const p = cols.path[i], s = p.indexOf('/'); return s < 0 ? p : p.slice(0, s) })
  const answers: Record<string, Totals> = {}
  for (const d of dates) {
    const D = BigInt(scanMs(d)), totals: Totals = {}
    first.forEach((i, j) => {
      if (!(cols.vf[i] <= D && D < cols.vt[i])) return
      const t = totals[bucket[j]] ??= [0n, 0n]
      t[0] += cols.size[i]; t[1] += cols.n_files[i]
    })
    answers[d] = Object.fromEntries(Object.keys(totals).sort().map(b => [b, totals[b]]))
  }
  return { rows_read: cols.s.length, rows_matching: matching, answers }
}

// --- storage seam (R2 in the Worker, files in tests) -------------------------------------------

/** Ranged reads of the generation's objects (keys relative to `STATIC_PREFIX`). */
export interface Blobs {
  /** `[offset, offset + length)` of `key`; `length` omitted = to the end. */
  range(key: string, offset: number, length?: number): Promise<ArrayBuffer>
  /** The last `n` bytes and the object's size. */
  suffix(key: string, n: number): Promise<{ buf: ArrayBuffer; size: number }>
  json<T>(key: string): Promise<T>
}

/** Where a query's time and bytes went (the `x-static-io` header). */
export interface Io { shard: number | null; groups: number; bytes: number; rows_read: number; rows_matching: number; index: 'isolate' | 'cache' | 'footer' | 'none'; footer_bytes?: number; ms: Record<string, number> }

export interface IndexCache { get(file: string): Promise<GroupIndex | null>; put(file: string, idx: GroupIndex): Promise<void> }

export class StaticNames {
  private shards?: Promise<Shard[]>
  private indexes = new Map<string, Promise<GroupIndex>>()
  constructor(readonly blobs: Blobs, readonly cache?: IndexCache, readonly clock: () => Promise<number> = async () => Date.now(), readonly maxIndexes = 24) {}

  plan(): Promise<Shard[]> {
    this.shards ??= this.blobs.json<{ shards: Shard[] }>('shards.json').then(d => d.shards).catch(e => { this.shards = undefined; throw e })
    return this.shards
  }

  /** A shard's group index: the isolate's, else the colo cache's, else cut from the footer (one suffix GET
   *  sized from the shard's row count, a second only if the footer is bigger than guessed). */
  private async index(shard: Shard, io: Io): Promise<GroupIndex> {
    const file = shardFile(shard.i)
    const held = this.indexes.get(file)
    if (held) { this.indexes.delete(file); this.indexes.set(file, held); io.index = 'isolate'; return held }
    const p = (async () => {
      const cached = await this.cache?.get(file)
      if (cached) { io.index = 'cache'; return cached }
      io.index = 'footer'
      const groups = Math.ceil(shard.rows / 8192)
      let { buf, size } = await this.blobs.suffix(file, Math.min(groups * 1600 + (64 << 10), 64 << 20))
      const tail = new DataView(buf, buf.byteLength - 8)
      const flen = tail.getUint32(0, true)
      if (flen + 8 > buf.byteLength) buf = (await this.blobs.suffix(file, flen + 8)).buf
      io.footer_bytes = flen
      const idx = groupIndex(new Uint8Array(buf, buf.byteLength - 8 - flen, flen), size)
      await this.cache?.put(file, idx)
      return idx
    })()
    this.indexes.set(file, p)
    p.catch(() => this.indexes.delete(file))
    while (this.indexes.size > this.maxIndexes) this.indexes.delete(this.indexes.keys().next().value!)
    return p
  }

  /** How many suffix rows (row-group granular, an upper bound on the matching rows) `key`'s range holds:
   *  the dispatch's bound, before any data is read. */
  async extent(key: string, io: Io): Promise<{ shard: Shard; idx: GroupIndex; a: number; b: number; rows: number } | null> {
    const shard = shardFor(await this.plan(), key)
    io.shard = shard?.i ?? null
    if (!shard) return null
    const idx = await this.index(shard, io)
    const [a, b] = selectGroups(idx, key)
    let rows = 0
    for (let g = a; g < b; g++) rows += idx.rows[g]
    return { shard, idx, a, b, rows }
  }

  /** Answer `key` (lowercased, ≥ 3 characters) on each date; `maxRows` refuses (null) a range above it
   *  before any data is fetched. */
  async answer(key: string, dates: string[], maxRows = Infinity): Promise<{ io: Io; answer: Answer | null }> {
    const io: Io = { shard: null, groups: 0, bytes: 0, rows_read: 0, rows_matching: 0, index: 'none', ms: {} }
    let t = await this.clock()
    const lap = async (name: string) => { const now = await this.clock(); io.ms[name] = now - t; t = now }
    const ext = await this.extent(key, io)
    await lap('index')
    if (!ext || ext.a === ext.b) return { io, answer: { rows_read: 0, rows_matching: 0, answers: Object.fromEntries(dates.map(d => [d, {}])) } }
    io.groups = ext.b - ext.a
    if (ext.rows > maxRows) { io.rows_read = ext.rows; return { io, answer: null } }
    const [start, end] = groupSpan(ext.idx, ext.a, ext.b)
    const buf = await this.blobs.range(shardFile(ext.shard.i), start, end - start)
    io.bytes = buf.byteLength
    await lap('fetch')
    const cols = await decodeGroups(ext.idx, ext.a, ext.b, buf, start)
    await lap('decode')
    const answer = answerRows(cols, key, dates)
    await lap('filter')
    io.rows_read = answer.rows_read
    io.rows_matching = answer.rows_matching
    return { io, answer }
  }
}

/** `Blobs` over an R2 bucket binding (keys under `STATIC_PREFIX`). */
export function r2Blobs(r2: R2Bucket, prefix = STATIC_PREFIX): Blobs {
  const get = async (key: string, range?: R2Range) => {
    const o = await r2.get(`${prefix}/${key}`, range ? { range } : undefined)
    if (!o) throw new Error(`static names: ${prefix}/${key} is missing`)
    return o
  }
  return {
    range: async (key, offset, length) => (await get(key, length == null ? { offset } : { offset, length })).arrayBuffer(),
    suffix: async (key, n) => { const o = await get(key, { suffix: n }); return { buf: await o.arrayBuffer(), size: o.size } },
    json: async key => (await get(key)).json(),
  }
}

/** The colo's Cache API as the group-index tier between isolates (JSON; ~1 MB per shard). */
export function cacheIndexes(cache: Cache, prefix = STATIC_PREFIX): IndexCache {
  const url = (file: string) => `https://static-names.invalid/${prefix}/${file}.index.json`
  return {
    async get(file) {
      const r = await cache.match(url(file))
      return r ? r.json() as Promise<GroupIndex> : null
    },
    async put(file, idx) {
      await cache.put(url(file), new Response(JSON.stringify(idx), { headers: { 'content-type': 'application/json', 'cache-control': 'public, max-age=604800' } }))
    },
  }
}

// The filter's matches as the fewest exact prefixes (`/api/filter-cover`, `functions/_lib/cover.ts`), and
// what the bulk bar and the table's rows do with them: under a filter, a selection is always match roots
// (or folders every object of which matches) — never a row's whole prefix.
import { type QueryClient, useQuery } from '@tanstack/react-query'
import { COVER_V, type CoverItem } from '../functions/_lib/cover'
import { HttpError, type PlanItem } from './batches'

export { ASSIGN_CHUNK, assignInBatches, chunks, STAGE_CHUNK, stageInBatches } from './batches'
export { actionTargets, type Resolved, rowItems, type Targets, targetsText } from './coverTargets'
import { type Resolved, rowItems } from './coverTargets'

export type { CoverItem } from '../functions/_lib/cover'

export interface FilterCover {
  date: string
  path: string
  q: string
  /** Sorted by path. */
  items: CoverItem[]
  roots: { n: number; b: number; o: number }
  /** False: the matches here can't all be listed; `reason` says why in plain words, and nothing acts. */
  complete: boolean
  reason?: string
  looked: number
  unchecked: number
}

/** A failed `/api/filter-cover` (its HTTP status: a 5xx is worth one retry, a 4xx never). */
export class CoverError extends HttpError {}

/** One retry, on a 5xx (the cold cover's D1 stall or isolate limit: gcs 10-09 `nemotron`, a 503 at 46 s). */
export const coverRetry = (failures: number, e: Error): boolean => failures < 1 && e instanceof CoverError && e.status >= 500

/** What a cover is asked for: a scan, a path (the view's, or one row's), a filter and its syntax. */
export interface CoverArgs { date: string | null; path: string; q: string | undefined; qs?: string }

/** The cover query's options (`useFilterCover`, `fetchCover`): one retry on a 5xx; `enabled` only gates the
 *  page's observer (an imperative fetch ignores it). */
export function coverQuery(
  fetcher: (url: string, init?: RequestInit) => Promise<Response>,
  storeKey: string,
  o: { date: string | null; path: string; q: string | undefined; qs?: string; enabled: boolean },
) {
  return {
    queryKey: ['filter-cover', storeKey, o.date, o.path, o.q, o.qs ?? ''],
    enabled: o.enabled && !!o.date && !!o.q,
    staleTime: Infinity,
    retry: coverRetry,
    queryFn: async ({ signal }: { signal?: AbortSignal }): Promise<FilterCover> => {
      const qs = new URLSearchParams({ cv: String(COVER_V), date: o.date!, path: o.path, q: o.q!, ...(o.qs ? { qs: o.qs } : {}) })
      const r = await fetcher(`/api/filter-cover?${qs}`, { credentials: 'include', signal })
      const text = await r.text()
      if (!r.ok) {
        let msg = text
        try { msg = (JSON.parse(text) as { error?: string }).error ?? text } catch { /* plain text */ }
        throw new CoverError(msg, r.status)
      }
      return JSON.parse(text) as FilterCover
    },
  }
}

/** The cover of `q`'s matches under `path` on `date`, from the cache (fetched by `prefetchCover` /
 *  `fetchCover` on the viewer's intent; with `enabled` false the observer never fetches by itself). */
export function useFilterCover(
  fetcher: (url: string, init?: RequestInit) => Promise<Response>,
  storeKey: string,
  o: { date: string | null; path: string; q: string | undefined; qs?: string; enabled: boolean },
) {
  return useQuery<FilterCover>(coverQuery(fetcher, storeKey, o))
}

type Fetcher = (url: string, init?: RequestInit) => Promise<Response>

/** The cover for `a`, cached (`staleTime: Infinity`) and deduplicated with any fetch in flight for the same
 *  key — the click awaits what the hover started. Rejects with the `CoverError` (after the one 5xx retry). */
export const fetchCover = (qc: QueryClient, fetcher: Fetcher, storeKey: string, a: CoverArgs): Promise<FilterCover> =>
  qc.fetchQuery(coverQuery(fetcher, storeKey, { ...a, enabled: true }))

/** Start the cover for `a` on the viewer's intent (hover, focus): a nop when cached or in flight. Never throws
 *  (a failure is the click's to report). */
export const prefetchCover = (qc: QueryClient, fetcher: Fetcher, storeKey: string, a: CoverArgs): Promise<void> =>
  qc.prefetchQuery(coverQuery(fetcher, storeKey, { ...a, enabled: true }))

/** A table row's matches (`row`: its path below the store root): the view's cover sliced to the row when it
 *  is cached and complete (no request), else the row's own cover (`path=row`) — under the cap even when the
 *  view's isn't, since a row's matches may collapse further. */
export async function rowCover(qc: QueryClient, fetcher: Fetcher, storeKey: string, view: CoverArgs, row: string): Promise<Resolved> {
  const v = qc.getQueryData<FilterCover>(coverQuery(fetcher, storeKey, { ...view, enabled: true }).queryKey)
  if (v?.complete) return { items: rowItems(v.items, row), complete: true }
  return fetchCover(qc, fetcher, storeKey, { ...view, path: row })
}

/** Several rows' matches as one set (the table's selection): complete only when each is. */
export async function rowsCover(resolve: (row: string) => Promise<Resolved>, rows: readonly string[]): Promise<Resolved> {
  const all = await Promise.all(rows.map(resolve))
  const short = all.find(r => !r.complete)
  return short ? { items: [], complete: false, reason: short.reason } : { items: all.flatMap(r => r.items), complete: true }
}

const parentOf = (p: string) => { const i = p.lastIndexOf('/'); return i < 0 ? '' : p.slice(0, i) }

/** Items grouped by parent folder, heaviest group first (ties by folder), items by path within each. */
export interface CoverGroup { parent: string; items: CoverItem[]; b: number; o: number }
export function groupItems(items: readonly CoverItem[]): CoverGroup[] {
  const by = new Map<string, CoverItem[]>()
  for (const i of items) by.set(parentOf(i.path), [...(by.get(parentOf(i.path)) ?? []), i])
  return [...by.entries()]
    .map(([parent, is]) => ({ parent, items: is, b: is.reduce((n, i) => n + i.b, 0), o: is.reduce((n, i) => n + i.o, 0) }))
    .sort((x, y) => y.b - x.b || (x.parent < y.parent ? -1 : x.parent > y.parent ? 1 : 0))
}

/** A row's label under a filter: the drawn chain of single children below it, as the treemap tile labels a
 *  collapsed chain (`a/b/c`, or `a/…/z` past three) — so a row holding one match shows the path to it. */
export interface ChainNode { n: string; c?: ChainNode[] }
export function chainOf(node: ChainNode): { label: string; segs: string[] } {
  const segs = [node.n]
  let cur = node
  while (cur.c?.length === 1 && !cur.c[0].n.startsWith('(')) { cur = cur.c[0]; segs.push(cur.n) }
  const label = segs.length > 3 ? `${segs[0]}/…/${segs[segs.length - 1]}` : segs.join('/')
  return { label, segs }
}

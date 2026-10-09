// The filter's matches as the fewest exact prefixes (`/api/filter-cover`, `functions/_lib/cover.ts`), and
// what the bulk bar and the table's rows do with them: under a filter, a selection is always match roots
// (or folders every object of which matches) — never a row's whole prefix.
import { useQuery } from '@tanstack/react-query'
import type { CoverItem } from '../functions/_lib/cover'

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

/** The cover of `q`'s matches under `path` on `date` (null while not wanted). */
export function useFilterCover(
  fetcher: (url: string, init?: RequestInit) => Promise<Response>,
  storeKey: string,
  o: { date: string | null; path: string; q: string | undefined; qs?: string; enabled: boolean },
) {
  return useQuery<FilterCover>({
    queryKey: ['filter-cover', storeKey, o.date, o.path, o.q, o.qs ?? ''],
    enabled: o.enabled && !!o.date && !!o.q,
    staleTime: Infinity,
    retry: false,
    queryFn: async ({ signal }) => {
      const qs = new URLSearchParams({ date: o.date!, path: o.path, q: o.q!, ...(o.qs ? { qs: o.qs } : {}) })
      const r = await fetcher(`/api/filter-cover?${qs}`, { credentials: 'include', signal })
      const text = await r.text()
      if (!r.ok) {
        let msg = text
        try { msg = (JSON.parse(text) as { error?: string }).error ?? text } catch { /* plain text */ }
        throw new Error(msg)
      }
      return JSON.parse(text) as FilterCover
    },
  })
}

const under = (p: string, a: string) => p === a || p.startsWith(a + '/')
const parentOf = (p: string) => { const i = p.lastIndexOf('/'); return i < 0 ? '' : p.slice(0, i) }
const depthOf = (p: string) => (p === '' ? 0 : p.split('/').length)

/** The items a table row (path below the root) holds: those at or under it. */
export const rowItems = (items: readonly CoverItem[], row: string): CoverItem[] => items.filter(i => under(i.path, row))

/** Items grouped by parent folder, heaviest group first (ties by folder), items by path within each. */
export interface CoverGroup { parent: string; items: CoverItem[]; b: number; o: number }
export function groupItems(items: readonly CoverItem[]): CoverGroup[] {
  const by = new Map<string, CoverItem[]>()
  for (const i of items) by.set(parentOf(i.path), [...(by.get(parentOf(i.path)) ?? []), i])
  return [...by.entries()]
    .map(([parent, is]) => ({ parent, items: is, b: is.reduce((n, i) => n + i.b, 0), o: is.reduce((n, i) => n + i.o, 0) }))
    .sort((x, y) => y.b - x.b || (x.parent < y.parent ? -1 : x.parent > y.parent ? 1 : 0))
}

/** What an action sends for the kept items, and what it leaves out and why.
 *  - assign: folders only — ownership is recorded per folder prefix, so a lone matching file (in a folder
 *    that also holds non-matching files) can't be assigned by itself;
 *  - stage: folders and files, but never a whole bucket (a plan item can't name one).
 *  An item whose kind the server couldn't look up is never sent. */
export interface Targets { prefixes: string[]; b: number; o: number; files: number; buckets: number; unknown: number }
export function actionTargets(items: readonly CoverItem[], scheme: string, action: 'assign' | 'stage'): Targets {
  const t: Targets = { prefixes: [], b: 0, o: 0, files: 0, buckets: 0, unknown: 0 }
  for (const i of items) {
    if (i.kind === null) { t.unknown++; continue }
    if (action === 'assign' && i.kind === 'file') { t.files++; continue }
    if (action === 'stage' && depthOf(i.path) < 2) { t.buckets++; continue }
    t.prefixes.push(i.kind === 'dir' ? `${scheme}${i.path}/` : `${scheme}${i.path}`)
    t.b += i.b
    t.o += i.o
  }
  return t
}

/** `xs` in runs of `n`. */
export const chunks = <T>(xs: readonly T[], n: number): T[][] => Array.from({ length: Math.ceil(xs.length / n) }, (_, i) => xs.slice(i * n, (i + 1) * n))

/** Owner actions per POST (`/api/actions` takes 1–500). */
export const ASSIGN_CHUNK = 500
/** Prefixes per stage POST; the batches are then merged into one (`/api/plans/:id/batches/merge`). */
export const STAGE_CHUNK = 500

/** Assign every prefix in turn, `ASSIGN_CHUNK` per POST; `progress(done, total)` after each. */
export async function assignInBatches<A>(
  prefixes: readonly string[],
  make: (pattern: string) => A,
  post: (actions: A[]) => Promise<unknown>,
  progress: (done: number, total: number) => void = () => {},
): Promise<number> {
  let done = 0
  for (const part of chunks(prefixes, ASSIGN_CHUNK)) {
    await post(part.map(make))
    done += part.length
    progress(done, prefixes.length)
  }
  return done
}

/** Stage every prefix, `STAGE_CHUNK` per POST, then fold the batches into the first so the gesture reads
 *  (and is reviewed) as the one batch it is. `stage` / `merge` are the two API calls. */
export async function stageInBatches(
  prefixes: readonly string[],
  stage: (part: string[]) => Promise<{ plan_id: number; batch_id: number | null }>,
  merge: (planId: number, into: number, ids: number[]) => Promise<unknown>,
  progress: (done: number, total: number) => void = () => {},
): Promise<{ plan_id: number | null; batch_id: number | null; batches: number }> {
  let planId: number | null = null
  const ids: number[] = []
  let done = 0
  for (const part of chunks(prefixes, STAGE_CHUNK)) {
    const r = await stage(part)
    planId = r.plan_id
    if (r.batch_id != null) ids.push(r.batch_id)
    done += part.length
    progress(done, prefixes.length)
  }
  if (planId != null && ids.length > 1) await merge(planId, ids[0], ids.slice(1))
  return { plan_id: planId, batch_id: ids[0] ?? null, batches: ids.length }
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

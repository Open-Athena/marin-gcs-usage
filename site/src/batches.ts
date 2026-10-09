// Large bulk actions in batches (DOM-free: the Functions' tests submit through these too): the APIs take a
// bounded number of prefixes per request, so a gesture over many is several POSTs — and a staging gesture is
// then folded back into one batch.
import type { PlanItem } from '../functions/_lib/plans'

/** What an action names: a folder prefix or one exact object (`functions/_lib/plans.ts`). */
export type { ItemKind, PlanItem } from '../functions/_lib/plans'


/** `xs` in runs of `n`. */
export const chunks = <T>(xs: readonly T[], n: number): T[][] => Array.from({ length: Math.ceil(xs.length / n) }, (_, i) => xs.slice(i * n, (i + 1) * n))

/** Owner actions per POST (`/api/actions` takes 1–500). */
export const ASSIGN_CHUNK = 500
/** Prefixes per stage POST; the batches are then merged into one (`/api/plans/:id/batches/merge`). */
export const STAGE_CHUNK = 500

/** Assign every item in turn, `ASSIGN_CHUNK` per POST; `progress(done, total)` after each. */
export async function assignInBatches<T, A>(
  prefixes: readonly T[],
  make: (item: T) => A,
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
export async function stageInBatches<T>(
  prefixes: readonly T[],
  stage: (part: T[]) => Promise<{ plan_id: number; batch_id: number | null }>,
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

/** One `/api/plans/stage` answer (`plans.ts` `StageResult`). */
export interface Staged {
  plan_id: number
  batch_id: number
  staged: string[]
  /** The staged items that are exact objects (absent: none). */
  staged_objects?: string[]
  covered: string[]
  absorbed: string[]
  as_of: string | null
}

/** One stage POST's body for `items`: folders in `prefixes`, exact keys in `objects` (sent only when there
 *  are any — a folders-only gesture posts what it always did). The kind is which list an item rides in. */
export const stageBody = (items: readonly PlanItem[], note: string | undefined, as_of: string | null | undefined) => {
  const objects = items.filter(i => i.kind === 'object').map(i => i.key)
  return { prefixes: items.filter(i => i.kind === 'prefix').map(i => i.key), ...(objects.length ? { objects } : {}), note: note?.trim() || undefined, as_of }
}

/** `stageInBatches` over the plan API: `post(url, method, body)` is the HTTP call. One result for the whole
 *  gesture (the first batch, every part's items). A bare string is a folder prefix. */
export async function stageMany(
  items: readonly (string | PlanItem)[],
  note: string | undefined,
  as_of: string | null | undefined,
  post: <T>(url: string, method: string, body: unknown) => Promise<T>,
): Promise<Staged> {
  const all: Staged[] = []
  const its = items.map((i): PlanItem => (typeof i === 'string' ? { key: i, kind: 'prefix' } : i))
  const r = await stageInBatches(its,
    async part => { const x = await post<Staged>('/api/plans/stage', 'POST', stageBody(part, note, as_of)); all.push(x); return x },
    (planId, into, ids) => post(`/api/plans/${planId}/batches/merge`, 'POST', { into, ids }))
  const objects = all.flatMap(x => x.staged_objects ?? [])
  return {
    plan_id: r.plan_id ?? all[0]?.plan_id, batch_id: r.batch_id ?? all[0]?.batch_id,
    staged: all.flatMap(x => x.staged), ...(objects.length ? { staged_objects: objects } : {}),
    covered: all.flatMap(x => x.covered), absorbed: all.flatMap(x => x.absorbed), as_of: all[0]?.as_of ?? null,
  }
}

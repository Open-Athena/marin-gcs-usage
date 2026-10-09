// Large bulk actions in batches (DOM-free: the Functions' tests submit through these too): the APIs take a
// bounded number of prefixes per request, so a gesture over many is several POSTs — and a staging gesture is
// then folded back into one batch.

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

/** One `/api/plans/stage` answer (`plans.ts` `StageResult`). */
export interface Staged { plan_id: number; batch_id: number; staged: string[]; covered: string[]; absorbed: string[]; as_of: string | null }

/** `stageInBatches` over the plan API: `post(url, method, body)` is the HTTP call. One result for the whole
 *  gesture (the first batch, every part's prefixes). */
export async function stageMany(
  prefixes: string[],
  note: string | undefined,
  as_of: string | null | undefined,
  post: <T>(url: string, method: string, body: unknown) => Promise<T>,
): Promise<Staged> {
  const all: Staged[] = []
  const r = await stageInBatches(prefixes,
    async part => { const x = await post<Staged>('/api/plans/stage', 'POST', { prefixes: part, note: note?.trim() || undefined, as_of }); all.push(x); return x },
    (planId, into, ids) => post(`/api/plans/${planId}/batches/merge`, 'POST', { into, ids }))
  return {
    plan_id: r.plan_id ?? all[0]?.plan_id, batch_id: r.batch_id ?? all[0]?.batch_id,
    staged: all.flatMap(x => x.staged), covered: all.flatMap(x => x.covered), absorbed: all.flatMap(x => x.absorbed), as_of: all[0]?.as_of ?? null,
  }
}

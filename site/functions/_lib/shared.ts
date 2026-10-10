/** Per-isolate memos of promises shared across requests — with a watchdog.
 *
 * Workers freeze a cancelled request's pending promises (a viewer navigating
 * away mid-compute): they never settle and never reject, so a plain
 * `Map<key, Promise>` can hold an entry nothing will ever resolve, and every
 * later request that awaits it is cut off by the runtime as "hung" until the
 * entry is evicted. Each caller therefore races the shared promise against a
 * timer of its own (a pending timer is progress, so the runtime waits); on
 * timeout it evicts the entry and computes afresh in its own context, where a
 * second stall is a real failure. Holders of a real stake (a lock, a slot)
 * must never be shared this way at all — there is no release to wait for. */
/** `joinMs`: how long a caller waits on an entry another request started (default `waitMs`). A computation that
 *  may legitimately run long (an interval store's handle: footers and runs, cold) takes a long `waitMs` for its
 *  own context but a short `joinMs`, so a request that joined a frozen entry recomputes within seconds instead of
 *  sitting out the long bound, past the edge's own (~125 s with no headers, gcs 2026-10-10). */
export async function shared<T>(map: Map<string, Promise<T>>, key: string, make: () => Promise<T>, waitMs: number, joinMs = waitMs): Promise<T> {
  const start = (): Promise<T> => {
    const q = make()
    map.set(key, q)
    q.catch(() => { if (map.get(key) === q) map.delete(key) })
    return q
  }
  const held = map.get(key)
  const p = held ?? start()
  const race = (q: Promise<T>, ms: number): Promise<T> => {
    let timer: ReturnType<typeof setTimeout> | null = null
    const stall = new Promise<never>((_, rej) => { timer = setTimeout(() => rej(new Stall(key, ms)), ms) })
    return Promise.race([q, stall]).finally(() => clearTimeout(timer))
  }
  try {
    return await race(p, held ? joinMs : waitMs)
  } catch (e) {
    if (!(e instanceof Stall)) throw e
    if (map.get(key) === p) map.delete(key)
    const q = start()
    try {
      return await race(q, waitMs)
    } catch (e2) {
      if (e2 instanceof Stall && map.get(key) === q) map.delete(key)
      throw e2
    }
  }
}

/** What `within` yields for a promise still pending at its deadline. */
export const STALLED: unique symbol = Symbol('stalled')

/** `p`'s value, or `STALLED` if it hasn't settled within `ms` (nothing is cancelled; a rejection propagates). The
 *  pending timer is progress to the Workers runtime, so a wait on another request's frozen promise ends here. */
export async function within<T>(p: Promise<T>, ms: number): Promise<T | typeof STALLED> {
  let timer: ReturnType<typeof setTimeout> | null = null
  const stall = new Promise<typeof STALLED>(res => { timer = setTimeout(() => res(STALLED), ms) })
  try {
    return await Promise.race([p, stall])
  } finally {
    clearTimeout(timer!)
  }
}

/** A computation shared only while in flight: a caller finding `key` pending waits on it up to `joinMs`, then
 *  evicts it and runs `make` itself — in its own context, unbounded (it's that request's own I/O). The entry leaves
 *  the map once it settles, either way. Unlike `shared`, which memoizes: for work whose result is cached apart. */
export async function inFlight<T>(map: Map<string, Promise<T>>, key: string, make: () => Promise<T>, joinMs: number): Promise<T> {
  const held = map.get(key)
  if (held) {
    const got = await within(held, joinMs)
    if (got !== STALLED) return got
    if (map.get(key) === held) map.delete(key)
  }
  const p = make()
  map.set(key, p)
  try {
    return await p
  } finally {
    if (map.get(key) === p) map.delete(key)
  }
}

class Stall extends Error {
  constructor(key: string, ms: number) {
    super(`shared ${key}: no result after ${ms} ms`)
  }
}

/** Where this deployment's snapshots live in the data bucket: `snapshots/`
 * for the default (GCS) store, `snapshots/<SNAPSHOTS_SUBDIR>/` for a store
 * published under a named subdir (cw-s3: `cw`). Every Function that lists or
 * reads snapshot dirs goes through this, so a deployment can't list another
 * store's scans (the 2026-09-16 preview charted the GCS fleet on cw). */
export const snapshotsPrefix = (env: { SNAPSHOTS_SUBDIR?: string }): string => {
  const sub = (env.SNAPSHOTS_SUBDIR ?? '').replace(/^\/+|\/+$/g, '')
  return sub ? `snapshots/${sub}/` : 'snapshots/'
}

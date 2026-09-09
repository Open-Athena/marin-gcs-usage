/**
 * Index extras (specs/index-extras.md): the per-scan sidecars beside the
 * index tiers — `ck.json` (checkpoint-shaped dirs, decided over each dir's
 * FULL child list) and `attr.json` (every attributing prefix →
 * [user, source, evidence]). Fetched once per (scan, generation) per isolate
 * and shared across requests; a scan without them (older generations, until
 * `gcs-usage index-extras` backfills) yields `null` and every consumer keeps
 * today's behaviour.
 */
import type { Env } from './auth.js'
import { makeStore } from './index.js'
import { shared } from './shared.js'

export type Provenance = [source: string, evidence: string | null, prefix: string]

export interface Extras {
  /** Checkpoint-shaped dir paths (index keys). */
  ck: ReadonlySet<string>
  /** Attributing prefix → [user, source, evidence]. */
  attr: ReadonlyMap<string, [string, string | null, string | null]>
  /** The provenance of `user`'s bytes under `path`: the deepest attributing
   *  ancestor-or-self whose user is `user`, or null. */
  provenance(path: string, user: string): Provenance | null
}

const memo = new Map<string, Promise<Extras | null>>()
const TTL_MS = 10 * 60_000

async function readJson(env: Env, key: string): Promise<unknown | null> {
  try {
    const { bytes } = await makeStore(env).get(key)
    return JSON.parse(new TextDecoder().decode(bytes))
  } catch {
    return null
  }
}

/** The scan's extras, or null when the generation has none. */
export async function extras(env: Env, date: string): Promise<Extras | null> {
  if (!env.DB) return null
  const r = await env.DB.prepare('SELECT dir FROM index_schema WHERE date = ? AND variant = ?').bind(date, 'path').first<{ dir: string | null }>()
  const dir = r?.dir ?? `listing/${date}`
  return shared(memo, `${date}|${dir}`, async () => {
    const [ckJ, atJ] = await Promise.all([readJson(env, `${dir}/ck.json`), readJson(env, `${dir}/attr.json`)])
    if (!ckJ && !atJ) return null
    const ck = new Set<string>(((ckJ as { ck?: string[] } | null)?.ck) ?? [])
    const attr = new Map<string, [string, string | null, string | null]>(
      Object.entries(((atJ as { attr?: Record<string, [string, string | null, string | null]> } | null)?.attr) ?? {}),
    )
    const provenance = (path: string, user: string): Provenance | null => {
      for (let p = path; p; p = p.includes('/') ? p.slice(0, p.lastIndexOf('/')) : '') {
        const hit = attr.get(p)
        if (hit && hit[0] === user) return [hit[1] ?? 'unknown', hit[2] ?? null, p]
      }
      return null
    }
    return { ck, attr, provenance }
  }, TTL_MS)
}

/** The scan an API request names — the Functions side of `src/scanSlug.ts`'s
 * resolver, against the store's index (`index_schema`'s `path` pointers).
 *
 *   ?date=<scan id>   (also `from=`, `to=`) exactly that scan
 *   ?d=<slug>         the latest indexed scan the slug names (`261009` a day,
 *                     `26100912` an hour, `2610091236` a minute — a date-only
 *                     scan's exact slug is its midnight, `2610090000`; legacy
 *                     and ISO spellings too)
 *
 * A scan id or slug naming no indexed scan is a 404 `{error: "no scan matches
 * d=26100912"}` — never the nearest or the latest scan instead. */
import { decodeScan, isScanId } from '../../src/scanSlug.js'
import { type Env, json } from './auth.js'
import { d1Variant, isPrimary, storeKey } from './stores.js'

/** The latest indexed scan matching `prefix` (exact: only `prefix` itself).
 * Without a D1 there is no index to ask: an exact id passes as given, a slug
 * resolves to nothing. */
export async function indexedScan(env: Env, prefix: string, exact: boolean): Promise<string | null> {
  if (!env.DB) return exact ? prefix : null
  // A decoded prefix is `[0-9T-]` only: no LIKE metacharacters to escape. A
  // date-only id matches as its midnight (`scanSlug.ts` `scanKey`), so its
  // exact slug `YYMMDD0000` (and the hour `YYMMDD00`) finds it.
  const cond = exact ? 'date = ?' : "(date LIKE ? OR (length(date) = 10 AND date || 'T0000' LIKE ?))"
  const args = exact ? [prefix] : [`${prefix}%`, `${prefix}%`]
  const r = isPrimary(env)
    ? await env.DB.prepare(`SELECT MAX(date) AS d FROM index_schema WHERE variant = 'path' AND ${cond}`).bind(...args).first<{ d: string | null }>()
    : await env.DB.prepare(`SELECT MAX(date) AS d FROM index_schema WHERE store = ? AND variant = ? AND ${cond}`).bind(storeKey(env), d1Variant(env, 'path'), ...args).first<{ d: string | null }>()
  return r?.d ?? null
}

/** The 404 a miss answers: `{error: "no scan matches <key>=<value>"}`. */
export const noScan = (key: string, value: string): Response => json({ error: `no scan matches ${key}=${value}` }, 404)

/** The scan `key` names: `date`/`from`/`to` take an exact scan id (absent
 * `date` falls back to a `d=<slug>`); `d` a slug. A missing or malformed
 * exact id is a 400; a miss (an unindexed id, a slug matching nothing or
 * naming no scan) a 404. */
export async function scanArg(env: Env, sp: URLSearchParams, key = 'date'): Promise<string | Response> {
  const raw = sp.get(key)
  if (raw === null && key === 'date' && sp.has('d')) return scanArg(env, sp, 'd')
  if (key === 'd') {
    const prefix = raw ? decodeScan(raw) : undefined
    return (prefix && await indexedScan(env, prefix, false)) || noScan('d', raw ?? '')
  }
  if (!raw || !isScanId(raw)) return json({ error: `${key}=YYYY-MM-DD[THHMM] required` }, 400)
  return (await indexedScan(env, raw, true)) ?? noScan(key, raw)
}

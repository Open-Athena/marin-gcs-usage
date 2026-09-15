/** Two-tier cache for the auth-gated, per-scan-immutable JSON endpoints
 * (`/api/subtree`, `/api/diff`): the colo cache (Workers Cache API) in front
 * of a global KV tier.
 *
 * The client copy is `private` (browsers may keep it; shared proxies must
 * not — the data is behind sign-in). But the Workers Cache API refuses to
 * store a response whose Cache-Control says not to share it: `cache.put`
 * reports that as a 413, which the endpoints never checked, so every diff and
 * subtree was recomputed for every viewer (a repeated 7-day root diff took
 * 21 s, 2026-09-15). The stored copy therefore carries `public, s-maxage`;
 * a hit is rewritten back to `private` on the way out. The scope gate
 * (`requireScope`) runs before `match`, so a hit still only reaches an
 * authorized viewer.
 *
 * The colo cache is per data-center, so the daily job's warm-up
 * (`gcs-usage warm-cache`, run in us-central1) would reach one colo; KV is
 * global, so a warmed key is a hit for the first viewer anywhere. A KV hit
 * back-fills the colo cache. Values are small JSON (≤ ~350 KB); keys are the
 * SHA-256 of the cache key URL (KV keys are capped at 512 bytes). */

const TTL = 86400
const KV_TTL = 30 * 86400
const PRIVATE = `private, max-age=${TTL}`
const JSON_HDR = { 'content-type': 'application/json; charset=utf-8' }

export interface CacheEnv { CACHE_KV?: KVNamespace }

export function cacheKeyFor(ns: string, parts: string): Request {
  return new Request(`https://${ns}.cache/${parts}`)
}

const colo = () => (caches as unknown as { default: Cache }).default

async function kvKey(key: Request): Promise<string> {
  const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(key.url))
  return [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, '0')).join('')
}

const publicRes = (body: string) => new Response(body, { headers: { ...JSON_HDR, 'cache-control': `public, s-maxage=${TTL}` } })
const clientRes = (body: string, tier: string) => new Response(body, { headers: { ...JSON_HDR, 'cache-control': PRIVATE, 'x-cache': tier } })

export async function cacheMatch(env: CacheEnv, key: Request): Promise<Response | null> {
  const hit = await colo().match(key)
  if (hit) {
    const res = new Response(hit.body, hit)
    res.headers.set('cache-control', PRIVATE)
    res.headers.set('x-cache', 'hit')
    return res
  }
  if (!env.CACHE_KV) return null
  const body = await env.CACHE_KV.get(await kvKey(key), 'text')
  if (body == null) return null
  await colo().put(key, publicRes(body))
  return clientRes(body, 'kv')
}

/** Store `body` (already-serialized JSON) in both tiers; return the client response. */
export async function cacheStore(env: CacheEnv, key: Request, body: string): Promise<Response> {
  const puts: Promise<unknown>[] = [colo().put(key, publicRes(body))]
  if (env.CACHE_KV) puts.push(env.CACHE_KV.put(await kvKey(key), body, { expirationTtl: KV_TTL }))
  await Promise.all(puts)
  return clientRes(body, 'miss')
}

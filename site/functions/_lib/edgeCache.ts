/** Colo-cache helpers for the auth-gated, per-scan-immutable JSON endpoints
 * (`/api/subtree`, `/api/diff`).
 *
 * The client copy is `private` (browsers may keep it; shared proxies must
 * not — the data is behind sign-in). But the Workers Cache API refuses to
 * store a response whose Cache-Control says not to share it: `cache.put`
 * reports that as a 413, which the endpoints never checked, so every diff and
 * subtree was recomputed for every viewer (a repeated 7-day root diff took
 * 21 s, 2026-09-15). The stored copy therefore carries `public, s-maxage`;
 * a hit is rewritten back to `private` on the way out. The scope gate
 * (`requireScope`) runs before `match`, so a hit still only reaches an
 * authorized viewer. The cache is per data-center — a global tier (KV) is
 * the follow-up for first views. */

const PRIVATE = (ttl: number) => `private, max-age=${ttl}`

export function cacheKeyFor(ns: string, parts: string): Request {
  return new Request(`https://${ns}.cache/${parts}`)
}

export async function cacheMatch(key: Request, ttl = 86400): Promise<Response | null> {
  const cache = (caches as unknown as { default: Cache }).default
  const hit = await cache.match(key)
  if (!hit) return null
  const res = new Response(hit.body, hit)
  res.headers.set('cache-control', PRIVATE(ttl))
  res.headers.set('x-cache', 'hit')
  return res
}

/** Store `body` (already-serialized JSON) and return the client response. */
export async function cacheStore(key: Request, body: string, ttl = 86400): Promise<Response> {
  const cache = (caches as unknown as { default: Cache }).default
  const headers = { 'content-type': 'application/json; charset=utf-8' }
  await cache.put(key, new Response(body, { headers: { ...headers, 'cache-control': `public, s-maxage=${ttl}` } }))
  return new Response(body, { headers: { ...headers, 'cache-control': PRIVATE(ttl), 'x-cache': 'miss' } })
}

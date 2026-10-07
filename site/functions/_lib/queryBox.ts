/** The Worker's hand-off to the serving box (specs/filter-query-service.md
 * §6 phase 4, specs/ch-store.md §5): with `QUERY_BOX_URL` set, `/api/subtree`,
 * `/api/diff` and `/api/series` ask `dt-cloud serve-query` first (bearer
 * `QUERY_BOX_TOKEN`) and answer with its body, in the same shape; the box
 * declining (409 / 501 / 503), failing, timing out or sending more than the
 * Worker should hold falls back to the Worker's own read. Unset, nothing here
 * runs: today's behaviour, byte for byte.
 *
 * Every response of a deployment with a box says which engine answered:
 * `x-query-engine: box;dur=…` (the box's own header) or
 * `worker;fallback=<why>`. */
import type { Env } from './auth.js'

export interface BoxEnv {
  /** The box's base URL (a tunnel or a private address). Unset = no box. */
  QUERY_BOX_URL?: string
  /** The bearer secret `serve-query` checks (`$QUERY_BOX_TOKEN` on the box). */
  QUERY_BOX_TOKEN?: string
  /** Per-request budget, ms (default `BOX_TIMEOUT_MS`). */
  QUERY_BOX_TIMEOUT_MS?: string
  /** Explicit dev-only opt-in for the separate coarse basename prototype. */
  QUERY_BOX_COARSE?: string
  /** Optional coarse-only endpoint; never enables canonical box routing. */
  QUERY_BOX_COARSE_URL?: string
}

export const BOX_TIMEOUT_MS = 20_000
/** The most of a box body the Worker reads before giving up on it: a filter's
 * every-root lists can run to a gigabyte, more than an isolate holds. */
export const BOX_MAX_BYTES = 64 << 20
/** The box's "ask the Worker" answers: a scan or scope it doesn't hold, a
 * query it can't answer exactly, still loading / its backend down. */
export const BOX_DECLINES = [409, 501, 503]

export type BoxAnswer =
  /** The box answered (200), or refused the request the way the Worker would (400 / 404). */
  | { kind: 'answer'; status: number; body: string; engine: string }
  /** Ask the Worker instead; `why` lands in `x-query-engine`. */
  | { kind: 'fallback'; why: string }

/** Whether this request goes to the box at all: a box is configured, the
 * request is for the primary store, and carries no scope the box declines
 * (a user lens, owner / class pools). */
export function boxFor(env: Env & BoxEnv, url: URL, route: 'canonical' | 'coarse' = 'canonical'): string | null {
  const base = route === 'coarse' ? env.QUERY_BOX_COARSE_URL ?? env.QUERY_BOX_URL : env.QUERY_BOX_URL
  if (!base || env.STORE_KEY) return null
  for (const k of ['lens', 'o', 'cl', 'by']) if (url.searchParams.get(k)) return null
  return base.replace(/\/+$/, '')
}

/** One request to the box: `route` with `params` (the client's query, plus
 * anything the Worker adds). */
export async function askBox(env: Env & BoxEnv, base: string, route: 'subtree' | 'diff' | 'series' | 'coarse', params: URLSearchParams, maxBytes = BOX_MAX_BYTES): Promise<BoxAnswer> {
  const ms = Number(env.QUERY_BOX_TIMEOUT_MS) || BOX_TIMEOUT_MS
  const headers: Record<string, string> = env.QUERY_BOX_TOKEN ? { authorization: `Bearer ${env.QUERY_BOX_TOKEN}` } : {}
  let res: Response
  try {
    res = await fetch(`${base}/api/${route}?${params}`, { headers, signal: AbortSignal.timeout(ms) })
  } catch (e) {
    const name = (e as Error).name
    return { kind: 'fallback', why: name === 'TimeoutError' || name === 'AbortError' ? 'timeout' : 'unreachable' }
  }
  if (BOX_DECLINES.includes(res.status)) {
    await res.body?.cancel()
    return { kind: 'fallback', why: String(res.status) }
  }
  if (res.status !== 200 && res.status !== 400 && res.status !== 404) {
    await res.body?.cancel()
    return { kind: 'fallback', why: String(res.status) }
  }
  const len = Number(res.headers.get('content-length'))
  if (len > maxBytes) {
    await res.body?.cancel()
    return { kind: 'fallback', why: 'too-big' }
  }
  try {
    const body = await readCapped(res, maxBytes)
    if (body == null) return { kind: 'fallback', why: 'too-big' }
    return { kind: 'answer', status: res.status, body, engine: res.headers.get('x-query-engine') ?? 'box' }
  } catch (e) {
    return { kind: 'fallback', why: (e as Error).name === 'TimeoutError' ? 'timeout' : 'read-error' }
  }
}

/** A response body as text, or null past `max` bytes (the read is cancelled). */
async function readCapped(res: Response, max: number): Promise<string | null> {
  if (!res.body) return ''
  const reader = res.body.getReader()
  const chunks: Uint8Array[] = []
  let n = 0
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    n += value.byteLength
    if (n > max) {
      await reader.cancel()
      return null
    }
    chunks.push(value)
  }
  const out = new Uint8Array(n)
  let at = 0
  for (const c of chunks) { out.set(c, at); at += c.byteLength }
  return new TextDecoder().decode(out)
}

/** A plain-text answer with the box's status (400 / 404) and engine. */
export const boxStatus = (a: Extract<BoxAnswer, { kind: 'answer' }>): Response =>
  new Response(a.body, { status: a.status, headers: { 'x-query-engine': a.engine } })

type TreeNode = { n: string; c?: TreeNode[]; us?: [string, number][]; pv?: unknown } & Record<string, unknown>

/** The index extras' provenance (`pv`) on a box tree's nodes, where the
 * Worker's `buildView` puts it: on every named node (not an `(other)` fold)
 * whose top owner has one, keyed `…, m, pv, c` as `buildView` orders them. */
export function withProvenance(tree: TreeNode, root: string, pv: (path: string, user: string) => unknown): TreeNode {
  const walk = (node: TreeNode, p: string): TreeNode => {
    const { c, ...rest } = node
    const top = node.us?.[0]?.[0]
    const got = top ? pv(p, top) : null
    const out: TreeNode = { ...rest, ...(got ? { pv: got } : {}) } as TreeNode
    if (c) out.c = c.map(k => (k.n === '(other)' ? k : walk(k, p === '' ? k.n : `${p}/${k.n}`)))
    return out
  }
  return walk(tree, root)
}

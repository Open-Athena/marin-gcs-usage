// /api/session-log — optional, time-boxed user-action logging (specs/session-log.md):
//
//   GET  /api/session-log                 { enabled, until, reason }   anyone (enabled only for a viewer)
//   POST /api/session-log                 a client batch → 204          viewer, while on
//   GET  /api/session-log/sessions        { sessions: SessionRow[] }    admin (?email= &since= &errors=1 &limit=)
//   GET  /api/session-log/sessions/:sid   { session, events }           admin
//
// Off unless `SESSION_LOG_UNTIL` is set and not past (`parseUntil`); the
// identity is the request's (never the client's say). The retention purge
// (`SESSION_LOG_RETAIN_DAYS`, default 30) rides the config GET and each page
// load's first batch, at most hourly per isolate, whenever the var is set.
import { type Ctx, type Env as AuthEnv, json, requireAdmin, requireViewer } from '../../_lib/auth.js'
import { ingest, listSessions, maybePurge, sessionDetail } from '../../_lib/sessionLog.js'
import { isOn, LIMITS, parseUntil, validateBatch } from '../../_lib/sessionLogShape.js'

type Env = AuthEnv & { SESSION_LOG_UNTIL?: string; SESSION_LOG_RETAIN_DAYS?: string }
type C = Ctx & { env: Env; params?: { path?: string | string[] }; waitUntil?: (p: Promise<unknown>) => void }

/** Per isolate: when the purge last ran. */
export const purgeState = { last: 0 }

const NO_STORE = { 'cache-control': 'no-store' }
const notSetUp = (e: unknown): boolean => /no such table/.test(String((e as Error)?.message))

function purgeLater(ctx: C, now: number): void {
  const db = ctx.env.DB
  if (!db || !ctx.env.SESSION_LOG_UNTIL) return
  const p = maybePurge(db, ctx.env.SESSION_LOG_RETAIN_DAYS, now, purgeState).catch(e => console.log(`session-log purge failed: ${(e as Error).message}`))
  if (ctx.waitUntil) ctx.waitUntil(p)
}

async function config(ctx: C): Promise<Response> {
  const now = Date.now()
  const u = parseUntil(ctx.env.SESSION_LOG_UNTIL, now)
  purgeLater(ctx, now)
  if (!isOn(u)) return json({ enabled: false, until: u.until, reason: u.reason }, 200, NO_STORE)
  if (!ctx.env.DB) return json({ enabled: false, until: u.until, reason: 'no D1 bound' }, 200, NO_STORE)
  const id = await requireViewer(ctx)
  if (id instanceof Response) return json({ enabled: false, until: u.until, reason: 'no viewer identity' }, 200, NO_STORE)
  return json({ enabled: true, until: u.until, reason: null }, 200, NO_STORE)
}

async function post(ctx: C): Promise<Response> {
  const now = Date.now()
  if (!isOn(parseUntil(ctx.env.SESSION_LOG_UNTIL, now))) return json({ error: 'session log is off' }, 503)
  const id = await requireViewer(ctx)
  if (id instanceof Response) return id
  const db = ctx.env.DB
  if (!db) return json({ error: 'no D1 bound' }, 503)
  if (Number(ctx.request.headers.get('content-length') ?? 0) > LIMITS.bodyBytes) return json({ error: `body over ${LIMITS.bodyBytes} bytes` }, 413)
  const text = await ctx.request.text()
  if (new TextEncoder().encode(text).length > LIMITS.bodyBytes) return json({ error: `body over ${LIMITS.bodyBytes} bytes` }, 413)
  let raw: unknown
  try { raw = JSON.parse(text) } catch { return json({ error: 'body is not JSON' }, 400) }
  const v = validateBatch(raw)
  if ('error' in v) return json({ error: v.error }, v.status)
  let r
  try {
    r = await ingest(db, { email: id.email, via: id.via }, ctx.request.headers.get('user-agent'), v.batch, Math.floor(now / 1000))
  } catch (e) {
    if (notSetUp(e)) return json({ error: 'session log is not set up on this deployment (no session_log table)' }, 503)
    throw e
  }
  if (v.batch.seq === 0) purgeLater(ctx, now)
  if (r === 'capped') return json({ error: `session over ${LIMITS.sessionEvents} events` }, 429)
  return new Response(null, { status: 204 })
}

async function admin(ctx: C, rest: string): Promise<Response> {
  const id = await requireAdmin(ctx)
  if (id instanceof Response) return id
  const db = ctx.env.DB
  if (!db) return json({ error: 'no D1 bound' }, 503)
  const q = new URL(ctx.request.url).searchParams
  try {
    if (rest === 'sessions') {
      const since = q.get('since')
      const sinceMs = since ? Date.parse(since) : NaN
      const limit = Number(q.get('limit'))
      const sessions = await listSessions(db, {
        email: q.get('email') || undefined,
        since: Number.isFinite(sinceMs) ? sinceMs : undefined,
        errors: q.get('errors') === '1',
        limit: Number.isFinite(limit) && limit > 0 ? limit : undefined,
      })
      return json({ sessions }, 200, NO_STORE)
    }
    const sid = rest.slice('sessions/'.length)
    const d = await sessionDetail(db, sid)
    return d ? json(d, 200, NO_STORE) : json({ error: `no session ${sid}` }, 404)
  } catch (e) {
    if (notSetUp(e)) return json({ error: 'session log is not set up on this deployment (no session_log table)' }, 404)
    throw e
  }
}

export const onRequest = async (ctx: C): Promise<Response> => {
  const p = ctx.params?.path
  const rest = decodeURIComponent((Array.isArray(p) ? p.join('/') : p) ?? '')
  const m = ctx.request.method
  if (rest === '') {
    if (m === 'GET') return config(ctx)
    if (m === 'POST') return post(ctx)
    return json({ error: 'method not allowed' }, 405)
  }
  if (rest === 'sessions' || /^sessions\/[A-Za-z0-9_-]+$/.test(rest)) {
    return m === 'GET' ? admin(ctx, rest) : json({ error: 'method not allowed' }, 405)
  }
  return json({ error: 'not found' }, 404)
}

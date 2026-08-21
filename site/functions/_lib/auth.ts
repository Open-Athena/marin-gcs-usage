/**
 * One gate for the whole site (`@open-athena/auth`, Tier 2), plus the
 * transition shim that keeps both hosts working while the CF Access topology
 * moves from "edge-gate the whole host" to "Access is an SSO IdP on
 * `/auth/sso`; the app gate authorizes everything else".
 *
 * Identity sources, in the order `requireScope` tries them:
 *
 *  1. `Cf-Access-Jwt-Assertion` header — present on any request that came
 *     through a CF Access edge gate. This is the *entire* auth story today
 *     (gcs.oa.dev pre-cutover) and stays the story on cw-s3.oa.dev, whose
 *     whole-host OA-only Access app is intentionally untouched (CW data is
 *     not for Stanford/external viewers).
 *  2. The app session cookie / `Authorization: Bearer` / `?key=` — the
 *     `@open-athena/auth` gate, backed by D1. This is what makes named share
 *     links ("anyone with the link can view") possible: minted links redeem
 *     for a session that re-joins its grant row every request, so revocation
 *     is instant.
 *
 * Scopes: staff (`@openathena.ai`) get everything; anyone else who made it
 * through an Access gate (the Stanford whitelist) gets `gcs` only — same for
 * email sessions minted at `/auth/sso`, whose scopes re-derive from
 * `scopesFor` on every request. Grant sessions carry the scopes they were
 * minted with (normally just `gcs`).
 */
import { type Auth, createGate, type Gate, hasScope } from '@open-athena/auth'
import { verifyAccessJwt } from '@open-athena/auth/cf-access'
import { d1AuditSink, d1GrantStore, d1RequestStore } from '@open-athena/auth/d1'
import type { D1Database } from '@cloudflare/workers-types'

export interface Env {
  DB?: D1Database
  SESSION_SECRET?: string
  ACCESS_TEAM_DOMAIN?: string
  /** AUD tags of the Access apps whose edge JWTs we accept (gcs + cw). */
  ACCESS_AUD?: string
  ACCESS_AUD_CW?: string
  STAFF_DOMAIN?: string
  GCS_HMAC_KEY_ID: string
  GCS_HMAC_SECRET: string
}

export interface Ctx {
  request: Request
  env: Env
}

export const GCS_SCOPE = 'gcs'
export const CW_SCOPE = 'cw'
export const ADMIN_SCOPE = 'admin'
export const REQUESTS_SCOPE = 'requests'

export const TEAM_DOMAIN = 'https://openathena-ai-pages.cloudflareaccess.com'

const staffDomain = (env: Env) => env.STAFF_DOMAIN ?? 'openathena.ai'

/**
 * Email → scopes. Staff get everything; anyone else must be in the D1
 * `allowed_emails` table (the app-owned whitelist — see /admin) to get the
 * base `gcs` scope. Email sessions re-derive scopes here on every request,
 * so removing a row de-authorizes existing sessions on their next request.
 * If the DB isn't bound (local dev), non-staff fall back to allowed — the
 * CF Access edge gate is the enforcement in that configuration.
 */
export const scopesFor = (env: Env) => async (email: string): Promise<string[] | null> => {
  if (email.endsWith(`@${staffDomain(env)}`)) return [GCS_SCOPE, CW_SCOPE, ADMIN_SCOPE, REQUESTS_SCOPE]
  if (!env.DB) return [GCS_SCOPE]
  const row = await env.DB.prepare('SELECT email FROM allowed_emails WHERE email = ?')
    .bind(email.toLowerCase()).first()
  return row ? [GCS_SCOPE] : null
}

export function gateFor(env: Env): Gate | null {
  if (!env.DB || !env.SESSION_SECRET) return null
  return createGate({
    store: d1GrantStore(env.DB),
    requests: d1RequestStore(env.DB),
    audit: d1AuditSink(env.DB),
    secret: env.SESSION_SECRET,
    policy: scopesFor(env),
  })
}

/** Identity + scopes however the request proved them; null if it didn't. */
export interface Identity {
  email: string | null
  /** Grant display name, for grant sessions with no email. */
  name: string | null
  scopes: string[]
  via: 'edge' | 'session' | 'grant'
}

async function edgeIdentity(req: Request, env: Env): Promise<Identity | null> {
  const jwt = req.headers.get('Cf-Access-Jwt-Assertion')
  if (!jwt) return null
  const teamDomain = env.ACCESS_TEAM_DOMAIN ?? TEAM_DOMAIN
  for (const aud of [env.ACCESS_AUD, env.ACCESS_AUD_CW]) {
    const email = await verifyAccessJwt(jwt, teamDomain, aud)
    if (email) {
      const scopes = await scopesFor(env)(email)
      if (!scopes) return null
      return { email, name: null, scopes, via: 'edge' }
    }
  }
  return null
}

function authIdentity(auth: Auth): Identity {
  if (auth.kind === 'sso') return { email: auth.email, name: null, scopes: auth.scopes, via: 'session' }
  return { email: auth.grant.email ?? null, name: auth.grant.name ?? null, scopes: auth.scopes, via: 'grant' }
}

export async function identify(ctx: Ctx): Promise<Identity | null> {
  const edge = await edgeIdentity(ctx.request, ctx.env)
  if (edge) return edge
  const gate = gateFor(ctx.env)
  if (!gate) return null
  const auth = await gate.authenticate(ctx.request)
  return auth ? authIdentity(auth) : null
}

/** Gate a handler on a scope; 401/403 as JSON. */
export async function requireScope(ctx: Ctx, scope: string): Promise<Identity | Response> {
  const id = await identify(ctx)
  if (!id) return json({ error: 'unauthenticated' }, 401)
  if (!id.scopes.includes(scope) && !id.scopes.includes('*')) return json({ error: 'forbidden' }, 403)
  return id
}

export { hasScope }

export const json = (data: unknown, status = 200, headers: Record<string, string> = {}): Response =>
  new Response(JSON.stringify(data) + '\n', {
    status,
    headers: { 'content-type': 'application/json; charset=utf-8', ...headers },
  })

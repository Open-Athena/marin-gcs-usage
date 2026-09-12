// Auth for cw-s3's admin API (plans + sweep). cw-s3 is whole-host CF Access
// gated (app `4c463052`, OA + coreweave.com domains), so every request that
// reaches a Function has already passed Access at the edge and carries a
// `Cf-Access-Jwt-Assertion` the edge set (client-supplied copies are stripped).
// We read the identity from it; the *admin* gate (who may mark sweep + dispatch
// deletes) is `admin_emails` in D1, with the staff domain implicitly admin so
// there's always a bootstrap admin without seeding a PII row into this public
// repo.
//
// This is leaner than gcs's `@open-athena/auth` (D1 grants / share links / SSO
// sessions): cw-s3 has no share-link or session layer — edge Access is the only
// gate, and this file only distinguishes admin from non-admin viewers.
import type { D1Database } from '@cloudflare/workers-types'

export interface Env {
  DB?: D1Database
  /** AUD tag of the cw Access app (`4c463052`), checked against the edge JWT. */
  ACCESS_AUD?: string
  ACCESS_TEAM_DOMAIN?: string
  /** Domain whose identities are staff (implicit admins). */
  STAFF_DOMAIN?: string
  /** Local dev only (`.dev.vars`): email the localhost dev identity acts as. */
  DEV_EMAIL?: string
  /** Dedicated SA key (Batch submit + actAs the job SA) for the dispatch bridge. */
  GCP_SA_KEY?: string
}

export interface Ctx {
  request: Request
  env: Env
}

export const json = (data: unknown, status = 200, headers: Record<string, string> = {}): Response =>
  new Response(JSON.stringify(data) + '\n', {
    status,
    headers: { 'content-type': 'application/json; charset=utf-8', ...headers },
  })

const staffDomain = (env: Env): string => env.STAFF_DOMAIN ?? 'openathena.ai'

function b64urlJson(seg: string): Record<string, unknown> | null {
  try {
    const b64 = seg.replace(/-/g, '+').replace(/_/g, '/')
    const pad = b64 + "=".repeat((4 - (b64.length % 4)) % 4)
    return JSON.parse(atob(pad)) as Record<string, unknown>
  } catch {
    return null
  }
}

/** Email from the edge Access JWT, checking `aud` + `exp`. The edge already
 * verified the signature (whole-host gate); we trust that and read the claims.
 * (JWKS signature re-verification is a possible hardening; unnecessary while the
 * whole host is Access-gated and the header is edge-set.) */
function edgeEmail(jwt: string, aud: string | undefined): string | null {
  const parts = jwt.split(".")
  if (parts.length !== 3) return null
  const payload = b64urlJson(parts[1])
  if (!payload) return null
  const auds = Array.isArray(payload.aud) ? payload.aud : [payload.aud]
  if (aud && !auds.includes(aud)) return null
  if (typeof payload.exp === "number" && payload.exp < Date.now() / 1000) return null
  return typeof payload.email === "string" ? payload.email : null
}

export interface Identity {
  email: string
  admin: boolean
  via: "edge" | "dev"
}

export async function isAdmin(env: Env, email: string): Promise<boolean> {
  if (email.toLowerCase().endsWith(`@${staffDomain(env)}`)) return true
  if (!env.DB) return false
  const row = await env.DB.prepare("SELECT email FROM admin_emails WHERE email = ?")
    .bind(email.toLowerCase())
    .first()
  return !!row
}

export async function identify(ctx: Ctx): Promise<Identity | null> {
  // Local dev has no CF Access edge, so `wrangler pages dev` requests would 401.
  // Cloudflare routes by real hostname, so this can't be reached in prod.
  const host = new URL(ctx.request.url).hostname
  if (host === "localhost" || host === "127.0.0.1") {
    return { email: ctx.env.DEV_EMAIL ?? "dev@example.test", admin: true, via: "dev" }
  }
  const jwt = ctx.request.headers.get("Cf-Access-Jwt-Assertion")
  if (!jwt) return null
  const email = edgeEmail(jwt, ctx.env.ACCESS_AUD)
  if (!email) return null
  return { email, admin: await isAdmin(ctx.env, email), via: "edge" }
}

/** Any authenticated viewer (reads). */
export async function requireViewer(ctx: Ctx): Promise<Identity | Response> {
  const id = await identify(ctx)
  return id ?? json({ error: "unauthenticated" }, 401)
}

/** An admin (plan writes + sweep dispatch). */
export async function requireAdmin(ctx: Ctx): Promise<Identity | Response> {
  const id = await identify(ctx)
  if (!id) return json({ error: "unauthenticated" }, 401)
  if (!id.admin) return json({ error: "forbidden (admin only)" }, 403)
  return id
}

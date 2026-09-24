// Identity plumbing (@open-athena/auth): where whoami comes from, per host.
//
// gcs.oa.dev is public shell + app-gated data — identity is the app session
// (`/api/auth/whoami`), minted at `/auth/sso` (CF Access as SSO IdP) or by
// redeeming a `?key=` share link.
import { displayName, useForgetWhoami, useWhoami, type Whoami, type WhoamiSource } from '@open-athena/auth/react'

// Deployment seam (specs/denovo-factor.md): the whoami source is a build-time
// flag. `edge` = the whole host sits behind a CF Access gate (cw-s3.oa.dev:
// `/cdn-cgi/access/get-identity`, sign-in bounces through `/login`); `app`
// (default) = the app session (`/api/auth/whoami`, minted at `/auth/sso`).
export const AUTH_MODE: 'app' | 'edge' = import.meta.env.VITE_AUTH_MODE === 'edge' ? 'edge' : 'app'
export const WHOAMI_SOURCE: WhoamiSource = { kind: AUTH_MODE }

// `?wall` forces the wall in dev (which otherwise short-circuits to authed,
// since neither identity source exists locally). A local session disables the
// stub entirely — dev then exercises the real whoami/scopes path, including
// guest grants. The real `oa_auth` cookie is HttpOnly, so a sign-in minted by
// the local Functions (Google / email-code) is announced by the JS-visible
// `oa_dev_session` marker (functions/_lib/devsession.ts); a cookie forged
// against the local wrangler's SESSION_SECRET via document.cookie also counts.
// Evaluated per render (not once at load) so an in-page code sign-in flips
// the gate without a reload.
const forceWall = new URLSearchParams(window.location.search).has('wall')
const hasLocalSession = (): boolean =>
  document.cookie.includes('oa_auth=') || document.cookie.includes('oa_dev_session=')
export const devIdentity = (): Whoami | null | undefined =>
  import.meta.env.DEV && !hasLocalSession()
    ? (forceWall ? null : { email: import.meta.env.VITE_DEV_EMAIL ?? 'dev@example.test' })
    : undefined

export const signInUrl = (): string =>
  AUTH_MODE === 'edge' ? '/login' : `/auth/sso?next=${encodeURIComponent(window.location.pathname + window.location.search)}`

export interface Ident {
  email: string
  name?: string
  /** A share-link (grant) session, not SSO — the chip shows the grant's own
   *  subject (name + avatar) rather than the owner-registry lookup. */
  guest?: boolean
  /** The grant subject's explicit avatar URL (Slack/GitHub/…), when set. */
  avatar?: string
}

/** Sign out of the app session (POST /api/auth/logout clears the cookie). */
export function useSignOut(): () => void {
  const forget = useForgetWhoami()
  return () => {
    if (AUTH_MODE === 'edge') { forget(); window.location.assign('/cdn-cgi/access/logout'); return }
    void fetch('/api/auth/logout', { method: 'POST', credentials: 'include' }).then(() => {
      forget()
    })
  }
}

/** The header chip's identity: null until (unless) someone is signed in. */
export function useIdent(): Ident | null {
  const { whoami } = useWhoami(WHOAMI_SOURCE, { devIdentity: devIdentity() })
  if (!whoami) return null
  const name = displayName(whoami) ?? undefined
  const email = (whoami as { email?: string | null }).email ?? name ?? 'guest'
  const w = whoami as { kind?: string; subject?: { avatar?: string | null } | null }
  return { email, name, guest: w.kind === 'grant', avatar: w.subject?.avatar ?? undefined }
}

/** Scopes on the current identity, or null when unknown (the dev stub carries
 *  none — the server has full scopes there, so callers treat null as dev-full). */
function useScopes(): string[] | null {
  const { whoami } = useWhoami(WHOAMI_SOURCE, { devIdentity: devIdentity() })
  const sc = (whoami as { scopes?: string[] } | null)?.scopes
  return Array.isArray(sc) ? sc : null
}

/**
 * Marking (keep/sweep/owner) is admin-only — non-admins propose deletions by
 * staging instead (specs/share-link-hardening.md); the server enforces the same.
 */
export function useCanMark(): boolean {
  const scopes = useScopes()
  if (scopes === null) return import.meta.env.DEV
  return scopes.includes('admin') || scopes.includes('*')
}

/**
 * Staging (the opt-in trash proposal) needs the full base scope; a read-only
 * guest link (`gcs:read`) cannot. The server enforces the same via `requireStager`.
 */
export function useCanStage(): boolean {
  const scopes = useScopes()
  if (scopes === null) return import.meta.env.DEV
  return scopes.includes('gcs') || scopes.includes('*')
}

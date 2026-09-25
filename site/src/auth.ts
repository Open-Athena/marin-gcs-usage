// Identity plumbing (@open-athena/auth): where whoami comes from, per host.
//
// gcs.oa.dev is public shell + app-gated data — identity is the app session
// (`/api/auth/whoami`), minted by our own Google OIDC client (`/auth/google`),
// an emailed code (`/auth/email/*`), or by redeeming a `?key=` share link.
import { displayName, useForgetWhoami, useWhoami, type Whoami } from '@open-athena/auth/react'

// `?wall` forces the wall in dev (which otherwise short-circuits to authed,
// since no session exists locally). A local session disables the stub
// entirely — dev then exercises the real whoami/scopes path, including guest
// grants. The real `oa_auth` cookie is HttpOnly, so a sign-in minted by the
// local Functions (Google / email-code) is announced by the JS-visible
// `oa_dev_session` marker (functions/_lib/devsession.ts); a cookie forged
// against the local wrangler's SESSION_SECRET via document.cookie also counts.
// Evaluated per render (not once at load) so an in-page code sign-in flips
// the gate without a reload. The stub carries every scope, matching what the
// Functions grant a localhost request (`DEV_SCOPES` in functions/_lib/auth.ts).
const forceWall = new URLSearchParams(window.location.search).has('wall')
const hasLocalSession = (): boolean =>
  document.cookie.includes('oa_auth=') || document.cookie.includes('oa_dev_session=')
const DEV_WHOAMI: Whoami = {
  kind: 'sso',
  email: import.meta.env.VITE_DEV_EMAIL ?? 'dev@example.test',
  admin: true,
  scopes: ['gcs', 'cw', 'admin', 'requests'],
  subject: null,
}
export const devIdentity = (): Whoami | null | undefined =>
  import.meta.env.DEV && !hasLocalSession() ? (forceWall ? null : DEV_WHOAMI) : undefined

/** Where the inline "sign in" links go: the `/signin` page (Google / emailed
 *  code), returning here after. */
export const signInUrl = (): string =>
  `/signin?next=${encodeURIComponent(window.location.pathname + window.location.search)}`

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
    void fetch('/api/auth/logout', { method: 'POST', credentials: 'include' }).then(() => {
      forget()
    })
  }
}

/** The header chip's identity: null until (unless) someone is signed in. */
export function useIdent(): Ident | null {
  const { whoami } = useWhoami(undefined, { devIdentity: devIdentity() })
  if (!whoami) return null
  const name = displayName(whoami) ?? undefined
  const email = whoami.email ?? name ?? 'guest'
  return { email, name, guest: whoami.kind === 'grant', avatar: whoami.subject?.avatar ?? undefined }
}

/** Scopes on the current identity, or null when nobody is signed in. */
function useScopes(): string[] | null {
  const { whoami } = useWhoami(undefined, { devIdentity: devIdentity() })
  return whoami?.scopes ?? null
}

/**
 * Marking (keep/sweep/owner) is admin-only — non-admins propose deletions by
 * staging instead (specs/share-link-hardening.md); the server enforces the same.
 */
export function useCanMark(): boolean {
  const scopes = useScopes()
  if (scopes === null) return false
  return scopes.includes('admin') || scopes.includes('*')
}

/**
 * Staging (the opt-in trash proposal) needs the full base scope; a read-only
 * guest link (`gcs:read`) cannot. The server enforces the same via `requireStager`.
 */
export function useCanStage(): boolean {
  const scopes = useScopes()
  if (scopes === null) return false
  return scopes.includes('gcs') || scopes.includes('*')
}

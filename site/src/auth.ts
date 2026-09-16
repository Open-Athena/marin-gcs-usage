// Identity plumbing (@open-athena/auth): where whoami comes from, per host.
//
// cw-s3.oa.dev is public shell + edge-gated data — identity is the Cloudflare
// Access session (`/cdn-cgi/access/get-identity`, Tier 1); `/login` bounces
// through Access to mint one.
import { displayName, useForgetWhoami, useWhoami, type Whoami, type WhoamiSource } from '@open-athena/auth/react'

export const WHOAMI_SOURCE: WhoamiSource = { kind: 'edge' }

// `?wall` forces the wall in dev (which otherwise short-circuits to authed,
// since there's no CF Access locally) so it can be eyeballed without a deploy.
const forceWall = new URLSearchParams(window.location.search).has('wall')
export const DEV_IDENTITY: Whoami | null | undefined =
  import.meta.env.DEV
    ? (forceWall ? null : { email: import.meta.env.VITE_DEV_EMAIL ?? 'dev@example.test' })
    : undefined

export const signInUrl = (): string => '/login'

export interface Ident {
  email: string
  name?: string
}

/** Sign out of the Access session (the edge clears the cookie and bounces). */
export function useSignOut(): () => void {
  const forget = useForgetWhoami()
  return () => {
    forget()
    window.location.assign('/cdn-cgi/access/logout')
  }
}

/** The header chip's identity: null until (unless) someone is signed in. */
export function useIdent(): Ident | null {
  const { whoami } = useWhoami(WHOAMI_SOURCE, { devIdentity: DEV_IDENTITY })
  if (!whoami) return null
  const name = displayName(whoami) ?? undefined
  const email = (whoami as { email?: string | null }).email ?? name ?? 'guest'
  return { email, name }
}

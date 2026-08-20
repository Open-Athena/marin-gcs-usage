// Identity plumbing (@open-athena/auth): where whoami comes from, per host.
//
// gcs.oa.dev is public shell + app-gated data — identity is the app session
// (`/api/auth/whoami`), minted at `/auth/sso` (CF Access as SSO IdP) or by
// redeeming a `?key=` share link. cw-* hosts stay whole-host edge-gated by
// their own OA-only Access app, so there identity is still the edge probe.
import { displayName, useWhoami, type Whoami, type WhoamiSource } from '@open-athena/auth/react'

const isCwHost = /^cw[-.]/.test(window.location.hostname)

export const WHOAMI_SOURCE: WhoamiSource = isCwHost ? { kind: 'edge' } : { kind: 'app' }

// `?wall` forces the wall in dev (which otherwise short-circuits to authed,
// since neither identity source exists locally).
const forceWall = new URLSearchParams(window.location.search).has('wall')
export const DEV_IDENTITY: Whoami | null | undefined =
  import.meta.env.DEV ? (forceWall ? null : { email: 'dev@example.test' }) : undefined

export const signInUrl = (): string =>
  `/auth/sso?next=${encodeURIComponent(window.location.pathname + window.location.search)}`

export interface Ident {
  email: string
  name?: string
}

/** The header chip's identity: null until (unless) someone is signed in. */
export function useIdent(): Ident | null {
  const { whoami } = useWhoami(WHOAMI_SOURCE, { devIdentity: DEV_IDENTITY })
  if (!whoami) return null
  const name = displayName(whoami) ?? undefined
  const email = (whoami as { email?: string | null }).email ?? name ?? 'guest'
  return { email, name }
}

import type { ReactNode } from 'react'
import { AuthGate as Gate } from '@open-athena/auth/react'
import { DEV_IDENTITY, signInUrl, WHOAMI_SOURCE } from './auth'

// Gate the human-facing routes on an identity: the app session on gcs.oa.dev
// (minted at /auth/sso, or by a `?key=` share link, which <Gate> redeems
// before probing), the CF Access edge session on cw-* hosts. The static shell
// + og:image stay publicly crawlable for link unfurls either way — crawlers
// read the og: meta from <head> regardless of which body we render.
export function AuthGate({ children }: { children: ReactNode }) {
  return (
    <Gate source={WHOAMI_SOURCE} devIdentity={DEV_IDENTITY} signIn={<LoginWall />}>
      {children}
    </Gate>
  )
}

function LoginWall() {
  return (
    <div className="authwall">
      <div className="card">
        <h1>Marin GCS usage</h1>
        <p>Storage attribution + cleanup across the six <code>marin-*</code> GCS buckets.</p>
        <p className="restrict">Access is limited to marin contributors and invited collaborators.</p>
        <a className="signin" href={signInUrl()}>Sign in</a>
        <p className="signin-how">
          Open Athena accounts use Google sign-in; everyone else picks <b>one-time PIN</b> on the
          next page — a code emailed to any allow-listed address (no Google account needed).
          Invited guests can also use a personal share link.
        </p>
      </div>
    </div>
  )
}

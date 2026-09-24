import type { ReactNode } from 'react'
import { AuthGate as Gate, SignInPanel, useForgetWhoami } from '@open-athena/auth/react'
import { devIdentity, WHOAMI_SOURCE } from './auth'
import { DEFAULT_STORE } from './stores'

// Gate the human-facing routes on an identity: the app session on gcs.oa.dev
// (our own Google OIDC or an emailed code, minted at `/auth/google` /
// `/auth/email/*`, or a `?key=` share link, which <Gate> redeems before
// probing), the CF Access edge session on cw-* hosts. The static shell +
// og:image stay publicly crawlable for link unfurls either way — crawlers read
// the og: meta from <head> regardless of which body we render.
export function AuthGate({ children }: { children: ReactNode }) {
  return (
    <Gate source={WHOAMI_SOURCE} devIdentity={devIdentity()} signIn={<LoginWall />}>
      {children}
    </Gate>
  )
}

// The wall: Google-first (one button, no typing), with an emailed-code fallback
// for the non-Google tail (Yahoo / custom domains that can't Google-auth) and a
// request-access form for anyone the D1 allowlist doesn't yet know. All three
// converge on the same app session; a `?denied=<email>` bounce pre-fills the
// request form with the provider-verified address. See specs/oidc-cutover.md.
function LoginWall() {
  const forget = useForgetWhoami()
  return (
    <div className="authwall">
      <div className="card">
        <h1>{DEFAULT_STORE.title}</h1>
        <p>{DEFAULT_STORE.desc}</p>
        <p className="restrict">{DEFAULT_STORE.wall.restrict}</p>
        <SignInPanel
          googleUrl="/auth/google"
          emailAuth={{ startEndpoint: '/auth/email/start', verifyEndpoint: '/auth/email/code' }}
          requestAccess
          onSignedIn={forget}
          classNames={{ root: 'signin-panel', googleButton: 'signin', divider: 'signin-or' }}
        />
        {DEFAULT_STORE.wall.how && <p className="signin-how">{DEFAULT_STORE.wall.how}</p>}
      </div>
    </div>
  )
}

import type { ReactNode } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { AuthGate as Gate, deniedEmail, RequestAccessForm, SignInPanel, useForgetWhoami } from '@open-athena/auth/react'
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
// for the non-Google tail (Yahoo / custom domains that can't Google-auth). The
// request-access form + how-to prose fold behind a disclosure — they're the
// tail for people the D1 allowlist doesn't yet know, not the wall itself — and
// unfold on a `?denied=<email>` bounce, which also pre-fills the form with the
// provider-verified address. All paths converge on the same app session. See
// specs/oidc-cutover.md.
//
// Inside <Gate> the wall stands in for the page, so signing in just refetches
// whoami (`onSignedIn={forget}`) and Google returns to the current URL. On the
// standalone `/signin` page (`next` given — the inline "sign in" links from a
// guest / expired session land there) both paths return to `next` instead.
function LoginWall({ next, error }: { next?: string; error?: string }) {
  const forget = useForgetWhoami()
  const navigate = useNavigate()
  const denied = deniedEmail()
  return (
    <div className="authwall">
      <div className="card">
        <h1>{DEFAULT_STORE.title}</h1>
        <p>{DEFAULT_STORE.desc}</p>
        <p className="restrict">{DEFAULT_STORE.wall.restrict}</p>
        {error && <p className="signin-error" role="alert">{error}</p>}
        <SignInPanel
          googleUrl={next ? `/auth/google?next=${encodeURIComponent(next)}` : '/auth/google'}
          withNext={!next}
          emailAuth={{ startEndpoint: '/auth/email/start', verifyEndpoint: '/auth/email/code' }}
          onSignedIn={() => { forget(); if (next) navigate(next, { replace: true }) }}
          classNames={{ root: 'signin-panel', googleButton: 'signin', divider: 'signin-or' }}
        />
        <details className="signin-more" open={Boolean(denied)}>
          <summary>{denied ? `${denied} isn't on the list yet — request access` : "Don't have access?"}</summary>
          <div className="signin-panel">
            <RequestAccessForm defaultEmail={denied} />
          </div>
          {DEFAULT_STORE.wall.how && <p className="signin-how">{DEFAULT_STORE.wall.how}</p>}
        </details>
      </div>
    </div>
  )
}

/** `?error=` on `/signin` → what to tell the person. The Functions send the
 *  adapter's reason (`google:<why>`) or `link` for a dead emailed link. */
function signInError(code: string | null): string | undefined {
  if (!code) return undefined
  if (code === 'link') return 'That sign-in link is no longer valid — it may have expired or already been used. Ask for a fresh code below.'
  if (code.startsWith('google:')) {
    const why = code.slice('google:'.length)
    const replay = /state|nonce/.test(why)
    return replay
      ? "Google sign-in didn't complete — the attempt expired or the page was reloaded mid-way. Try again."
      : `Google sign-in didn't complete (${why}). Try again, or use an emailed code.`
  }
  return 'Sign-in didn\'t complete. Try again.'
}

/** `/signin?next=<path>` — the wall as a page, for the inline "sign in" links
 *  (`signInUrl()`): a guest upgrading to a real identity, or a session that
 *  expired mid-page — and where the sign-in Functions land a failure
 *  (`?error=`). Same-origin paths only; anything else goes home. */
export function SignInPage() {
  const params = new URLSearchParams(useLocation().search)
  const raw = params.get('next') ?? '/'
  const next = raw.startsWith('/') && !raw.startsWith('//') ? raw : '/'
  return <LoginWall next={next} error={signInError(params.get('error'))} />
}

import { useEffect, useState } from 'react'
import type { ReactNode } from 'react'

// Gate the human-facing routes behind a CF Access identity check. Now that
// Access gates only the data API (`/data*`, `/v1*`, `/login`) — so the static
// shell + og:image are publicly crawlable for link unfurls — the "members only"
// UX moves here: probe `/cdn-cgi/access/get-identity` up front and, if there's
// no session, show a login wall instead of letting the (still edge-gated) data
// fetches fail silently. Crawlers read the og: meta from <head> regardless of
// which body we render, so unfurls are unaffected.
type St = 'checking' | 'authed' | 'anon'

// `?wall` forces the wall in dev (which otherwise short-circuits to authed,
// since there's no CF Access locally) so it can be eyeballed without a deploy.
const forceWall = new URLSearchParams(window.location.search).has('wall')
const devAuthed = import.meta.env.DEV && !forceWall

export function AuthGate({ children }: { children: ReactNode }) {
  const [st, setSt] = useState<St>(devAuthed ? 'authed' : 'checking')

  useEffect(() => {
    if (devAuthed) return
    let cancel = false
    void fetch('/cdn-cgi/access/get-identity')
      .then(r => (r.ok ? r.json() : null))
      .then((d: { email?: string } | null) => !cancel && setSt(d?.email ? 'authed' : 'anon'))
      .catch(() => !cancel && setSt('anon'))
    return () => { cancel = true }
  }, [])

  if (st === 'checking') return null // brief same-origin probe; avoids a wall flash
  if (st === 'anon') return <LoginWall />
  return <>{children}</>
}

function LoginWall() {
  return (
    <div className="authwall">
      <div className="card">
        <h1>Marin GCS usage</h1>
        <p>Per-user storage attribution across the six <code>marin-*</code> GCS buckets.</p>
        <p className="restrict">This dashboard is restricted to Open Athena members.</p>
        <a className="signin" href="/login">Sign in with Open Athena</a>
      </div>
    </div>
  )
}

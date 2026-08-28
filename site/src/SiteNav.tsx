import { useState } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { Avatar } from './Avatar'
import { useCanMark, useIdent, useSignOut } from './auth'
import { IDENTITIES } from './identities.gen'
import { useMyUser, useUserEmails } from './sweep'
import TokenModal from './TokenModal'
import { Tooltip } from './Tooltip'
import { ghHandle, shortName } from './UserChip'

// Shared page chrome: cross-page nav links + the signed-in identity chip. The
// home page embeds it `inline` in its own header row (its h1 + store switcher
// lead); every other page renders the full bar (brand link + links + chip), so
// "where am I / who am I" reads the same everywhere.
export function SiteNav({ inline = false }: { inline?: boolean }) {
  const { pathname } = useLocation()
  const ident = useIdent()
  const canMark = useCanMark()
  const signOut = useSignOut()
  // Mark-related pages are GCS-only (CoreWeave is out of the sweep).
  const markLinks = canMark && !pathname.startsWith('/cw')
  const myUser = useMyUser(ident?.email, canMark)
  const emails = useUserEmails(canMark)
  const [tokenOpen, setTokenOpen] = useState(false)
  const link = (to: string, label: string) => {
    const here = to === '/' ? pathname === '/' : pathname === to || pathname.startsWith(to + '/')
    return <Link className="nav-files" to={to} aria-current={here ? 'page' : undefined}>{label}</Link>
  }
  const body = (
    <>
      {tokenOpen && <TokenModal onClose={() => setTokenOpen(false)} />}
      {!inline && <Link className="brand" to="/">Marin GCS usage</Link>}
      <span className="nav-links">
        {link('/files', 'Browse scans →')}
        {markLinks && link('/users', 'Users →')}
        {markLinks && link('/marks', 'Recent marks →')}
      </span>
      {ident && (
        <div className="whoami">
          <Tooltip content={<WhoamiTip email={ident.email} name={ident.name} user={myUser} emails={emails} />}>
            <span className="has-tt" style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
              <Avatar github={ghHandle(ident.email)} name={ident.name || ident.email} size={22} />
              <span className="email">{ident.name || shortName(ident.email)}</span>
            </span>
          </Tooltip>
          {canMark && (
            <button className="token-btn" type="button" onClick={() => setTokenOpen(true)} title="Personal token for agents / CLI">
              token
            </button>
          )}
          <button className="logout" type="button" onClick={signOut}>log out</button>
        </div>
      )}
    </>
  )
  return inline ? body : <div className="hrow site-nav">{body}</div>
}

// Identity chip tooltip: which sign-in email this session is, which attribution
// user it maps to, and that user's other known handles/emails — so signing in
// via an alternate address (personal Google, OTP) is legible as "still you".
function WhoamiTip({ email, name, user, emails }: {
  email: string
  name?: string
  user: string | null
  emails?: Record<string, string>
}) {
  const rec = user ? IDENTITIES[user] : undefined
  const aliases = user ? Object.keys(IDENTITIES).filter(k => k !== user && IDENTITIES[k].u === user) : []
  const others = user && emails ? Object.keys(emails).filter(e => emails[e] === user && e !== email.toLowerCase()) : []
  return (
    <div className="whoami-tt">
      <div><b>{name && name !== email ? name : email}</b>{name && name !== email && <> · {email}</>}</div>
      {user ? (
        <>
          <div>attribution user: <Link to={`/user/${user}`}><code>{user}</code></Link>{rec?.team && <> · {rec.team}</>}</div>
          {rec?.github && <div>GitHub: <a href={`https://github.com/${rec.github}`} target="_blank" rel="noreferrer">{rec.github}</a></div>}
          {aliases.length > 0 && <div>aliases: {aliases.map(a => <code key={a} style={{ marginRight: 4 }}>{a}</code>)}</div>}
          {others.length > 0 && <div>also signs in as: {others.map(e => <code key={e} style={{ marginRight: 4 }}>{e}</code>)}</div>}
        </>
      ) : (
        <div>not mapped to an attribution user — "My files" won't resolve; ping Ryan.</div>
      )}
    </div>
  )
}

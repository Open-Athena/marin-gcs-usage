import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router-dom'

// Share-link console (staff-only; the backend enforces the `admin` scope on
// every /api/auth/grants route — this page just renders the 403 politely).
// Mint a named link, copy it exactly once (the raw token is never shown
// again), and revoke it to kill every session it ever minted, instantly.

interface Grant {
  id: string
  name: string | null
  note: string | null
  email: string | null
  scopes: string[]
  maxRedeems: number | null
  redeems: number
  expiresAt: number | null
  createdAt: number
  createdBy: string
  revokedAt: number | null
  lastUsedAt: number | null
}

const fmtTs = (ts: number | null): string => (ts ? new Date(ts * 1000).toLocaleString() : '—')

const linkFor = (token: string): string => `${window.location.origin}/?key=${token}`

export function AdminPage() {
  const qc = useQueryClient()
  const [name, setName] = useState('')
  const [note, setNote] = useState('')
  const [days, setDays] = useState('30')
  const [minted, setMinted] = useState<{ name: string | null; url: string } | null>(null)

  const grantsQ = useQuery<{ grants: Grant[] }, Error>({
    queryKey: ['auth', 'grants'],
    retry: false,
    queryFn: async () => {
      const r = await fetch('/api/auth/grants', { credentials: 'include' })
      if (r.status === 401 || r.status === 403) throw new Error('staff only')
      if (!r.ok) throw new Error(`grants: ${r.status}`)
      return r.json()
    },
  })

  const mint = useMutation({
    mutationFn: async () => {
      const expiresInS = days.trim() ? Number(days) * 86400 : null
      const r = await fetch('/api/auth/grants', {
        method: 'POST',
        credentials: 'include',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ name: name.trim() || null, note: note.trim() || null, scopes: ['gcs'], expiresInS }),
      })
      if (!r.ok) throw new Error(`mint failed: ${r.status}`)
      return r.json() as Promise<{ grant: Grant; token: string }>
    },
    onSuccess: ({ grant, token }) => {
      setMinted({ name: grant.name, url: linkFor(token) })
      setName('')
      setNote('')
      void qc.invalidateQueries({ queryKey: ['auth', 'grants'] })
    },
  })

  const revoke = useMutation({
    mutationFn: async (id: string) => {
      const r = await fetch(`/api/auth/grants/${id}/revoke`, { method: 'POST', credentials: 'include' })
      if (!r.ok) throw new Error(`revoke failed: ${r.status}`)
    },
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['auth', 'grants'] }),
  })

  if (grantsQ.error) {
    return (
      <div className="admin-page">
        <h1>Share links</h1>
        <p>{grantsQ.error.message === 'staff only' ? 'This console is staff-only.' : grantsQ.error.message}</p>
        <p><Link to="/">← back</Link></p>
      </div>
    )
  }

  const grants = grantsQ.data?.grants ?? []
  return (
    <div className="admin-page">
      <h1>Share links</h1>
      <p>
        Named, revocable view links for people outside the SSO/whitelist set. The raw link is shown{' '}
        <strong>once</strong>, at mint time; revoking kills every session it minted, on their next request.{' '}
        <Link to="/">← back to the dashboard</Link>
      </p>
      <form
        className="mint"
        onSubmit={e => {
          e.preventDefault()
          mint.mutate()
        }}
      >
        <input value={name} onChange={e => setName(e.target.value)} placeholder="Who it's for — e.g. Jane Doe (Stanford)" required />
        <input value={note} onChange={e => setNote(e.target.value)} placeholder="Why (optional)" />
        <label>
          expires in{' '}
          <input className="days" value={days} onChange={e => setDays(e.target.value)} inputMode="numeric" size={4} /> days
          {' '}(blank = never)
        </label>
        <button type="submit" disabled={mint.isPending}>Mint link</button>
        {mint.error && <span className="err">{mint.error.message}</span>}
      </form>
      {minted && (
        <div className="minted">
          <p>
            Link for <strong>{minted.name ?? 'unnamed'}</strong> — copy it now; it won't be shown again:
          </p>
          <div className="token-row">
            <code>{minted.url}</code>
            <button type="button" onClick={() => void navigator.clipboard.writeText(minted.url)}>copy</button>
          </div>
        </div>
      )}
      <table className="grants">
        <thead>
          <tr>
            <th>name</th><th>note</th><th>scopes</th><th>redeems</th><th>last used</th><th>expires</th><th>created</th><th></th>
          </tr>
        </thead>
        <tbody>
          {grants.map(g => (
            <tr key={g.id} className={g.revokedAt ? 'revoked' : ''}>
              <td>{g.name ?? <em>unnamed</em>}</td>
              <td>{g.note}</td>
              <td>{g.scopes.join(' ')}</td>
              <td>{g.redeems}{g.maxRedeems != null ? `/${g.maxRedeems}` : ''}</td>
              <td>{fmtTs(g.lastUsedAt)}</td>
              <td>{fmtTs(g.expiresAt)}</td>
              <td title={`by ${g.createdBy}`}>{fmtTs(g.createdAt)}</td>
              <td>
                {g.revokedAt
                  ? <span className="revoked-label">revoked {fmtTs(g.revokedAt)}</span>
                  : <button type="button" onClick={() => revoke.mutate(g.id)} disabled={revoke.isPending}>revoke</button>}
              </td>
            </tr>
          ))}
          {!grants.length && !grantsQ.isPending && (
            <tr><td colSpan={8}><em>no links minted yet</em></td></tr>
          )}
        </tbody>
      </table>
    </div>
  )
}

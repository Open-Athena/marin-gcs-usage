// Personal agent-token panel (specs/actions-ledger.md § API).
//
// Opens from the header chip. Shows whether the signed-in user has an active
// token, mints/rotates one (the raw value is shown exactly once — the server
// stores only its hash), and revokes. The copy-once secret plus the CLI recipe
// are the whole point: paste it into an agent's env and it can `gcs-usage mark`.
import { useCallback, useEffect, useState } from 'react'

interface Status {
  active: boolean
  created: number | null
}

type Phase =
  | { k: 'loading' }
  | { k: 'status'; s: Status }
  | { k: 'minted'; token: string; created: number }
  | { k: 'error'; msg: string }

const fmt = (unixS: number): string =>
  new Date(unixS * 1000).toISOString().replace('T', ' ').slice(0, 16) + ' UTC'

async function call(method: 'GET' | 'POST' | 'DELETE'): Promise<Response> {
  return fetch('/api/token', { method, credentials: 'include' })
}

export default function TokenModal({ onClose }: { onClose: () => void }) {
  const [phase, setPhase] = useState<Phase>({ k: 'loading' })
  const [busy, setBusy] = useState(false)
  const [copied, setCopied] = useState(false)

  const loadStatus = useCallback(async () => {
    try {
      const r = await call('GET')
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error ?? `HTTP ${r.status}`)
      setPhase({ k: 'status', s: (await r.json()) as Status })
    } catch (e) {
      setPhase({ k: 'error', msg: e instanceof Error ? e.message : String(e) })
    }
  }, [])

  useEffect(() => {
    void loadStatus()
  }, [loadStatus])

  // Escape closes; the backdrop click does too (handler below).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const mint = async () => {
    setBusy(true)
    try {
      const r = await call('POST')
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error ?? `HTTP ${r.status}`)
      const { token, created } = (await r.json()) as { token: string; created: number }
      setPhase({ k: 'minted', token, created })
      setCopied(false)
    } catch (e) {
      setPhase({ k: 'error', msg: e instanceof Error ? e.message : String(e) })
    } finally {
      setBusy(false)
    }
  }

  const revoke = async () => {
    setBusy(true)
    try {
      const r = await call('DELETE')
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error ?? `HTTP ${r.status}`)
      await loadStatus()
    } catch (e) {
      setPhase({ k: 'error', msg: e instanceof Error ? e.message : String(e) })
    } finally {
      setBusy(false)
    }
  }

  const copy = (token: string) => {
    void navigator.clipboard?.writeText(token).then(() => setCopied(true))
  }

  return (
    <div className="token-backdrop" onClick={onClose}>
      <div className="token-modal" onClick={e => e.stopPropagation()} role="dialog" aria-label="Agent token">
        <div className="token-head">
          <strong>Agent token</strong>
          <button className="token-x" type="button" onClick={onClose} aria-label="Close">×</button>
        </div>

        {phase.k === 'loading' && <p className="token-muted">Loading…</p>}

        {phase.k === 'error' && (
          <>
            <p className="token-err">{phase.msg}</p>
            <button type="button" onClick={() => void loadStatus()}>Retry</button>
          </>
        )}

        {phase.k === 'status' && (
          <>
            <p className="token-muted">
              A personal token lets your agents mark prefixes as you, from the CLI or any HTTP client.
              It carries only the <code>gcs</code> scope (mark &amp; view), and is shown once — we store
              only its hash.
            </p>
            {phase.s.active
              ? <p>Active token, created <strong>{fmt(phase.s.created!)}</strong>.</p>
              : <p className="token-muted">No token yet.</p>}
            <div className="token-actions">
              <button type="button" onClick={() => void mint()} disabled={busy}>
                {phase.s.active ? 'Rotate token' : 'Generate token'}
              </button>
              {phase.s.active && (
                <button type="button" className="token-danger" onClick={() => void revoke()} disabled={busy}>
                  Revoke
                </button>
              )}
            </div>
            {phase.s.active && (
              <p className="token-muted token-small">
                Rotating revokes the current token immediately — anything using it stops working until
                you paste the new one.
              </p>
            )}
          </>
        )}

        {phase.k === 'minted' && (
          <>
            <p className="token-warn">
              Copy this now — it won’t be shown again.
            </p>
            <div className="token-value">
              <code>{phase.token}</code>
              <button type="button" onClick={() => copy(phase.token)}>{copied ? 'Copied' : 'Copy'}</button>
            </div>
            <p className="token-muted token-small">Then, in your agent’s environment:</p>
            <pre className="token-recipe">{`export GCS_USAGE_TOKEN=${phase.token}
echo gs://marin-us-central1/checkpoints/my-run/ | gcs-usage mark`}</pre>
            <div className="token-actions">
              <button type="button" onClick={() => void loadStatus()}>Done</button>
            </div>
          </>
        )}
      </div>
    </div>
  )
}

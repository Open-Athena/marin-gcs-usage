import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { SiteNav } from './SiteNav'
import { UserChip } from './UserChip'

// /sweep — the sweep console (specs/sweep-executor.md § Phase 2): review the
// candidate sweep-only bands with their ownership evidence, sign bands off
// (rows in `sweep_approvals`; admin scope — everyone else sees read-only),
// and follow executor runs (`deletion_runs`, written by `gcs-usage sweep
// execute`) through to their object-level logs in /files.

interface Candidate {
  prefix: string
  net_bytes: number
  net_objects: number
  sweepers: string[]
  top_user: string | null
  share: number | null
  owner_match: boolean
}

interface DeletionRun {
  run_id: string
  plan: string
  scan: string
  mode: string
  actor: string
  started_ts: number
  finished_ts: number | null
  deleted_bytes: number
  deleted_objects: number
  skipped_gone: number
  skipped_overwritten: number
  drift_dirs: number
  ledger_drift_dirs: number
  undo_deadline: number | null
  undo_state: string
  log_dir: string
}

const tb = (b: number) => `${(b / 1e12).toFixed(2)} TB`
const when = (ts: number | null) => (ts ? new Date(ts * 1000).toISOString().slice(0, 16).replace('T', ' ') : '—')

const jfetch = async <T,>(url: string): Promise<T> => {
  const r = await fetch(url, { credentials: 'include' })
  if (!r.ok) throw new Error(`${url}: ${r.status}`)
  return r.json() as Promise<T>
}

export function SweepPage() {
  const qc = useQueryClient()
  const latestQ = useQuery({
    queryKey: ['sweep-latest'],
    queryFn: () => jfetch<{ plan: string }>('/v1/files/sweep/latest.json'),
    staleTime: 60_000,
  })
  const plan = latestQ.data?.plan
  const candsQ = useQuery({
    queryKey: ['sweep-candidates', plan],
    enabled: !!plan,
    queryFn: () => jfetch<{ plan: string; scan: string; head: number; bands: Candidate[] }>(`/v1/files/sweep/${plan}/candidates.json`),
    staleTime: 60_000,
  })
  const apprQ = useQuery({
    queryKey: ['sweep-approvals'],
    queryFn: () => jfetch<{ spec: { canWrite: boolean }; rows: { prefix: string; who: string; ts: number }[] }>('/api/db/sweep_approvals'),
  })
  const runsQ = useQuery({
    queryKey: ['deletion-runs'],
    queryFn: () => jfetch<{ rows: DeletionRun[] }>('/api/db/deletion_runs'),
    refetchInterval: 30_000,
  })
  const canWrite = apprQ.data?.spec.canWrite ?? false
  const approvals = new Map((apprQ.data?.rows ?? []).map(r => [r.prefix, r]))

  const approve = useMutation({
    mutationFn: async (c: Candidate) => {
      const r = await fetch('/api/db/sweep_approvals', {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ values: {
          prefix: c.prefix,
          scan: candsQ.data!.scan,
          head: String(candsQ.data!.head),
          note: `console: sweepers=${c.sweepers.join(',')} top=${c.top_user ?? '—'}${c.share != null ? ` ${(c.share * 100).toFixed(0)}%` : ''}`,
        } }),
      })
      if (!r.ok) throw new Error((await r.json() as { error?: string }).error ?? `${r.status}`)
    },
    onSettled: () => qc.invalidateQueries({ queryKey: ['sweep-approvals'] }),
  })
  const revoke = useMutation({
    mutationFn: async (prefix: string) => {
      const r = await fetch(`/api/db/sweep_approvals?pk=${encodeURIComponent(prefix)}`, { method: 'DELETE', credentials: 'include' })
      if (!r.ok) throw new Error(`${r.status}`)
    },
    onSettled: () => qc.invalidateQueries({ queryKey: ['sweep-approvals'] }),
  })

  const bands = candsQ.data?.bands ?? []
  const approvedBytes = bands.filter(b => approvals.has(b.prefix)).reduce((s, b) => s + b.net_bytes, 0)

  return (
    <main className="sweep-page">
      <SiteNav />
      <h1>Sweep console</h1>
      <p className="sub">
        Candidate bands are <b>sweep-only under the vote model</b> (no keep votes anywhere) — approval is the
        human owner check (sweeper vs the attribution top user). Approved bands feed{' '}
        <code>sweep manifest --approved-from-site</code>; runs land below with their logs.
        {plan && <> Plan <b>{plan}</b>{candsQ.data && <> · head {candsQ.data.head}</>} · approved <b>{tb(approvedBytes)}</b></>}
      </p>

      {latestQ.isError && <p className="err">No plan baked yet — run <code>gcs-usage sweep plan -C</code>.</p>}
      {candsQ.data && (
        <table className="sweep-table">
          <thead>
            <tr><th>band</th><th className="num">size</th><th className="num">objects</th><th>swept by</th><th>attributed top user</th><th>status</th></tr>
          </thead>
          <tbody>
            {bands.map(c => {
              const a = approvals.get(c.prefix)
              const drill = '/' + c.prefix.replace(/^gs:\/\//, '').replace(/\/$/, '')
              return (
                <tr key={c.prefix} className={a ? 'approved' : c.owner_match ? 'matched' : ''}>
                  <td><Link to={drill}><code>{c.prefix.replace('gs://', '')}</code></Link></td>
                  <td className="num">{tb(c.net_bytes)}</td>
                  <td className="num">{c.net_objects.toLocaleString()}</td>
                  <td>{c.sweepers.map(s => <UserChip key={s} who={s} size={16} />)}</td>
                  <td>
                    {c.top_user ? (
                      <>
                        <UserChip who={c.top_user} size={16} />
                        {c.share != null && <span className="pct"> {(c.share * 100).toFixed(0)}%</span>}
                        {c.owner_match && <span className="match-tag">= sweeper</span>}
                      </>
                    ) : <span className="dim">unattributed</span>}
                  </td>
                  <td>
                    {a ? (
                      <>
                        <span className="ok">approved</span> <span className="dim">by {a.who.split('@')[0]}</span>
                        {canWrite && <button className="mini" onClick={() => revoke.mutate(c.prefix)}>revoke</button>}
                      </>
                    ) : canWrite ? (
                      <button className="mini go" onClick={() => approve.mutate(c)}>approve</button>
                    ) : (
                      <span className="dim">—</span>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
      {(approve.error || revoke.error) && <p className="err">{String(approve.error ?? revoke.error)}</p>}

      <h2>Deletion runs</h2>
      {!runsQ.data?.rows.length && <p className="dim">None yet — the executor records every run (dry + real) here.</p>}
      {!!runsQ.data?.rows.length && (
        <table className="sweep-table">
          <thead>
            <tr><th>run</th><th>mode</th><th>started</th><th className="num">{'∑'} deleted</th><th className="num">gone</th><th className="num">overwritten</th><th className="num">drift</th><th>undo by</th><th>logs</th></tr>
          </thead>
          <tbody>
            {runsQ.data.rows.map(r => (
              <tr key={r.run_id}>
                <td><code>{r.run_id}</code> <span className="dim">by {r.actor}</span></td>
                <td>{r.mode === 'real' ? <b className="real">real</b> : 'dry'}</td>
                <td>{when(r.started_ts)}</td>
                <td className="num">{tb(r.deleted_bytes)} · {r.deleted_objects.toLocaleString()}</td>
                <td className="num">{r.skipped_gone.toLocaleString()}</td>
                <td className="num">{r.skipped_overwritten.toLocaleString()}</td>
                <td className="num">{r.drift_dirs + r.ledger_drift_dirs}</td>
                <td>{r.mode === 'real' ? when(r.undo_deadline) : '—'}</td>
                <td><Link to={`/files/${r.log_dir.replace('gs://oa-gcs-usage-dvx/', '')}${r.mode === 'real' ? 'deleted' : 'would-delete'}/`}>parquet →</Link></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </main>
  )
}

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { SiteNav } from './SiteNav'
import { Tooltip } from './Tooltip'
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
  /** Per-child sweeper-vs-attribution split (gross): the manifest's attr
   * gate only deletes the sweeper-attributed slice of an approved band. */
  attr_match_bytes?: number | null
  attr_other_bytes?: number | null
  attr_unattr_bytes?: number | null
}

/** What approving this band would let the executor delete (≈, gross-capped). */
const attrCap = (c: Candidate): number | null =>
  c.attr_match_bytes == null ? null : Math.min(c.attr_match_bytes, c.net_bytes)

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
    queryFn: () => jfetch<{ plan: string }>('/v1/files/get?path=sweep/latest.json'),
    staleTime: 60_000,
  })
  const plan = latestQ.data?.plan
  const candsQ = useQuery({
    queryKey: ['sweep-candidates', plan],
    enabled: !!plan,
    queryFn: () => jfetch<{ plan: string; scan: string; head: number; bands: Candidate[] }>(`/v1/files/get?path=${encodeURIComponent(`sweep/${plan}/candidates.json`)}`),
    staleTime: 60_000,
  })
  const apprQ = useQuery({
    queryKey: ['sweep-approvals'],
    queryFn: () => jfetch<{ spec: { canWrite: boolean }; rows: { prefix: string; who: string; ts: number; mode?: string }[] }>('/api/db/sweep_approvals'),
  })
  const runsQ = useQuery({
    queryKey: ['deletion-runs'],
    queryFn: () => jfetch<{ rows: DeletionRun[] }>('/api/db/deletion_runs'),
    refetchInterval: 30_000,
  })
  const canWrite = apprQ.data?.spec.canWrite ?? false
  const approvals = new Map((apprQ.data?.rows ?? []).map(r => [r.prefix, r]))

  const approve = useMutation({
    mutationFn: async ({ c, mode }: { c: Candidate; mode: 'slice' | 'full' }) => {
      const r = await fetch('/api/db/sweep_approvals', {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ values: {
          prefix: c.prefix,
          scan: candsQ.data!.scan,
          head: String(candsQ.data!.head),
          mode,
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
  // Default view: only bands where an approve would actually delete something
  // (a non-zero sweeper-attributed slice, or already approved). Overly-broad
  // sweeps of other users' data are noise for the reviewer — folded away.
  const [showAll, setShowAll] = useState(false)
  const actionable = (c: Candidate) => approvals.has(c.prefix) || (attrCap(c) ?? 0) > 0
  const hidden = bands.filter(c => !actionable(c))
  const hiddenBytes = hidden.reduce((s, b) => s + b.net_bytes, 0)
  const shown = showAll ? bands : [...bands.filter(actionable)].sort((x, y) => (attrCap(y) ?? 0) - (attrCap(x) ?? 0))
  const approvedRows = bands.filter(b => approvals.has(b.prefix))
  const approvedBytes = approvedRows.reduce((s, b) => s + b.net_bytes, 0)
  // 'full'-mode approvals bypass the attr gate → count the whole band
  const approvedAttrBytes = approvedRows.reduce(
    (s, b) => s + (approvals.get(b.prefix)?.mode === 'full' ? b.net_bytes : attrCap(b) ?? b.net_bytes), 0)

  const [armed, setArmed] = useState(false)
  const dispatch = useMutation({
    mutationFn: async (mode: 'dry' | 'real') => {
      const r = await fetch('/api/sweep/dispatch', {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode, date: candsQ.data!.scan }),
      })
      const j = await r.json() as { error?: string; job_id?: string; plan?: string; mode?: string }
      if (!r.ok) throw new Error(j.error ?? `${r.status}`)
      return j as { job_id: string; plan: string; mode: string }
    },
    onSuccess: () => setArmed(false),
  })

  return (
    <main className="sweep-page">
      <SiteNav />
      <h1>Sweep console</h1>
      <p className="sub">
        Candidate bands are <b>sweep-only under the vote model</b> (no keep votes anywhere). Two ways to sign one off:{' '}
        <b>approve</b> (slice) lets the executor delete only the <i>sweeper's own slice</i> — each directory must be
        majority-attributed to its sweeper, so other users' data inside a broad sweep is deferred to their own votes;{' '}
        <b>all</b> signs off the entire band regardless of attribution (for bands verified out-of-band). Approved bands
        feed <code>sweep manifest --approved-from-site</code>; runs land below with their logs.
        {plan && <> Plan <b>{plan}</b>{candsQ.data && <> · head {candsQ.data.head}</>} · approved <b>{tb(approvedBytes)}</b>
          {approvedAttrBytes !== approvedBytes && <> (≈<b>{tb(approvedAttrBytes)}</b> after the attr gate)</>}</>}
      </p>

      {latestQ.isError && <p className="err">No plan baked yet — run <code>gcs-usage sweep plan -C</code>.</p>}
      {candsQ.data && (
        <table className="sweep-table">
          <thead>
            <tr>
              <th>band</th>
              <th className="num">size</th>
              <th className="num">
                <Tooltip content={<>What an <b>approve slice</b> on this band would actually delete: the executor's attribution gate only deletes directories <b>majority-attributed to the band's sweeper</b> (per the scan's path index). Other users' and unattributed/mixed directories are deferred to their own votes. Estimate is gross (kept data inside still counts toward it), capped at the band's net size; the dry-run gives exact numbers.</>}>
                  <span className="hashelp">≈ deletable</span>
                </Tooltip>
              </th>
              <th className="num">objects</th><th>swept by</th><th>attributed top user</th><th>status</th>
            </tr>
          </thead>
          <tbody>
            {shown.map(c => {
              const a = approvals.get(c.prefix)
              const drill = '/' + c.prefix.replace(/^gs:\/\//, '').replace(/\/$/, '')
              return (
                <tr key={c.prefix} className={a ? 'approved' : c.owner_match ? 'matched' : ''}>
                  <td><Link to={drill}><code>{c.prefix.replace('gs://', '')}</code></Link></td>
                  <td className="num">{tb(c.net_bytes)}</td>
                  <td className="num" title={c.attr_other_bytes ? `${tb(c.attr_other_bytes)} attributed to other users + ${tb(c.attr_unattr_bytes ?? 0)} unattributed/mixed are deferred, not deleted` : undefined}>
                    {attrCap(c) == null ? <span className="dim">—</span> : (
                      <>
                        {tb(attrCap(c)!)}
                        {(c.attr_other_bytes ?? 0) + (c.attr_unattr_bytes ?? 0) > 0 && tb(attrCap(c)!) !== tb(c.net_bytes)
                          ? <span className="dim"> of {tb(c.net_bytes)}</span> : null}
                      </>
                    )}
                  </td>
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
                        {a.mode === 'full'
                          ? <Tooltip content={<>Approved in <b>full</b> mode: the ENTIRE band is deletable — including data attributed to other users or unattributed. The attribution gate is skipped for this band.</>}>
                              <span className="warn-tag">approved · FULL</span>
                            </Tooltip>
                          : <Tooltip content={<>Approved in <b>slice</b> mode: only directories majority-attributed to the sweeper ({c.sweepers.join(', ')}) are deletable{attrCap(c) != null && <> — ≈{tb(attrCap(c)!)} of {tb(c.net_bytes)}</>}. Everyone else's data in this band stays.</>}>
                              <span className="ok">approved</span>
                            </Tooltip>}
                        {' '}<span className="dim">by {a.who.split('@')[0]}</span>
                        {canWrite && <button className="mini" onClick={() => revoke.mutate(c.prefix)}>revoke</button>}
                      </>
                    ) : canWrite ? (
                      <>
                        <Tooltip content={<>Sign off the <b>sweeper's slice</b>: the executor deletes only directories majority-attributed to {c.sweepers.join(', ')}{attrCap(c) != null && <> — ≈<b>{tb(attrCap(c)!)}</b> of {tb(c.net_bytes)}</>}. Data attributed to other users, or unattributed, is deferred to their own votes — never deleted by this approval.</>}>
                          <button className="mini go" onClick={() => approve.mutate({ c, mode: 'slice' })}>
                            approve{attrCap(c) != null && <span className="btn-sub"> ≈{tb(attrCap(c)!)}</span>}
                          </button>
                        </Tooltip>
                        <Tooltip content={<>Sign off the <b>ENTIRE band</b> — all {tb(c.net_bytes)}, including data attributed to other users or unattributed. Skips the attribution gate. Use only after confirming out-of-band (e.g. with the affected users) that everything under this prefix can go.</>}>
                          <button className="mini warn" onClick={() => approve.mutate({ c, mode: 'full' })}>all</button>
                        </Tooltip>
                      </>
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
      {candsQ.data && hidden.length > 0 && (
        <p className="dim table-fold">
          {showAll
            ? <>showing all {bands.length} bands (incl. {hidden.length} with nothing slice-deletable) </>
            : <>{hidden.length} bands hidden — nothing slice-deletable (≈{tb(hiddenBytes)}, mostly other users' data under overly-broad sweeps; deferred to their own votes) </>}
          <button className="mini" onClick={() => setShowAll(v => !v)}>{showAll ? 'hide them' : 'show anyway'}</button>
        </p>
      )}
      {(approve.error || revoke.error) && <p className="err">{String(approve.error ?? revoke.error)}</p>}

      {canWrite && candsQ.data && (
        <div className="dispatch">
          {/* Dispatches a GCP Batch executor run: `sweep manifest -S` (reads
              the approvals above) → `sweep execute` — the run records itself
              into the table below. Dry-run is the default posture; "real"
              takes a second, armed click and shows what it will consume. */}
          <button className="mini go" disabled={dispatch.isPending} onClick={() => dispatch.mutate('dry')}>dispatch dry-run</button>
          {!armed ? (
            <button className="mini danger" disabled={approvedBytes === 0 || dispatch.isPending} onClick={() => setArmed(true)}
                    title={approvedBytes === 0 ? 'approve at least one band first' : undefined}>
              real delete…
            </button>
          ) : (
            <>
              <button className="mini danger armed" disabled={dispatch.isPending} onClick={() => dispatch.mutate('real')}>
                confirm REAL delete — ≈{tb(approvedAttrBytes)} (attr-gated, of {tb(approvedBytes)} approved)
              </button>
              <button className="mini" onClick={() => setArmed(false)}>cancel</button>
            </>
          )}
          {dispatch.isPending && <span className="dim">submitting…</span>}
          {dispatch.data && (
            <span className="ok">
              submitted <code>{dispatch.data.job_id}</code> — appears below once the executor records it
              (streaming the listing takes a while; this page refetches runs every 30s)
            </span>
          )}
          {dispatch.error != null && <span className="err">{String(dispatch.error)}</span>}
        </div>
      )}

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

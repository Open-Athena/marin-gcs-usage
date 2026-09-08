import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { useActions } from 'use-kbd'
import { SiteNav } from './SiteNav'
import { Tooltip } from './Tooltip'
import { Avatar } from './Avatar'
import { MultiSelect } from './MultiSelect'
import { ghHandle, shortName, UserChip } from './UserChip'
import { useUnits } from './units'

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

const userMatch = (u: string | null | undefined, t: string): boolean =>
  !!u && (u.toLowerCase().includes(t) || shortName(u).toLowerCase().includes(t))

/** Filter-box query: whitespace-separated terms, all must match. A bare term
 * matches the path or any user on the row; `owner:`/`o:` and `sweeper:`/`s:`
 * (or `by:`) pin a user to one role; `is:approved` / `is:todo` / `is:unowned`
 * filter on state. */
const matchRow = (c: Candidate, approved: boolean, q: string): boolean =>
  q.toLowerCase().split(/\s+/).filter(Boolean).every(t => {
    const i = t.indexOf(':')
    const [k, v] = i > 0 ? [t.slice(0, i), t.slice(i + 1)] : ['', t]
    switch (k) {
      case 'o': case 'owner': return userMatch(c.top_user, v)
      case 's': case 'sweeper': case 'by': return c.sweepers.some(s => userMatch(s, v))
      case 'is': return v === 'approved' ? approved : v === 'todo' || v === 'unapproved' ? !approved : v === 'unowned' ? !c.top_user : true
      default: return c.prefix.toLowerCase().includes(t) || userMatch(c.top_user, t) || c.sweepers.some(s => userMatch(s, t))
    }
  })

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

/** A `gcs-sweep-*` Batch job as `/api/sweep/jobs` reports it (live state). */
interface SweepJob {
  job_id: string
  mode: 'dry' | 'real'
  state: string
  created: string
  updated: string | null
  run_secs: number | null
  by: string | null
  date: string | null
  plan: string
  last_event: string | null
  logs: string
}

const when = (ts: number | null) => (ts ? new Date(ts * 1000).toISOString().slice(0, 16).replace('T', ' ') : '—')
const fmtDur = (s: number): string => {
  const m = Math.floor(s / 60)
  return m < 60 ? `${m}m` : `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m`
}

/** Run `fn` over `xs` with at most `n` in flight; rejects on the first failure. */
const mapLimit = async <T,>(xs: T[], n: number, fn: (x: T) => Promise<void>): Promise<void> => {
  let i = 0
  const worker = async () => { while (i < xs.length) await fn(xs[i++]) }
  await Promise.all(Array.from({ length: Math.min(n, xs.length) }, worker))
}

type Status = 'approved' | 'todo'
const STATUSES: Status[] = ['approved', 'todo']

const PAGE_SIZES = [25, 50, 100, Infinity]
const fmtPageSize = (n: number) => (n === Infinity ? 'all' : String(n))
const clamp = (n: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, n))

const jfetch = async <T,>(url: string): Promise<T> => {
  const r = await fetch(url, { credentials: 'include' })
  if (!r.ok) throw new Error(`${url}: ${r.status}`)
  return r.json() as Promise<T>
}

export function SweepPage() {
  const qc = useQueryClient()
  // Site-wide byte-unit preference (IEC TiB default; header toggle / `?si`) —
  // the treemap pages use the same formatter, so sizes agree across pages.
  const { fmtBytes: tb } = useUnits()
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
  // Live Batch state for every sweep job — visible from the moment it is
  // queued, hours before the executor writes its `deletion_runs` row.
  const jobsQ = useQuery({
    queryKey: ['sweep-jobs'],
    queryFn: () => jfetch<{ jobs: SweepJob[]; configured: boolean }>('/api/sweep/jobs'),
    refetchInterval: 30_000,
  })
  const canWrite = apprQ.data?.spec.canWrite ?? false
  const approvals = new Map((apprQ.data?.rows ?? []).map(r => [r.prefix, r]))

  // One row per approval on the wire (the /api/db route inserts one row per
  // POST); a bulk approve/revoke fans those out a few at a time and refetches
  // once. The first failure aborts the rest and surfaces as the error.
  const approve = useMutation({
    mutationFn: async ({ cs, mode }: { cs: Candidate[]; mode: 'slice' | 'full' }) => {
      await mapLimit(cs, 4, async c => {
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
        if (!r.ok) throw new Error(`${c.prefix}: ${(await r.json() as { error?: string }).error ?? r.status}`)
      })
    },
    onSettled: () => qc.invalidateQueries({ queryKey: ['sweep-approvals'] }),
  })
  const revoke = useMutation({
    mutationFn: async (prefixes: string[]) => {
      await mapLimit(prefixes, 4, async prefix => {
        const r = await fetch(`/api/db/sweep_approvals?pk=${encodeURIComponent(prefix)}`, { method: 'DELETE', credentials: 'include' })
        if (!r.ok) throw new Error(`${prefix}: ${r.status}`)
      })
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
  // ---- paging + selection ----
  // Selection is keyed by prefix (so it survives paging and the show-all
  // toggle); the cursor is a row index within the current page, as in the
  // use-kbd table demo. Click / `x` toggle a row; shift+click and shift+j/k
  // select a range from the cursor; j/k move the cursor without touching the
  // selection; the checkbox column is the touch/mouse equivalent.
  const [q, setQ] = useState('')
  // Status axis: a multi-select over approved / todo; both (or neither) = all.
  const [stSel, setStSel] = useState<Status[]>(STATUSES)
  const st: 'all' | Status = stSel.length === 1 ? stSel[0] : 'all'
  const nApproved = shown.filter(c => approvals.has(c.prefix)).length
  const rows = shown.filter(c => {
    const a = approvals.has(c.prefix)
    return (st === 'all' || (st === 'approved') === a) && (!q.trim() || matchRow(c, a, q))
  })
  const [pageSize, setPageSize] = useState(PAGE_SIZES[0])
  const [pageRaw, setPage] = useState(0)
  const pages = Math.max(1, Math.ceil(rows.length / pageSize))
  const page = clamp(pageRaw, 0, pages - 1)
  const pageRows = rows.slice(page * pageSize, (page + 1) * pageSize)
  const [cursor, setCursor] = useState(-1)
  const [selected, setSelected] = useState<Set<string>>(() => new Set())
  const cursorRow = pageRows[cursor] as Candidate | undefined
  const selRows = bands.filter(b => selected.has(b.prefix))
  const rowRefs = useRef<(HTMLTableRowElement | null)[]>([])
  useEffect(() => { rowRefs.current[cursor]?.scrollIntoView({ block: 'nearest' }) }, [cursor, page])
  useEffect(() => { setCursor(-1); setPage(0) }, [pageSize, showAll, q, st])

  const toggle = useCallback((prefixes: string[], on?: boolean) => setSelected(prev => {
    const next = new Set(prev)
    const add = on ?? !prefixes.every(p => prev.has(p))
    for (const p of prefixes) add ? next.add(p) : next.delete(p)
    return next
  }), [])
  const clearSel = () => { setSelected(new Set()); setCursor(-1) }
  const moveCursor = (d: number, extend: boolean) => {
    if (!pageRows.length) return
    const from = cursor < 0 ? (d > 0 ? -1 : pageRows.length) : cursor
    const to = clamp(from + d, 0, pageRows.length - 1)
    if (extend) {
      const a = cursor < 0 ? to : cursor
      toggle(pageRows.slice(Math.min(a, to), Math.max(a, to) + 1).map(r => r.prefix), true)
    }
    setCursor(to)
  }
  const rowClick = (i: number, e: React.MouseEvent) => {
    if ((e.target as HTMLElement).closest('a, button, input')) return
    if (e.shiftKey && cursor >= 0) toggle(pageRows.slice(Math.min(cursor, i), Math.max(cursor, i) + 1).map(r => r.prefix), true)
    else toggle([pageRows[i].prefix])
    setCursor(i)
  }
  const gotoPage = (p: number) => { setPage(clamp(p, 0, pages - 1)); setCursor(-1) }

  // Bulk targets: the selection, else the cursor row. Approve skips bands
  // already approved; revoke skips the rest.
  const targets = selRows.length ? selRows : cursorRow ? [cursorRow] : []
  const toApprove = targets.filter(c => !approvals.has(c.prefix))
  const toRevoke = targets.filter(c => approvals.has(c.prefix))
  const busy = approve.isPending || revoke.isPending
  const selBytes = selRows.reduce((s, b) => s + b.net_bytes, 0)
  const selCap = selRows.reduce((s, b) => s + (attrCap(b) ?? 0), 0)

  const G = 'Sweep console'
  useActions({
    'sweep:down': { label: 'Cursor down', group: G, defaultBindings: ['j', 'arrowdown'], handler: () => moveCursor(1, false) },
    'sweep:up': { label: 'Cursor up', group: G, defaultBindings: ['k', 'arrowup'], handler: () => moveCursor(-1, false) },
    'sweep:extend-down': { label: 'Select down (extend from the cursor)', group: G, defaultBindings: ['shift+j', 'shift+arrowdown'], handler: () => moveCursor(1, true) },
    'sweep:extend-up': { label: 'Select up (extend from the cursor)', group: G, defaultBindings: ['shift+k', 'shift+arrowup'], handler: () => moveCursor(-1, true) },
    'sweep:toggle': { label: 'Select / deselect the cursor row', group: G, defaultBindings: ['x', 'space'], handler: e => { e?.preventDefault(); if (cursorRow) toggle([cursorRow.prefix]) } },
    'sweep:toggle-page': { label: 'Select / deselect every row on this page', group: G, defaultBindings: ['shift+x'], handler: () => toggle(pageRows.map(r => r.prefix)) },
    'sweep:clear': { label: 'Clear the selection', group: G, defaultBindings: ['escape'], handler: clearSel },
    'sweep:next-page': { label: 'Next page', group: G, defaultBindings: [']'], handler: () => gotoPage(page + 1) },
    'sweep:prev-page': { label: 'Previous page', group: G, defaultBindings: ['['], handler: () => gotoPage(page - 1) },
    'sweep:approve': { label: 'Approve (slice) the selected bands', group: G, defaultBindings: ['a'], enabled: canWrite, handler: () => { if (toApprove.length && !busy) approve.mutate({ cs: toApprove, mode: 'slice' }) } },
    'sweep:revoke': { label: 'Revoke the selected approvals', group: G, defaultBindings: ['r'], enabled: canWrite, handler: () => { if (toRevoke.length && !busy) revoke.mutate(toRevoke.map(c => c.prefix)) } },
  })

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
        majority-owned by its sweeper, so other users' data inside a broad sweep is deferred to their own votes;{' '}
        <b>all</b> signs off the entire band regardless of ownership (for bands verified out-of-band). Approved bands
        feed <code>sweep manifest --approved-from-site</code>; runs land below with their logs.
        {plan && <> Plan <b>{plan}</b>{candsQ.data && <> · head {candsQ.data.head}</>} · approved <b>{tb(approvedBytes)}</b>
          {approvedAttrBytes !== approvedBytes && <> (≈<b>{tb(approvedAttrBytes)}</b> after the ownership gate)</>}</>}
      </p>

      {latestQ.isError && <p className="err">No plan baked yet — run <code>gcs-usage sweep plan -C</code>.</p>}
      {candsQ.data && (<>
        <div className="sweep-tools">
          <span className="tb-axis nb">
            <span className="lbl">status</span>
            <MultiSelect<Status>
              label="approval status"
              options={[
                { key: 'approved', label: `approved ${nApproved}`, glyph: '✓', color: '#3fb950', tip: 'Bands somebody has signed off (slice or full) — what a dispatch would plan.' },
                { key: 'todo', label: `todo ${shown.length - nApproved}`, glyph: '○', color: 'var(--ink-2)', tip: 'Bands still waiting for a decision.' },
              ]}
              selected={stSel}
              onChange={keys => setStSel(keys.length === 0 ? STATUSES : keys)}
            />
          </span>
          <input className="filter" type="search" value={q} onChange={e => setQ(e.target.value)}
                 placeholder="filter: path · user · owner:x · sweeper:x · is:unowned" />
          {(q.trim() || st !== 'all') && <span className="dim nb">{rows.length} of {shown.length} match</span>}
          {selected.size > 0 && (
            <span className="sel-bar">
              <b>{selRows.length}</b> selected · ≈<b>{tb(selCap)}</b> deletable{selCap !== selBytes && <span className="dim"> of {tb(selBytes)}</span>}
              {canWrite && (
                <>
                  <Tooltip content={<>Approve the <b>sweeper's slice</b> of every selected band not yet approved ({toApprove.length}). Other users' and unowned data in them is deferred, never deleted by this.</>}>
                    <button className="mini go" disabled={!toApprove.length || busy} onClick={() => approve.mutate({ cs: toApprove, mode: 'slice' })}>approve ×{toApprove.length}</button>
                  </Tooltip>
                  <Tooltip content={<>Approve the <b>ENTIRE band</b> — including other users' and unowned data — for every selected band not yet approved ({toApprove.length}). Skips the ownership gate.</>}>
                    <button className="mini warn" disabled={!toApprove.length || busy} onClick={() => approve.mutate({ cs: toApprove, mode: 'full' })}>all ×{toApprove.length}</button>
                  </Tooltip>
                  <button className="mini" disabled={!toRevoke.length || busy} onClick={() => revoke.mutate(toRevoke.map(c => c.prefix))}>revoke ×{toRevoke.length}</button>
                </>
              )}
              <button className="mini" onClick={clearSel}>clear</button>
            </span>
          )}
        </div>
        <div className="table-scroll"><table className="sweep-table">
          <thead>
            <tr>
              <th className="col-sel"><input type="checkbox" title="select / deselect this page (⇧x)" checked={pageRows.length > 0 && pageRows.every(r => selected.has(r.prefix))} onChange={() => toggle(pageRows.map(r => r.prefix))} /></th>
              <th>band</th>
              <th className="num">
                <Tooltip content={<>The band's net size. The <span className="warn-ink">yellow ✓</span> approves the <b>whole band</b> for deletion — other users' and unowned data included (skips the ownership gate).</>}>
                  <span className="hashelp">size</span>
                </Tooltip>
              </th>
              <th className="num">
                <Tooltip content={<>What an <b>approve slice</b> on this band would actually delete: the executor's ownership gate only deletes directories <b>majority-owned by the band's sweeper</b> (per the scan's path index). Other users' and unowned/mixed directories are deferred to their own votes. Estimate is gross (kept data inside still counts toward it), capped at the band's net size; the dry-run gives exact numbers. The <span className="go-ink">green ✓</span> approves this slice.</>}>
                  <span className="hashelp">≈ deletable</span>
                </Tooltip>
              </th>
              <th className="num col-objects">objects</th><th>swept by</th><th>top owner</th><th>status</th>
            </tr>
          </thead>
          <tbody>
            {pageRows.map((c, i) => {
              const a = approvals.get(c.prefix)
              const cap = attrCap(c)
              // Land on the band scoped to its sweeper's attributed slice —
              // the data an approval would actually delete — colored by read
              // recency (staleness is the case for deletion).
              const drill = '/' + c.prefix.replace(/^gs:\/\//, '').replace(/\/$/, '')
                + '?c=read' + (c.sweepers.length === 1 ? `&o=${encodeURIComponent(c.sweepers[0])}` : '')
              const cls = [a ? 'approved' : c.owner_match ? 'matched' : '', selected.has(c.prefix) ? 'sel' : '', i === cursor ? 'cur' : ''].filter(Boolean).join(' ')
              return (
                <tr key={c.prefix} ref={el => { rowRefs.current[i] = el }} className={cls} onClick={e => rowClick(i, e)}
                    onMouseDown={e => { if (e.shiftKey) e.preventDefault() }}>
                  <td className="col-sel"><input type="checkbox" checked={selected.has(c.prefix)} onChange={() => toggle([c.prefix])} /></td>
                  <td><Link to={drill}><code>{c.prefix.replace('gs://', '')}</code></Link></td>
                  <td className="num">
                    <span className="nb">
                      {tb(c.net_bytes)}
                      {canWrite && !a && (
                        <Tooltip content={<>Approve the <b>ENTIRE band</b> — all {tb(c.net_bytes)}, including data owned by other users or unowned. Skips the ownership gate. Use only after confirming out-of-band (e.g. with the affected users) that everything under this prefix can go.</>}>
                          <button className="mini ico warn" disabled={busy} onClick={() => approve.mutate({ cs: [c], mode: 'full' })}>✓</button>
                        </Tooltip>
                      )}
                    </span>
                  </td>
                  <td className="num" title={c.attr_other_bytes ? `${tb(c.attr_other_bytes)} owned by other users + ${tb(c.attr_unattr_bytes ?? 0)} unowned/mixed are deferred, not deleted` : undefined}>
                    <span className="nb">
                      {cap == null ? <span className="dim">—</span> : tb(cap)}
                      {canWrite && !a && (
                        <Tooltip content={<>Approve the <b>sweeper's slice</b>: the executor deletes only directories majority-owned by {c.sweepers.join(', ')}{cap != null && <> — ≈<b>{tb(cap)}</b> of {tb(c.net_bytes)}</>}. Data owned by other users, or unowned, is deferred to their own votes — never deleted by this approval.</>}>
                          <button className="mini ico go" disabled={busy} onClick={() => approve.mutate({ cs: [c], mode: 'slice' })}>✓</button>
                        </Tooltip>
                      )}
                    </span>
                  </td>
                  <td className="num col-objects"><span className="nb">{c.net_objects.toLocaleString()}</span></td>
                  <td>{c.sweepers.map(s => <UserChip key={s} who={s} size={16} />)}</td>
                  <td>
                    {c.owner_match ? (
                      // The sweeper owns the band: one chip (in "swept by") is enough.
                      <span className="match-tag nb">= sweeper{c.share != null && c.share < 0.995 && <> · {(c.share * 100).toFixed(0)}%</>}</span>
                    ) : c.top_user ? (
                      <>
                        <UserChip who={c.top_user} size={16} />
                        {c.share != null && <span className="pct nb"> {(c.share * 100).toFixed(0)}%</span>}
                      </>
                    ) : <span className="dim">unowned</span>}
                  </td>
                  <td>
                    {a ? (
                      <>
                        {a.mode === 'full'
                          ? <Tooltip content={<>Approved in <b>full</b> mode: the ENTIRE band is deletable — including data owned by other users or unowned. The ownership gate is skipped for this band.</>}>
                              <span className="warn-tag">approved · FULL</span>
                            </Tooltip>
                          : <Tooltip content={<>Approved in <b>slice</b> mode: only directories majority-owned by the sweeper ({c.sweepers.join(', ')}) are deletable{cap != null && <> — ≈{tb(cap)} of {tb(c.net_bytes)}</>}. Everyone else's data in this band stays.</>}>
                              <span className="ok">approved</span>
                            </Tooltip>}
                        {' '}<Tooltip content={<>approved by <b>{shortName(a.who)}</b> · {when(a.ts)} UTC · {a.mode === 'full' ? 'full band' : 'slice'}</>}>
                          <span className="approver"><Avatar github={ghHandle(a.who)} name={shortName(a.who)} size={16} /></span>
                        </Tooltip>
                        {canWrite && <button className="mini" disabled={busy} onClick={() => revoke.mutate([c.prefix])}>revoke</button>}
                      </>
                    ) : (
                      <span className="dim">—</span>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table></div>
        <div className="pager">
          <span className="dim nb">{rows.length ? `${page * pageSize + 1}–${Math.min(rows.length, (page + 1) * pageSize)} of ${rows.length}` : 'no bands match'}</span>
          {pages > 1 && (
            <span className="nb">
              <button className="mini" disabled={page === 0} onClick={() => gotoPage(page - 1)}>‹</button>
              {Array.from({ length: pages }, (_, p) => <button key={p} className={`mini${p === page ? ' on' : ''}`} onClick={() => gotoPage(p)}>{p + 1}</button>)}
              <button className="mini" disabled={page === pages - 1} onClick={() => gotoPage(page + 1)}>›</button>
            </span>
          )}
          <span className="nb"><span className="dim">per page</span>{PAGE_SIZES.map(n => <button key={n} className={`mini${n === pageSize ? ' on' : ''}`} onClick={() => setPageSize(n)}>{fmtPageSize(n)}</button>)}</span>
          <span className="dim kbd-hint"><kbd>j</kbd>/<kbd>k</kbd> move · <kbd>x</kbd> select · <kbd>⇧j</kbd>/<kbd>⇧k</kbd> range · <kbd>⇧x</kbd> page{canWrite && <> · <kbd>a</kbd> approve · <kbd>r</kbd> revoke</>} · <kbd>[</kbd>/<kbd>]</kbd> prev/next</span>
        </div>
      </>)}
      {candsQ.data && hidden.length > 0 && (
        <p className="dim table-fold">
          {showAll
            ? <>showing all {bands.length} bands (incl. {hidden.length} with nothing slice-deletable) </>
            : <>{hidden.length} bands hidden — nothing slice-deletable (≈{tb(hiddenBytes)}, mostly other users' data under overly-broad sweeps; deferred to their own votes) </>}
          <button className="mini" onClick={() => setShowAll(v => !v)}>{showAll ? 'hide them' : 'show anyway'}</button>
        </p>
      )}
      {(approve.error || revoke.error) && <p className="err">{String(approve.error ?? revoke.error)}</p>}

      {canWrite && candsQ.data && (() => {
        // What a dispatch operates on: every approved band, whoever approved
        // it — spelled out here so "dispatch" is never a leap of faith.
        const ap = bands.filter(c => approvals.has(c.prefix))
        const objs = ap.reduce((n, c) => n + c.net_objects, 0)
        const bySweeper = new Map<string, number>()
        for (const c of ap) for (const sw of c.sweepers) bySweeper.set(sw, (bySweeper.get(sw) ?? 0) + 1)
        const full = ap.filter(c => approvals.get(c.prefix)!.mode === 'full').length
        return (
          <div className="dispatch">
            <p className="dispatch-sum">
              {ap.length === 0 ? <>Nothing approved yet — a dispatch would plan nothing.</> : (
                <>
                  <b>{ap.length}</b> approved band{ap.length === 1 ? '' : 's'} · <b>≈{tb(approvedAttrBytes)}</b> deletable
                  {approvedAttrBytes !== approvedBytes && <span className="dim"> (of {tb(approvedBytes)} in the bands; the rest is other users' or unowned data, deferred)</span>}
                  {' '}· {objs.toLocaleString()} objects in the bands
                  {full > 0 && <> · <span className="warn-tag">{full} in FULL mode</span></>}
                  {' '}· swept by {[...bySweeper].sort((x, y) => y[1] - x[1]).map(([sw, n], i) => <span key={sw}>{i > 0 && ', '}<UserChip who={sw} size={14} /> ×{n}</span>)}
                </>
              )}
            </p>
            <p className="dim dispatch-note">
              A dry-run re-lists each approved band, plans the deletions under the ownership gate, and records the run below — it deletes nothing. The real run takes the same plan and deletes.
            </p>
            <div className="dispatch-btns">
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
          </div>
        )
      })()}

      <h2>Dispatches</h2>
      {jobsQ.data?.configured === false && <p className="dim">Dispatch isn't configured on this deployment (no <code>GCP_SA_KEY</code>), so there is nothing to list.</p>}
      {jobsQ.isError && <p className="err">{String(jobsQ.error)}</p>}
      {jobsQ.data?.configured && !jobsQ.data.jobs.length && <p className="dim">None yet — every job the console (or the CLI) submits to Batch shows here from the moment it is queued.</p>}
      {!!jobsQ.data?.jobs.length && (
        <div className="table-scroll"><table className="sweep-table jobs">
          <thead>
            <tr><th>job</th><th>mode</th><th>state</th><th>by</th><th>started</th><th className="num">elapsed</th><th>plan</th><th>logs</th></tr>
          </thead>
          <tbody>
            {jobsQ.data.jobs.map(j => {
              const live = j.state === 'RUNNING' || j.state === 'QUEUED' || j.state === 'SCHEDULED'
              const secs = j.run_secs ?? (live ? (Date.now() - Date.parse(j.created)) / 1000 : null)
              const recorded = runsQ.data?.rows.some(r => r.log_dir.includes(j.job_id))
              return (
                <tr key={j.job_id} className={j.state === 'FAILED' ? 'failed' : live ? 'live' : ''}>
                  <td><code>{j.job_id}</code></td>
                  <td>{j.mode === 'real' ? <span className="warn-tag">REAL</span> : 'dry'}</td>
                  <td>
                    <span className={j.state === 'SUCCEEDED' ? 'ok' : j.state === 'FAILED' ? 'err' : live ? 'live-tag' : 'dim'}>{j.state.toLowerCase()}</span>
                    {recorded && <span className="dim nb"> · run recorded ↓</span>}
                    {j.state === 'FAILED' && j.last_event && <div className="dim small">{j.last_event}</div>}
                  </td>
                  <td>{j.by ? shortName(j.by) : '—'}</td>
                  <td><span className="nb">{when(Date.parse(j.created) / 1000)}</span></td>
                  <td className="num"><span className="nb">{secs == null ? '—' : fmtDur(secs)}</span></td>
                  <td><Link to={`/files/${j.plan.replace('gs://oa-gcs-usage-dvx/', '')}/`}>{j.date ?? 'plan'} →</Link></td>
                  <td><a href={j.logs} target="_blank" rel="noreferrer">Cloud Logging ↗</a></td>
                </tr>
              )
            })}
          </tbody>
        </table></div>
      )}

      <h2>Deletion runs</h2>
      {!runsQ.data?.rows.length && <p className="dim">None yet — the executor records every run (dry + real) here, once its manifest step is done.</p>}
      {!!runsQ.data?.rows.length && (
        <div className="table-scroll"><table className="sweep-table">
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
        </table></div>
      )}
    </main>
  )
}

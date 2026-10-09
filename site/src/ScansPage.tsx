// /scans and /scans/<run id> (specs/scan-runs-ui.md): what each scan job did —
// when it ran, its phases, what it wrote and how that compares to the
// previous scan — from the D1 rows `dt-cloud scan-run record | backfill`
// writes. DIY SVG: a duration strip over all runs, a phase bar per row, and a
// phase timeline per run.
import { useEffect, useMemo, useState, type PointerEvent as ReactPointerEvent, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { SiteNav } from './SiteNav'
import { SiteKbd } from './SiteKbd'
import { Tooltip } from './Tooltip'
import { fmtDur } from './runs'
import { axisTicks } from './runChart'
import { fmtBytesPrecise, fmtN } from './types'
import { useUnits } from './units'
import { DEFAULT_STORE } from './stores'
import {
  batchHref, logsHref, mapHref, metaHref, type OutputDelta, phaseOrder, type PhaseSpan, type RunSummary, type ScanRun,
  type ScanRunPhase,
} from './scanRunsModel'
import './scans.scss'

interface Links { project: string | null; meta: string | null }
type ListResponse = Links & { configured: boolean; runs: RunSummary[] }
type DetailResponse = Links & {
  run: ScanRun; secs: number | null; failed_at: string | null; phases: ScanRunPhase[]; spans: PhaseSpan[]
  outputs: OutputDelta[]; downstream: ScanRun[]; siblings: ScanRun[]; prev_scan: string | null; next_scan: string | null
}

async function getJson<T>(url: string): Promise<T> {
  const r = await fetch(url, { credentials: 'include' })
  const text = await r.text()
  let data: unknown = null
  try { data = JSON.parse(text) } catch { data = null }
  if (!r.ok) throw new Error((data as { error?: string } | null)?.error ?? `${r.status} ${r.statusText}`)
  return data as T
}

const useScanRuns = () => useQuery<ListResponse, Error>({ queryKey: ['scan-runs'], queryFn: () => getJson('/api/scan-runs'), refetchInterval: 60_000, staleTime: 15_000 })
const useScanRun = (id: string) => useQuery<DetailResponse, Error>({
  queryKey: ['scan-run', id], queryFn: () => getJson(`/api/scan-runs/${encodeURIComponent(id)}`),
  refetchInterval: q => (q.state.data?.run.status === 'running' ? 30_000 : false), staleTime: 15_000,
})

const utc = (ts: number | null): string => (ts == null ? '—' : new Date(ts * 1000).toISOString().slice(0, 16).replace('T', ' '))
const scanHref = (id: string) => `/scans/${encodeURIComponent(id)}`
const PALETTE = ['--s1', '--s2', '--s4', '--s7', '--s5', '--s6', '--s3', '--s8']
/** A stable color per phase name: its position in the usual phase order. */
const colorer = (order: string[]) => (phase: string) => `var(${PALETTE[Math.max(0, order.indexOf(phase)) % PALETTE.length]})`

function useBytes() {
  const { units, suffixB } = useUnits()
  return {
    bytes: (b: number | null | undefined) => (b == null ? '—' : fmtBytesPrecise(b, units, suffixB)),
    delta: (b: number | null | undefined) => (b == null ? '—' : b === 0 ? '±0' : `${b > 0 ? '+' : '−'}${fmtBytesPrecise(Math.abs(b), units, suffixB)}`),
  }
}

/** A tip for SVG marks (the HTML `Tooltip` wraps its reference in a span,
 * which SVG can't hold): marks set it on pointer move, the wrapper renders it. */
function useSvgTip() {
  const [tip, setTip] = useState<{ x: number; y: number; content: ReactNode } | null>(null)
  const on = (content: ReactNode) => ({
    onPointerMove: (e: ReactPointerEvent<SVGElement>) => {
      const box = (e.currentTarget.ownerSVGElement ?? e.currentTarget).parentElement!.getBoundingClientRect()
      setTip({ x: e.clientX - box.left, y: e.clientY - box.top, content })
    },
    onPointerLeave: () => setTip(null),
  })
  const el = tip && <div className="sr-svgtip tooltip-content" style={{ left: tip.x, top: tip.y }}>{tip.content}</div>
  return { on, el }
}

const usd = (x: number | null | undefined) => (x == null ? '—' : `$${x < 10 ? x.toFixed(2) : Math.round(x).toLocaleString('en-US')}`)

const deltaClass = (d: number | null | undefined) => (d == null || d === 0 ? 'dim' : d > 0 ? 'grew' : 'shrank')

function Status({ run, failedAt }: { run: ScanRun; failedAt?: string | null }) {
  const tag = <span className={`sr-status ${run.status}`}>{run.status === 'nop' ? 'no-op' : run.status}</span>
  if (run.status !== 'failed') return tag
  return <Tooltip content={<>{failedAt && <div>Failed {failedAt.startsWith('after') ? failedAt : `in ${failedAt}`}</div>}{run.error && <div className="sr-err">{run.error}</div>}</>}>{tag}</Tooltip>
}

/** A run's phases as one bar (the main lane; overlapped phases are in its tip). */
function PhaseBar({ spans, secs, color, width = 160 }: { spans: PhaseSpan[]; secs: number | null; color: (p: string) => string; width?: number }) {
  if (!secs) return <span className="dim">—</span>
  const x = (t: number) => (t / secs) * width
  const main = spans.filter(s => s.lane === 0)
  return (
    <Tooltip content={spans.length ? <table className="sr-tip"><tbody>{spans.map(s => <tr key={s.phase}><td><i className="sw" style={{ background: color(s.phase) }} />{s.phase}{s.lane ? ' (beside)' : ''}</td><td className="num">{fmtDur(s.end - s.start)}</td></tr>)}</tbody></table> : 'No phase markers recorded for this run'}>
      <svg className="sr-bar" width={width} height={10} viewBox={`0 0 ${width} 10`} role="img" aria-label={`${fmtDur(secs)} in ${main.length} phases`}>
        <rect className="sr-bar-bg" x={0} y={0} width={width} height={10} rx={2} />
        {main.map(s => <rect key={s.phase} x={x(s.start)} y={0} width={Math.max(1, x(s.end) - x(s.start))} height={10} style={{ fill: color(s.phase) }} />)}
      </svg>
    </Tooltip>
  )
}

/** Duration per run over time, stacked by phase; a failed run in the danger ink. */
function DurationStrip({ runs, color, onPick }: { runs: RunSummary[]; color: (p: string) => string; onPick: (id: string) => void }) {
  const tip = useSvgTip()
  const pts = runs.filter(r => r.secs != null && r.run.started_ts != null)
  if (pts.length < 2) return null
  const W = 1000, H = 180, L = 44, R = 8, T = 8, B = 22
  const t0 = Math.min(...pts.map(p => p.run.started_ts!)), t1 = Math.max(...pts.map(p => p.run.started_ts!))
  const span = Math.max(3600, t1 - t0)
  const hours = Math.max(...pts.map(p => p.secs!)) / 3600
  const ticks = axisTicks(hours)
  const top = ticks[ticks.length - 1] || 1
  const x = (t: number) => L + ((t - t0) / span) * (W - L - R)
  const y = (h: number) => H - B - (h / top) * (H - T - B)
  const bw = Math.max(2, Math.min(14, ((W - L - R) / pts.length) * 0.7))
  const days = [...new Set(pts.map(p => new Date(p.run.started_ts! * 1000).toISOString().slice(0, 10)))]
  const labelEvery = Math.max(1, Math.ceil(days.length / 8))
  return (
    <div className="sr-svgwrap">
      <svg className="sr-strip" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Run duration over time, by phase">
        {ticks.map(h => <g key={h}><line className="grid" x1={L} x2={W - R} y1={y(h)} y2={y(h)} /><text className="tick" x={L - 6} y={y(h) + 4} textAnchor="end">{h}h</text></g>)}
        {days.filter((_, i) => i % labelEvery === 0).map(d => {
          const t = Date.parse(`${d}T00:00:00Z`) / 1000
          return <text key={d} className="tick" x={Math.min(W - R - 16, Math.max(L + 16, x(t)))} y={H - 6} textAnchor="middle">{d.slice(5)}</text>
        })}
        {pts.map(p => {
          const cx = x(p.run.started_ts!) - bw / 2
          const failed = p.run.status === 'failed'
          const main = p.spans.filter(s => s.lane === 0)
          const where = p.failed_at ? ` · failed ${p.failed_at.startsWith('after') ? p.failed_at : `in ${p.failed_at}`}` : ''
          return (
            <g key={p.run.run_id} className={`sr-col${failed ? ' failed' : ''}`} onClick={() => onPick(p.run.run_id)} role="link" aria-label={`${p.run.scan} ${p.run.kind} run`}
              {...tip.on(<><div><b>{p.run.scan}</b> · {p.run.kind} · {p.run.status === 'nop' ? 'no-op' : p.run.status}</div><div>{fmtDur(p.secs!)}{where}</div>{main.map(s => <div key={s.phase}><i className="sw" style={{ background: color(s.phase) }} />{s.phase} {fmtDur(s.end - s.start)}</div>)}</>)}>
              <rect className="sr-col-hit" x={cx - 2} y={T} width={bw + 4} height={H - T - B} />
              <rect className="sr-col-total" x={cx} y={y(p.secs! / 3600)} width={bw} height={Math.max(1, y(0) - y(p.secs! / 3600))} />
              {!failed && main.map(s => (
                <rect key={s.phase} x={cx} y={y(s.end / 3600)} width={bw} height={Math.max(0.5, y(s.start / 3600) - y(s.end / 3600))} style={{ fill: color(s.phase) }} />
              ))}
            </g>
          )
        })}
        <line className="axis" x1={L} x2={W - R} y1={y(0)} y2={y(0)} />
      </svg>
      {tip.el}
    </div>
  )
}

function Legend({ order, color }: { order: string[]; color: (p: string) => string }) {
  if (!order.length) return null
  return <div className="sr-legend">{order.map(p => <span key={p}><i className="sw" style={{ background: color(p) }} />{p}</span>)}<span><i className="sw failed" />failed</span></div>
}

function Links({ run, links }: { run: ScanRun; links: Links }) {
  return <>
    {links.project && run.job_name && run.region && <a href={batchHref(links.project, run.region, run.job_name)} target="_blank" rel="noreferrer">Batch ↗</a>}
    {links.project && <> · <a href={logsHref(links.project, run)} target="_blank" rel="noreferrer">logs ↗</a></>}
  </>
}

export function ScansPage() {
  const q = useScanRuns()
  const nav = useNavigate()
  const { bytes, delta } = useBytes()
  const [kind, setKind] = useState('')
  const [status, setStatus] = useState('')
  useEffect(() => { document.title = 'Scan runs' }, [])
  const runs = q.data?.runs ?? []
  const order = useMemo(() => phaseOrder(runs.flatMap(r => r.spans.map((s, i) => ({ phase: s.phase, seq: i })))), [runs])
  const color = colorer(order)
  const kinds = [...new Set(runs.map(r => r.run.kind))].sort()
  const shown = runs.filter(r => (!kind || r.run.kind === kind) && (!status || r.run.status === status))
  return (
    <main className="staged-page scans-page">
      <SiteNav />
      <h1>Scan runs</h1>
      <p className="sub">Each scan job's run: when it ran, how long each phase took, what it wrote, and how each structure changed against the previous scan.</p>
      {q.isLoading ? <p>Loading runs…</p> : q.error ? <p className="err">{q.error.message}</p> : !q.data?.configured ? (
        <p className="dim">Scan runs aren't recorded on this deployment yet (its D1 has no <code>scan_runs</code> table: migration <code>scan_runs</code>, then <code>dt-cloud scan-run backfill</code>).</p>
      ) : (<>
        <section className="sr-chart">
          <h3>Duration by run</h3>
          <DurationStrip runs={shown} color={color} onPick={id => nav(scanHref(id))} />
          <Legend order={order} color={color} />
        </section>
        <div className="run-filters">
          <select aria-label="Filter by kind" value={kind} onChange={e => setKind(e.target.value)}><option value="">all kinds</option>{kinds.map(k => <option key={k}>{k}</option>)}</select>
          <select aria-label="Filter by status" value={status} onChange={e => setStatus(e.target.value)}><option value="">all statuses</option>{['succeeded', 'failed', 'running', 'nop'].map(s => <option key={s} value={s}>{s === 'nop' ? 'no-op' : s}</option>)}</select>
          <span className="dim">{shown.length} / {runs.length} runs</span>
        </div>
        <div className="runs-wrap">
          <table className="runs sr-runs">
            <thead><tr>
              <th>scan</th><th>kind</th><th>status</th><th>started (UTC)</th>
              <th className="num">duration</th><th>phases</th>
              <th className="num"><Tooltip content="Bytes of the run's top-level outputs (listing, index, snapshot, …).">wrote</Tooltip></th>
              <th className="num"><Tooltip content="Against the previous scan's same structures. Green grew, red shrank.">Δ prev</Tooltip></th>
              <th className="num"><Tooltip content="Compute estimate: the machine's list $/h × run hours × tasks, including the run's downstream jobs (listing fan-out).">cost</Tooltip></th>
              <th>links</th>
            </tr></thead>
            <tbody>
              {shown.map(s => {
                const r = s.run
                const map = r.status === 'succeeded' ? mapHref(r.scan, DEFAULT_STORE.path) : null
                return (
                  <tr key={r.run_id} className={r.status === 'failed' ? 'failed' : undefined}>
                    <td className="nb"><Link to={scanHref(r.run_id)}><code>{r.scan}</code></Link></td>
                    <td>{r.kind}{s.downstream > 0 && <Tooltip content={`${s.downstream} downstream job${s.downstream > 1 ? 's' : ''} (listing fan-out, …)`}><span className="dim"> +{s.downstream}</span></Tooltip>}</td>
                    <td><Status run={r} failedAt={s.failed_at} /></td>
                    <td className="nb">{utc(r.started_ts)}</td>
                    <td className="num nb">{s.secs == null ? <span className="dim">—</span> : fmtDur(s.secs)}</td>
                    <td><PhaseBar spans={s.spans} secs={s.secs} color={color} /></td>
                    <td className="num nb">{bytes(s.out_bytes)}</td>
                    <td className={`num nb ${deltaClass(s.delta_bytes)}`}>{delta(s.delta_bytes)}</td>
                    <td className="num nb">{s.cost_usd == null ? <span className="dim">—</span> : usd(s.cost_usd)}</td>
                    <td className="nb links"><Link to={scanHref(r.run_id)}>details</Link>{map && <> · <Link to={map}>map</Link></>}{' · '}<Links run={r} links={q.data!} /></td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      </>)}
      <SiteKbd />
    </main>
  )
}

/** The run's phases on its clock: one row per phase. */
function PhaseTimeline({ spans, secs, color }: { spans: PhaseSpan[]; secs: number | null; color: (p: string) => string }) {
  const tip = useSvgTip()
  if (!spans.length || !secs) return <p className="dim">No phase markers for this run (the job records its phases; the backfill recovers them from the last 30 days of logs).</p>
  const W = 1000, rowH = 24, L = 170, R = 80
  const H = spans.length * rowH + 22
  const x = (t: number) => L + (t / secs) * (W - L - R)
  const step = [60, 300, 600, 900, 1800, 3600, 7200, 14400].find(s => secs / s <= 8) ?? 28800
  const ticks = Array.from({ length: Math.floor(secs / step) + 1 }, (_, i) => i * step)
  return (
    <div className="sr-svgwrap">
      <svg className="sr-gantt" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Phase timeline">
        {ticks.map(t => <g key={t}><line className="grid" x1={x(t)} x2={x(t)} y1={0} y2={H - 18} /><text className="tick" x={x(t)} y={H - 4} textAnchor="middle">{t ? fmtDur(t).replace(' 00m', 'h') : '0'}</text></g>)}
        {spans.map((s, i) => (
          <g key={s.phase} {...tip.on(<><div><b>{s.phase}</b>{s.lane ? ' — beside the main sequence' : ''}</div><div>{fmtDur(s.start)} → {fmtDur(s.end)} · {fmtDur(s.end - s.start)}</div>{s.note && <div className="dim">{s.note}</div>}</>)}>
            <rect className="sr-row-hit" x={0} y={i * rowH} width={W} height={rowH} />
            <text className="lbl" x={L - 8} y={i * rowH + 16} textAnchor="end">{s.phase.length > 24 ? `${s.phase.slice(0, 23)}…` : s.phase}</text>
            <rect x={x(s.start)} y={i * rowH + 5} width={Math.max(2, x(s.end) - x(s.start))} height={rowH - 10} rx={2} style={{ fill: color(s.phase) }} className={s.status === 'failed' ? 'failed' : undefined} />
            <text className="dur" x={x(s.end) + 6} y={i * rowH + 16}>{fmtDur(s.end - s.start)}</text>
          </g>
        ))}
      </svg>
      {tip.el}
    </div>
  )
}

function OutputsTable({ outputs, meta }: { outputs: OutputDelta[]; meta: string | null }) {
  const { bytes, delta } = useBytes()
  const [open, setOpen] = useState<ReadonlySet<string>>(new Set())
  if (!outputs.length) return <p className="dim">No outputs recorded for this run.</p>
  const tops = outputs.filter(o => !o.key.includes('/'))
  const kids = (k: string) => outputs.filter(o => o.key.startsWith(`${k}/`))
  const row = (o: OutputDelta, child: boolean) => {
    const href = metaHref(meta, o.uri)
    const n = child ? 0 : kids(o.key).length
    const isOpen = open.has(o.key)
    return (
      <tr key={o.key} className={child ? 'child' : undefined}>
        <td className="nb">
          {n > 0 ? <button type="button" className="fold" aria-expanded={isOpen} onClick={() => setOpen(s => { const t = new Set(s); if (isOpen) t.delete(o.key); else t.add(o.key); return t })}>{isOpen ? '▾' : '▸'}</button> : <span className="fold-pad" />}
          <Tooltip content={<code>{o.uri}</code>}><span>{child ? o.key.split('/').slice(1).join('/') : o.key}</span></Tooltip>
          {n > 0 && <span className="dim"> ({n})</span>}
        </td>
        <td className="num nb">{bytes(o.bytes)}</td>
        <td className={`num nb ${deltaClass(o.delta_bytes)}`}>{delta(o.delta_bytes)}</td>
        <td className="num nb">{o.objects == null ? '—' : fmtN(o.objects)}</td>
        <td className="num nb">{o.rows == null ? <span className="dim">—</span> : fmtN(o.rows)}</td>
        <td className={`num nb ${deltaClass(o.delta_rows)}`}>{o.delta_rows == null ? '—' : `${o.delta_rows > 0 ? '+' : o.delta_rows < 0 ? '−' : '±'}${fmtN(Math.abs(o.delta_rows))}`}</td>
        <td className="nb">{o.gen ? <code className="dim">{o.gen}</code> : ''}</td>
        <td className="nb">{href ? <a href={href} target="_blank" rel="noreferrer">meta ↗</a> : <span className="dim">—</span>}</td>
      </tr>
    )
  }
  return (
    <div className="runs-wrap">
      <table className="runs sr-outputs">
        <thead><tr>
          <th>structure</th><th className="num">bytes</th>
          <th className="num"><Tooltip content={`Against the previous scan${tops[0]?.prev_scan ? ` (${tops[0].prev_scan})` : ''}: the same structure's bytes.`}>Δ prev</Tooltip></th>
          <th className="num">objects</th><th className="num">rows</th><th className="num">Δ rows</th><th>gen</th>
          <th><Tooltip content="The output's directory in the /meta tree (the deployments' own data bucket, scanned periodically: a just-written output appears after its next meta scan).">tree</Tooltip></th>
        </tr></thead>
        <tbody>{tops.flatMap(o => [row(o, false), ...(open.has(o.key) ? kids(o.key).map(k => row(k, true)) : [])])}</tbody>
      </table>
    </div>
  )
}

function RunList({ runs, links, title }: { runs: ScanRun[]; links: Links; title: string }) {
  if (!runs.length) return null
  return <>
    <h3>{title}</h3>
    <div className="runs-wrap">
      <table className="runs">
        <thead><tr><th>run</th><th>kind</th><th>status</th><th>started (UTC)</th><th className="num">duration</th><th>machine</th><th className="num">cost</th><th>links</th></tr></thead>
        <tbody>{runs.map(r => (
          <tr key={r.run_id} className={r.status === 'failed' ? 'failed' : undefined}>
            <td className="nb">{r.parent ? <code>{r.job_name ?? r.run_id}</code> : <Link to={scanHref(r.run_id)}><code>{r.job_name ?? r.run_id}</code></Link>}</td>
            <td>{r.kind}</td><td><Status run={r} /></td><td className="nb">{utc(r.started_ts)}</td>
            <td className="num nb">{r.started_ts != null && r.finished_ts != null ? fmtDur(r.finished_ts - r.started_ts) : '—'}</td>
            <td className="nb">{r.machine ?? '—'}{r.tasks && r.tasks > 1 ? ` × ${r.tasks}` : ''}{r.spot ? ' (spot)' : ''}</td>
            <td className="num nb">{r.cost_usd == null ? '—' : usd(r.cost_usd)}</td>
            <td className="nb"><Links run={r} links={links} /></td>
          </tr>
        ))}</tbody>
      </table>
    </div>
  </>
}

export function ScanRunPage() {
  const id = useParams()['*'] ?? ''
  const q = useScanRun(id)
  const list = useScanRuns()
  useEffect(() => { document.title = q.data ? `Scan run ${q.data.run.scan}` : 'Scan run' }, [q.data])
  const order = useMemo(() => phaseOrder((list.data?.runs ?? []).flatMap(r => r.spans.map((s, i) => ({ phase: s.phase, seq: i })))), [list.data])
  const color = colorer(order.length ? order : (q.data?.spans ?? []).map(s => s.phase))
  const d = q.data
  const scanRun = (scan: string | null) => scan ? list.data?.runs.find(s => s.run.scan === scan && s.run.kind === 'scan' && s.run.status === 'succeeded') ?? list.data?.runs.find(s => s.run.scan === scan) : undefined
  const prev = scanRun(d?.prev_scan ?? null), next = scanRun(d?.next_scan ?? null)
  const r = d?.run
  const map = r && r.status === 'succeeded' ? mapHref(r.scan, DEFAULT_STORE.path) : null
  return (
    <main className="staged-page scans-page">
      <SiteNav />
      <p className="sr-crumbs"><Link to="/scans">Scan runs</Link>{prev && <> · <Link to={scanHref(prev.run.run_id)}>‹ {prev.run.scan}</Link></>}{next && <> · <Link to={scanHref(next.run.run_id)}>{next.run.scan} ›</Link></>}</p>
      {q.isLoading ? <p>Loading run…</p> : q.error ? <p className="err">{q.error.message}</p> : d && r && <>
        <h1><code>{r.scan}</code> <span className="dim">{r.kind}</span></h1>
        <p className="sub">
          <Status run={r} failedAt={d.failed_at} /> · {utc(r.started_ts)} → {r.finished_ts ? utc(r.finished_ts).slice(11) : '…'} UTC
          {d.secs != null && <> · {fmtDur(d.secs)}</>}
          {r.machine && <> · {r.machine}{r.spot ? ' (spot)' : ''}</>}
          {r.cost_usd != null && <> · ~{usd(r.cost_usd)}</>}
          {map && <> · <Link to={map}>map at this scan</Link></>}
          {' · '}<Links run={r} links={d} />
        </p>
        {r.status === 'failed' && <div className="sr-failure"><b>Failed {d.failed_at ? (d.failed_at.startsWith('after') ? d.failed_at : `in ${d.failed_at}`) : ''}</b>{r.error && <div><code>{r.error}</code></div>}</div>}
        <dl className="sr-facts">
          <div><dt>run id</dt><dd><code>{r.run_id}</code></dd></div>
          {r.job_name && <div><dt>Batch job</dt><dd><code>{r.job_name}</code>{r.region && <span className="dim"> · {r.region}</span>}</dd></div>}
          {r.gen && <div><dt>index generation</dt><dd><code>{r.gen}</code></dd></div>}
          {r.image && <div><dt>image</dt><dd><code>{r.image}</code></dd></div>}
          <div><dt>recorded by</dt><dd>{r.source === 'job' ? 'the job' : 'backfill (Batch, logs, the bucket)'}</dd></div>
        </dl>
        <h3>Phases</h3>
        <PhaseTimeline spans={d.spans} secs={d.secs} color={color} />
        <h3>Outputs</h3>
        <OutputsTable outputs={d.outputs} meta={d.meta} />
        <RunList runs={d.downstream} links={d} title={`Downstream jobs (${d.downstream.length})`} />
        <RunList runs={d.siblings} links={d} title="This scan's other runs" />
      </>}
      <SiteKbd />
    </main>
  )
}

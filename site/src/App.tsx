import { useEffect, useMemo, useState } from 'react'
import { AgeChart } from './AgeChart'
import { AttributionRules } from './AttributionRules'
import { buildUserIndex } from './colors'
import { Treemap } from './Treemap'
import type { DateRange } from './Treemap'
import type { AgeRow, ColorMode, Meta, Rules, TreeNode } from './types'
import { MODE_LABELS, fmtBytes, fmtN } from './types'

const CLASS_NAMES: Record<string, string> = { 1: 'Standard', 2: 'Nearline', 3: 'Coldline', 4: 'Archive' }
const CLASS_PRICE_US: Record<string, number> = { 1: 0.02, 2: 0.01, 3: 0.004, 4: 0.0012 }

// CF Access identity (present when served behind gcs.oa.dev; absent in local dev)
interface Identity { email: string; name?: string }

function useIdentity(): Identity | null {
  const [ident, setIdent] = useState<Identity | null>(null)
  useEffect(() => {
    void fetch('/cdn-cgi/access/get-identity')
      .then(r => (r.ok ? r.json() : null))
      .then(d => d?.email && setIdent(d))
      .catch(() => {})
  }, [])
  return ident
}

const avatarHue = (s: string): number => {
  let h = 0
  for (const c of s) h = (h * 31 + c.codePointAt(0)!) % 360
  return h
}

export default function App() {
  const [tree, setTree] = useState<TreeNode | null>(null)
  const [age, setAge] = useState<AgeRow[]>([])
  const [meta, setMeta] = useState<Meta | null>(null)
  const [rules, setRules] = useState<Rules | null>(null)
  const [mode, setMode] = useState<ColorMode>('team')
  const [scans, setScans] = useState<string[]>([])
  const [asof, setAsof] = useState<string | null>(null)
  const ident = useIdentity()
  const hasAttr = !!tree?.tm
  const effMode: ColorMode = hasAttr ? mode : 'tree'

  const userIdx = useMemo(() => buildUserIndex(meta?.users ?? []), [meta])

  const dateRange = useMemo((): DateRange | null => {
    if (!tree) return null
    let min = Infinity
    let max = -Infinity
    const walk = (n: TreeNode) => {
      if (n.d != null && !n.c) {
        if (n.d < min) min = n.d
        if (n.d > max) max = n.d
      }
      n.c?.forEach(walk)
    }
    walk(tree)
    return min < max ? { min, max } : null
  }, [tree])

  useEffect(() => {
    void fetch('/data/scans.json').then(r => r.json()).then((list: string[]) => {
      setScans(list)
      const param = new URLSearchParams(location.search).get('scan')
      setAsof(param && list.includes(param) ? param : list[0])
    })
    void fetch('/data/rules.json').then(r => r.json()).then(setRules).catch(() => {})
  }, [])

  useEffect(() => {
    if (!asof) return
    setTree(null)
    void fetch(`/data/${asof}/tree.json`).then(r => r.json()).then(setTree)
    void fetch(`/data/${asof}/age.json`).then(r => r.json()).then(setAge)
    void fetch(`/data/${asof}/meta.json`).then(r => r.json()).then(setMeta)
    const url = new URL(location.href)
    if (asof === scans[0]) url.searchParams.delete('scan')
    else url.searchParams.set('scan', asof)
    history.replaceState(null, '', url)
  }, [asof, scans])

  const catOrder = useMemo(() => {
    if (!tree) return []
    const catBytes = new Map<string, number>()
    for (const bucket of tree.c ?? [])
      for (const d of bucket.c ?? []) {
        const k = d.n.startsWith('(') ? '(other)' : d.n
        catBytes.set(k, (catBytes.get(k) ?? 0) + d.b)
      }
    return [...catBytes.entries()].sort((a, b) => b[1] - a[1]).map(([k]) => k).filter(k => k !== '(other)')
  }, [tree])

  const estCost = useMemo(() => {
    if (!meta) return null
    const gib = (b: number) => b / 1024 ** 3
    const list = Object.entries(meta.class_bytes).reduce(
      (s, [c, b]) => s + gib(b) * (CLASS_PRICE_US[c] ?? 0.02),
      0,
    )
    return { list, discounted: list * 0.7 }
  }, [meta])

  return (
    <main>
      <header>
        <div className="hrow">
          <h1>Marin GCS usage</h1>
          {ident && (
            <div className="whoami">
              <span className="avatar" style={{ background: `hsl(${avatarHue(ident.email)} 55% 42%)` }} title={ident.name || ident.email}>
                {(ident.name || ident.email).trim()[0].toUpperCase()}
              </span>
              <span className="email">{ident.email}</span>
              <a className="logout" href="/cdn-cgi/access/logout">log out</a>
            </div>
          )}
        </div>
        {meta && (
          <p className="sub">
            scan{' '}
            {scans.length > 1 && asof ? (
              <select className="scanpick" value={asof} onChange={e => setAsof(e.target.value)} aria-label="Scan date">
                {scans.map(s => <option key={s} value={s}>{s}</option>)}
              </select>
            ) : (
              <b>{meta.asof}</b>
            )}
            {' '}· <b>{fmtBytes(meta.total_bytes)}</b> · <b>{fmtN(meta.total_objects)}</b> objects
            {estCost && (
              <>
                {' '}· est. <b>${Math.round(estCost.list).toLocaleString()}/mo</b> list
                {' '}(<b>${Math.round(estCost.discounted).toLocaleString()}</b> at −30%)
              </>
            )}
          </p>
        )}
      </header>

      <section className="prose">
        <p>
          Storage across the six <code>marin-*</code> GCS buckets, from the weekly{' '}
          <a href="https://github.com/marin-community/marin/blob/main/scripts/ops/storage/" target="_blank" rel="noreferrer">Ops&nbsp;-&nbsp;Storage&nbsp;Report</a>{' '}
          scan (per-object listing, deduped). Treemap drills into prefixes; the “color by” control recolors
          both plots — by owning team, top-level tree, age (older→newer), or owning user (hi-contrast, or
          hues grouped by team). Ownership comes from the{' '}
          <code>marin-gcs-usage</code> attribution pipeline (W&B run/config joins, executor sidecars, manual
          curation) — hover a cell for its team split and top users.
        </p>
      </section>

      {hasAttr && (
        <div className="colorctl" role="radiogroup" aria-label="Color plots by">
          <span className="lbl">color by</span>
          {(Object.keys(MODE_LABELS) as ColorMode[]).map(m => (
            <button
              key={m}
              role="radio"
              aria-checked={effMode === m}
              className={effMode === m ? 'on' : ''}
              onClick={() => setMode(m)}
            >
              {MODE_LABELS[m]}
            </button>
          ))}
        </div>
      )}

      {tree ? (
        <Treemap root={tree} mode={effMode} userIdx={userIdx} dateRange={dateRange} />
      ) : (
        <p className="loading">loading tree…</p>
      )}

      <section>
        <h2>Bytes by created month</h2>
        <p className="sub">
          When today’s objects were written (created-time strata, colored by {MODE_LABELS[effMode]}).
        </p>
        {age.length > 0 && <AgeChart rows={age} catOrder={catOrder} mode={effMode} userIdx={userIdx} />}
      </section>

      {rules && tree?.tm && meta?.users && (
        <AttributionRules rules={rules} tree={tree} users={meta.users} />
      )}

      {meta && (
        <section>
          <h2>Storage classes</h2>
          <table className="classes">
            <thead>
              <tr><th>class</th><th>bytes</th><th>est. $/mo (list, US)</th></tr>
            </thead>
            <tbody>
              {Object.entries(meta.class_bytes)
                .sort((a, b) => b[1] - a[1])
                .map(([c, b]) => (
                  <tr key={c}>
                    <td>{CLASS_NAMES[c] ?? c}</td>
                    <td>{fmtBytes(b)}</td>
                    <td>${Math.round((b / 1024 ** 3) * (CLASS_PRICE_US[c] ?? 0.02)).toLocaleString()}</td>
                  </tr>
                ))}
            </tbody>
          </table>
        </section>
      )}
    </main>
  )
}

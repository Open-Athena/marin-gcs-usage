import { useEffect, useMemo, useState } from 'react'
import { AgeChart } from './AgeChart'
import { Treemap } from './Treemap'
import type { AgeRow, ColorMode, Meta, TreeNode } from './types'
import { fmtBytes, fmtN } from './types'

const CLASS_NAMES: Record<string, string> = { 1: 'Standard', 2: 'Nearline', 3: 'Coldline', 4: 'Archive' }
const CLASS_PRICE_US: Record<string, number> = { 1: 0.02, 2: 0.01, 3: 0.004, 4: 0.0012 }

export default function App() {
  const [tree, setTree] = useState<TreeNode | null>(null)
  const [age, setAge] = useState<AgeRow[]>([])
  const [meta, setMeta] = useState<Meta | null>(null)
  const [mode, setMode] = useState<ColorMode>('team')
  const hasAttr = !!tree?.tm
  const effMode: ColorMode = hasAttr ? mode : 'tree'

  useEffect(() => {
    void fetch('/data/tree.json').then(r => r.json()).then(setTree)
    void fetch('/data/age.json').then(r => r.json()).then(setAge)
    void fetch('/data/meta.json').then(r => r.json()).then(setMeta)
  }, [])

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
        <h1>Marin GCS usage</h1>
        {meta && (
          <p className="sub">
            scan <b>{meta.asof}</b> · <b>{fmtBytes(meta.total_bytes)}</b> · <b>{fmtN(meta.total_objects)}</b> objects
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
          <a href="https://github.com/marin-community/marin/blob/main/scripts/ops/storage/">Ops&nbsp;-&nbsp;Storage&nbsp;Report</a>{' '}
          scan (per-object listing, deduped). Treemap drills into prefixes; the “color by” control switches
          both plots between owning-team and top-level-tree palettes. Ownership comes from the{' '}
          <code>marin-gcs-usage</code> attribution pipeline (W&B run/config joins, executor sidecars, manual
          curation) — hover a cell for its team split and top users.
        </p>
      </section>

      {hasAttr && (
        <div className="colorctl" role="radiogroup" aria-label="Color plots by">
          <span className="lbl">color by</span>
          {(['team', 'tree'] as ColorMode[]).map(m => (
            <button
              key={m}
              role="radio"
              aria-checked={effMode === m}
              className={effMode === m ? 'on' : ''}
              onClick={() => setMode(m)}
            >
              {m === 'team' ? 'owning team' : 'top-level tree'}
            </button>
          ))}
        </div>
      )}

      {tree ? <Treemap root={tree} mode={effMode} /> : <p className="loading">loading tree…</p>}

      <section>
        <h2>Bytes by created month</h2>
        <p className="sub">
          When today’s objects were written (created-time strata, colored by{' '}
          {effMode === 'team' ? 'owning team' : 'top-level tree'}).
        </p>
        {age.length > 0 && <AgeChart rows={age} catOrder={catOrder} mode={effMode} />}
      </section>

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

import { useMemo } from 'react'
import type { CSSProperties } from 'react'
import { Treemap as DtTreemap, divergingColor, divergingInk } from '@disk-tree/react'
import { boolParam, useUrlState } from 'use-prms'
import { useUnits } from './units'

const { abs, max, min, sign } = Math

// Ported from disk-tree's CompareView (ui/src/components/CompareView.tsx):
// `divergingColor` is red-positive, so negate on the way in.
const UNCHANGED_GREY = 'rgba(110, 118, 129, 0.28)'
const deltaColor = (t: number) => divergingColor(-t)

// diff.json row (job/cw-diff.py): p=path d=depth k=kind s=status a/b=bytes
// oa/ob=n_desc x=expanded pr=pruned.
export interface DiffRow {
  p: string
  d: number
  k: string
  s: 'added' | 'removed' | 'changed' | 'unchanged'
  a: number
  b: number
  oa: number
  ob: number
  x?: boolean
  pr?: boolean
}

export interface DiffData {
  prev: string | null
  curr: string | null
  total_a: number
  total_b: number
  objects_a: number
  objects_b: number
  expansions: number
  truncated: boolean
  rows: DiffRow[]
}

type AreaMode = 'max' | 'delta'

interface DiffNode {
  key: string
  label: string
  weight: number
  delta: number
  status: DiffRow['s'] | 'filler' | 'root'
  size_old: number
  size_new: number
  n_desc_delta: number
  pruned?: boolean
  children?: DiffNode[]
}

/**
 * Frontier rows → nested tree. Weights are bottom-up: a leaf is
 * `max(old, new)` (or `|Δ|` in Δ mode); a parent is `max(own, Σ children)` —
 * churn (delete X + add Y) makes children sum past either side's bytes.
 * Under-filled parents get a grey `(unchanged)` filler cell so areas stay
 * truthful without shipping every unchanged row.
 */
function buildTree(data: DiffData, areaMode: AreaMode): { cells: DiffNode[]; maxAbsDelta: number } {
  const byPath = new Map<string, DiffNode>()
  const roots: DiffNode[] = []
  const attach = (node: DiffNode, path: string) => {
    byPath.set(path, node)
    const i = path.lastIndexOf('/')
    if (i < 0) {
      roots.push(node)
      return
    }
    const parentPath = path.slice(0, i)
    let parent = byPath.get(parentPath)
    if (!parent) {
      // Expanded-but-net-zero dir whose children were emitted without it.
      parent = {
        key: parentPath, label: parentPath.split('/').pop()!, weight: 0, delta: 0,
        status: 'unchanged', size_old: 0, size_new: 0, n_desc_delta: 0, children: [],
      }
      attach(parent, parentPath)
    }
    ;(parent.children ??= []).push(node)
  }

  const rows = [...data.rows].sort((a, b) => a.d - b.d || a.p.localeCompare(b.p))
  for (const r of rows) {
    attach({
      key: r.p,
      label: r.p.split('/').pop() || r.p,
      weight: 0,
      delta: r.b - r.a,
      status: r.s,
      size_old: r.a,
      size_new: r.b,
      n_desc_delta: r.ob - r.oa,
      pruned: r.pr,
    }, r.p)
  }

  let maxAbs = 0
  const finalize = (node: DiffNode): number => {
    maxAbs = max(maxAbs, abs(node.delta))
    const own = areaMode === 'max' ? max(node.size_old, node.size_new) : abs(node.delta)
    if (!node.children?.length) {
      node.weight = own
      return node.weight
    }
    let kidSum = 0
    for (const k of node.children) kidSum += finalize(k)
    node.weight = max(own, kidSum)
    const gap = node.weight - kidSum
    if (areaMode === 'max' && gap > max(1_000_000, node.weight * 0.002)) {
      node.children.push({
        key: `${node.key}/__unchanged__`, label: '(unchanged)', weight: gap, delta: 0,
        status: 'filler', size_old: gap, size_new: gap, n_desc_delta: 0,
      })
    }
    node.children.sort((a, b) => (b.delta - a.delta) || (b.weight - a.weight))
    return node.weight
  }
  for (const r of roots) finalize(r)

  const cells = roots.filter(r => r.weight > 0)
  cells.sort(areaMode === 'max'
    ? (a, b) => (b.delta - a.delta) || (b.weight - a.weight)
    : (a, b) => b.weight - a.weight)
  return { cells, maxAbsDelta: maxAbs }
}

export function DiffTreemap({ data, label }: { data: DiffData; label: string }) {
  const { fmtBytes } = useUnits()
  const fmtDelta = (d: number) => (d >= 0 ? '+' : '−') + fmtBytes(abs(d))
  // Area mode is shareable state: `?dd` (bare flag) = Δ mode; `max` is the
  // default and stays out of the URL.
  const [dd, setDd] = useUrlState('dd', boolParam)
  const areaMode: AreaMode = dd ? 'delta' : 'max'
  const setAreaMode = (m: AreaMode) => setDd(m === 'delta')
  // Root arithmetic for the crumb: start − removed + added = end. Bytes move
  // at the frontier (non-expanded rows; an expanded row's own Δ is carried by
  // its children), so sum the frontier's signed deltas. A truncated walk (or a
  // client-side alignment) can leave small movements unenumerated, in which
  // case the two terms are approximate — the endpoints are always exact.
  const { added, removed } = useMemo(() => {
    let added = 0
    let removed = 0
    for (const r of data.rows) {
      if (r.x) continue
      const d = r.b - r.a
      if (d > 0) added += d
      else removed -= d
    }
    return { added, removed }
  }, [data])
  const { root, maxAbsDelta } = useMemo(() => {
    const { cells, maxAbsDelta: maxAbs } = buildTree(data, areaMode)
    const root: DiffNode = {
      key: label,
      label,
      weight: cells.reduce((s, c) => s + c.weight, 0),
      delta: data.total_b - data.total_a,
      status: 'root',
      size_old: data.total_a,
      size_new: data.total_b,
      n_desc_delta: data.objects_b - data.objects_a,
      children: cells,
    }
    return { root, maxAbsDelta: maxAbs }
  }, [data, areaMode, label])

  if (!root.children?.length) return null

  return (
    <div className="diff-tm">
      <DtTreemap<DiffNode>
        root={root}
        getSize={n => n.weight}
        getChildren={n => n.children}
        getLabel={n => n.label}
        getId={(_n, p) => p.map(x => x.key).join('|')}
        formatSize={n => fmtBytes(n)}
        // The core's default suffix is the node's *area weight* (Σ max(old,new),
        // or Σ|Δ|), which reads as a nonsense total next to the header's scan
        // size. Show the movement instead: start − removed + added = end.
        renderCrumbSuffix={n => n.status === 'root'
          ? <>
              — {fmtBytes(n.size_old)}{' '}
              <span className="shrank">− {fmtBytes(removed)}</span>{' '}
              <span className="grew">+ {fmtBytes(added)}</span>{' '}
              {data.truncated || added - removed !== n.delta ? '≈' : '='} {fmtBytes(n.size_new)}{' '}
              <span className={n.delta >= 0 ? 'grew' : 'shrank'}>({fmtDelta(n.delta)})</span>
            </>
          : <>— {fmtBytes(n.size_old)} → {fmtBytes(n.size_new)} <span className={n.delta >= 0 ? 'grew' : 'shrank'}>({fmtDelta(n.delta)})</span></>}
        collapseChains
        depthFade={1}
        rootFade={1}
        colorForCell={n => {
          if (areaMode === 'max') {
            if (n.children?.length) {
              const t = n.weight === 0 ? 0 : n.delta / n.weight
              return { bg: deltaColor(t), ink: divergingInk(t) }
            }
            const f = n.weight === 0 ? 0 : min(1, abs(n.delta) / n.weight)
            if (f === 0) return { bg: UNCHANGED_GREY, ink: divergingInk(0) }
            const pct = `${(f * 100).toFixed(2)}%`
            const band = deltaColor(sign(n.delta))
            return {
              bg: `linear-gradient(to top, ${band} ${pct}, ${UNCHANGED_GREY} ${pct})`,
              ink: divergingInk(f > 0.85 ? 1 : 0),
            }
          }
          const t = maxAbsDelta === 0 ? 0 : n.delta / maxAbsDelta
          return { bg: deltaColor(t), ink: divergingInk(t) }
        }}
        renderCellExtra={areaMode === 'max' ? (n, _path, { w, h }) => {
          if (n.children?.length || w < 56) return null
          const f = n.weight === 0 ? 0 : min(1, abs(n.delta) / n.weight)
          if (f === 0) return null
          const bandH = h * f
          const greyH = h - bandH
          const minBytes = min(n.size_old, n.size_new)
          const lbl = (top: string, height: number, text: string, style: CSSProperties) => (
            <div style={{
              position: 'absolute', top, left: 0, right: 0, height,
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              pointerEvents: 'none', fontSize: '0.75rem', ...style,
            }}>{text}</div>
          )
          return (
            <>
              {bandH >= 16 && lbl(`${(100 - f * 100).toFixed(2)}%`, bandH,
                fmtDelta(n.delta), { color: '#fff', fontWeight: 600 })}
              {f < 1 && greyH >= 44 && minBytes > 0 && lbl('0', greyH,
                fmtBytes(minBytes), { color: 'var(--ink-2)', opacity: 0.65 })}
            </>
          )
        } : undefined}
        renderTooltip={n => (
          <>
            <div style={{ fontWeight: 500 }}>{n.key}</div>
            <div style={{ opacity: 0.75, fontSize: '0.85em' }}>
              {fmtBytes(n.size_old)} → {fmtBytes(n.size_new)} ({fmtDelta(n.delta)})
            </div>
            {n.n_desc_delta !== 0 && (
              <div style={{ opacity: 0.6, fontSize: '0.8em' }}>
                Δobjects: {n.n_desc_delta > 0 ? '+' : ''}{n.n_desc_delta.toLocaleString('en-US')}
              </div>
            )}
            <div style={{ opacity: 0.5, fontSize: '0.75em', marginTop: 2 }}>
              {n.status === 'filler' ? 'unchanged bytes the diff never needed to enumerate' : n.status}
              {n.pruned && ' · more change below (walk budget)'}
            </div>
          </>
        )}
        renderLegend={() => (
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: '0.8rem', opacity: 0.85 }}>
            <span style={{ display: 'inline-block', width: 12, height: 12, background: deltaColor(1), borderRadius: 2 }} />
            grew
            <span style={{ display: 'inline-block', width: 12, height: 12, background: deltaColor(-1), borderRadius: 2 }} />
            shrank
            {areaMode === 'max' && <>
              <span style={{ display: 'inline-block', width: 12, height: 12, background: UNCHANGED_GREY, borderRadius: 2 }} />
              unchanged
            </>}
            <span style={{ opacity: 0.6, marginLeft: 4 }}>
              {areaMode === 'max' ? 'area = max(old, new), band = |Δ|' : 'area = |Δ|'}
            </span>
            <span style={{ display: 'inline-flex', gap: 2, marginLeft: 6 }}>
              {(['max', 'delta'] as const).map(m => (
                <button
                  key={m}
                  onClick={e => { e.stopPropagation(); setAreaMode(m) }}
                  className={'diff-mode' + (areaMode === m ? ' on' : '')}
                >
                  {m === 'max' ? 'max' : 'Δ'}
                </button>
              ))}
            </span>
          </span>
        )}
      />
    </div>
  )
}

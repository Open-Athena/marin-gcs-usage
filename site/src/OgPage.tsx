import { useEffect, useMemo, useState } from 'react'
import { Treemap } from './Treemap'
import type { Store } from './stores'
import { DEFAULT_STORE } from './stores'
import type { TreeNode } from './types'
import { TEAM_VARS, groupLabel, sharedColor } from './types'
import type { UserIndexEntry } from './colors'

// `<store>/og` — a redacted, fixed-size (1200×630) render of that store's
// treemap, used only to screenshot its public og:image. Reuses <Treemap redact>
// so the cell layout + colors match the live view exactly, but every text
// detail is dropped: no cell labels, no $/byte totals, no user names.
const EMPTY_USERS = new Map<string, UserIndexEntry>()
const SLOTS = ['--s1', '--s2', '--s3', '--s4', '--s5', '--s6', '--s7', '--s8']

// communal is all-shared (washed-out swatch, matching its cells); the rest solid.
const TEAM_LEGEND: [string, string][] = [
  [groupLabel('communal'), sharedColor(TEAM_VARS.communal)],
  [groupLabel('oa'), `var(${TEAM_VARS.oa})`],
  [groupLabel('stanford'), `var(${TEAM_VARS.stanford})`],
  [groupLabel('unattributed'), `var(${TEAM_VARS.unattributed})`],
]

// Stores with no ownership overlay colour by top-level prefix instead, so their
// legend is the same slot assignment <Treemap> derives internally.
const treeLegend = (root: TreeNode): [string, string][] => {
  const bytes = new Map<string, number>()
  for (const bucket of root.c ?? [])
    for (const d of bucket.c ?? []) {
      const k = d.n.startsWith('(') ? '(other)' : d.n
      bytes.set(k, (bytes.get(k) ?? 0) + d.b)
    }
  return [...bytes.entries()]
    .filter(([k]) => k !== '(other)')
    .sort((a, b) => b[1] - a[1])
    .slice(0, 8)
    .map(([k], i): [string, string] => [k, `var(${SLOTS[i]})`])
}

export function OgPage({ store = DEFAULT_STORE }: { store?: Store }) {
  const [tree, setTree] = useState<TreeNode | null>(null)

  useEffect(() => {
    const prev = document.documentElement.dataset.theme
    document.documentElement.dataset.theme = 'dark'
    return () => {
      if (prev) document.documentElement.dataset.theme = prev
      else delete document.documentElement.dataset.theme
    }
  }, [])

  useEffect(() => {
    let cancel = false
    void fetch(`${store.base}/scans.json`)
      .then(r => r.json())
      .then((scans: string[]) => {
        const asof = scans[0]
        return asof ? fetch(`${store.base}/${asof}/tree.json`).then(r => r.json()) : null
      })
      .then((t: TreeNode | null) => t && !cancel && setTree(t))
    return () => { cancel = true }
  }, [store])

  const attributed = !!tree?.tm
  const legend = useMemo(
    () => (!tree ? [] : attributed ? TEAM_LEGEND : treeLegend(tree)),
    [tree, attributed],
  )

  return (
    <div className="og">
      <div className="og-head">
        <h1>{store.title}</h1>
        <p>{store.desc}</p>
      </div>
      <div className="og-map">
        {tree && (
          <Treemap
            root={tree}
            mode={attributed ? 'team' : 'tree'}
            userIdx={EMPTY_USERS}
            dateRange={null}
            scheme={store.scheme}
            redact
          />
        )}
      </div>
      <div className="og-legend">
        {legend.map(([k, c]) => (
          <span className="li" key={k}>
            <span className="sw" style={{ background: c }} />
            {k}
          </span>
        ))}
      </div>
    </div>
  )
}

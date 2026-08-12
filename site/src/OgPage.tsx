import { useEffect, useState } from 'react'
import { Treemap } from './Treemap'
import type { TreeNode } from './types'
import { TEAM_VARS, groupLabel, sharedColor } from './types'
import type { UserIndexEntry } from './colors'

// `/og` — a redacted, fixed-size (1200×630) render of the group treemap, used
// only to screenshot the public og:image. Reuses <Treemap redact> so the cell
// layout + group colors match `/` exactly, but every text detail is dropped:
// no cell labels, no $/byte totals, no user names. Group color key only.
const EMPTY_USERS = new Map<string, UserIndexEntry>()

// communal is all-shared (washed-out swatch, matching its cells); the rest solid.
const LEGEND: [string, string][] = [
  [groupLabel('communal'), sharedColor(TEAM_VARS.communal)],
  [groupLabel('oa'), `var(${TEAM_VARS.oa})`],
  [groupLabel('stanford'), `var(${TEAM_VARS.stanford})`],
  [groupLabel('unattributed'), `var(${TEAM_VARS.unattributed})`],
]

export function OgPage() {
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
    void fetch('/data/scans.json')
      .then(r => r.json())
      .then((scans: string[]) => {
        const asof = scans[0]
        return asof ? fetch(`/data/${asof}/tree.json`).then(r => r.json()) : null
      })
      .then((t: TreeNode | null) => t && !cancel && setTree(t))
    return () => { cancel = true }
  }, [])

  return (
    <div className="og">
      <div className="og-head">
        <h1>Marin GCS usage</h1>
        <p>
          Per-group storage attribution across the six <code>marin-*</code> GCS buckets
        </p>
      </div>
      <div className="og-map">
        {tree && <Treemap root={tree} mode="team" userIdx={EMPTY_USERS} dateRange={null} redact />}
      </div>
      <div className="og-legend">
        {LEGEND.map(([k, c]) => (
          <span className="li" key={k}>
            <span className="sw" style={{ background: c }} />
            {k}
          </span>
        ))}
      </div>
    </div>
  )
}

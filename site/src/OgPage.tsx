import { useEffect, useState } from 'react'
import { Treemap } from './Treemap'
import { buildUserIndex } from './colors'
import type { UserIndex } from './colors'
import type { Meta, TreeNode } from './types'

// `/og` — a redacted, fixed-size (1200×630) render of the per-user treemap,
// used only to screenshot the public og:image. Reuses <Treemap redact> so the
// cell layout + user colors match `/` exactly, but every text detail is
// dropped: no cell labels, no $/byte totals, no user names (hence no legend).
export function OgPage() {
  const [tree, setTree] = useState<TreeNode | null>(null)
  const [userIdx, setUserIdx] = useState<UserIndex>(new Map())

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
      .then(async (scans: string[]) => {
        const asof = scans[0]
        if (!asof || cancel) return
        const [t, m] = await Promise.all([
          fetch(`/data/${asof}/tree.json`).then(r => r.json()) as Promise<TreeNode>,
          fetch(`/data/${asof}/meta.json`).then(r => r.json()) as Promise<Meta>,
        ])
        if (cancel) return
        setUserIdx(buildUserIndex(m.users ?? []))
        setTree(t)
      })
    return () => { cancel = true }
  }, [])

  return (
    <div className="og">
      <div className="og-head">
        <h1>Marin GCS usage</h1>
        <p>
          Per-user storage attribution across the six <code>marin-*</code> GCS buckets
        </p>
      </div>
      <div className="og-map">
        {tree && <Treemap root={tree} mode="user" userIdx={userIdx} dateRange={null} redact />}
      </div>
    </div>
  )
}

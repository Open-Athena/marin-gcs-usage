import { useSyncExternalStore } from 'react'
import type { Tiling } from '@disk-tree/react'

// Treemap tiling preference: `shared` (cells share edges — one stroke per
// boundary, exact areas; the default) vs `gaps` (2px gutters, rounded). Same
// module-store + useSyncExternalStore shape as upstream disk-tree's
// `ui/src/utils/tiling.ts`; persisted in localStorage; every treemap reads it.
const KEY = 'gcs-usage:tiling'
const load = (): Tiling => {
  try {
    const v = localStorage.getItem(KEY)
    if (v === 'gaps' || v === 'shared') return v
  } catch { /* no storage */ }
  return 'shared'
}
let current: Tiling = load()
const listeners = new Set<() => void>()
const get = () => current
export const setTiling = (t: Tiling): void => {
  if (t === current) return
  current = t
  try { localStorage.setItem(KEY, t) } catch { /* in-memory only */ }
  listeners.forEach(l => l())
}
const subscribe = (l: () => void) => { listeners.add(l); return () => { listeners.delete(l) } }
export const useTiling = (): [Tiling, (t: Tiling) => void] => [useSyncExternalStore(subscribe, get, get), setTiling]

/** Two-button `shared | gaps` control — lives in the treemap's crumbs bar. */
export function TilingToggle() {
  const [t, set] = useTiling()
  return (
    <span className="tiling-toggle" role="radiogroup" aria-label="Treemap tiling" title="Treemap tiling: shared edges (one stroke per boundary, exact areas) vs gutters">
      {(['shared', 'gaps'] as Tiling[]).map(v => (
        <button key={v} type="button" role="radio" aria-checked={t === v} className={t === v ? 'on' : ''} onClick={() => set(v)}>{v}</button>
      ))}
    </span>
  )
}

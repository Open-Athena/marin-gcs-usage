import { useSyncExternalStore } from 'react'
import type { Tiling } from '@disk-tree/react'
import { Tooltip } from './Tooltip'

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

/** Single `gaps` toggle chip (off by default → shared-edge tiling) — lives in
 * the treemap's crumbs bar. */
export function TilingToggle() {
  const [t, set] = useTiling()
  const on = t === 'gaps'
  return (
    <Tooltip content="Cell gutters. Off (default): cells share edges — one stroke per boundary, areas stay exact. On: gaps and rounded corners between cells.">
      <button
        type="button"
        className={`tiling-toggle${on ? ' on' : ''}`}
        aria-pressed={on}
        onClick={() => set(on ? 'shared' : 'gaps')}
      >
        gaps
      </button>
    </Tooltip>
  )
}

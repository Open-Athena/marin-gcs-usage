import { useMemo } from 'react'
import { ACTION_COLORS } from './MarkControls'
import { ACTION_LABELS, useMarks } from './marks'

// The ledger's keep + owner rows, folded into one newest-first event stream —
// shared by the full `/marks` feed and the path-scoped "Mark history" section.

export interface MarkEvent {
  ts: number
  who: string
  prefix: string
  id: number
  label: string
  color: string
  memo: string | null
}

export function useMarkEvents(): { events: MarkEvent[]; isLoading: boolean; error: Error | null } {
  const { data, isLoading, error } = useMarks(true)
  const events = useMemo((): MarkEvent[] => {
    if (!data) return []
    const evs: MarkEvent[] = []
    for (const r of data.keeps)
      evs.push({
        ts: r.ts, who: r.who, prefix: r.prefix, id: r.action_id, memo: r.memo,
        label: r.keep == null ? 'cleared' : ACTION_LABELS[r.keep],
        color: r.keep == null ? 'var(--line)' : ACTION_COLORS[r.keep],
      })
    for (const r of data.owners)
      evs.push({
        ts: r.ts, who: r.who, prefix: r.prefix, id: r.action_id, memo: r.memo,
        label: r.owner == null ? 'released' : `claimed${r.owner === r.who ? '' : ` for ${r.owner}`}`,
        color: 'var(--t-oa)',
      })
    // Newest first; action_id breaks ties within the same second.
    return evs.sort((a, b) => b.ts - a.ts || b.id - a.id)
  }, [data])
  return { events, isLoading, error: error ?? null }
}

/** Events touching a prefix: those under it, plus ancestor marks that cover it. */
export const eventsUnder = (events: MarkEvent[], prefix: string): MarkEvent[] => {
  const p = prefix.endsWith('/') ? prefix : prefix + '/'
  return events.filter(e => {
    const ep = e.prefix.endsWith('/') ? e.prefix : e.prefix + '/'
    return ep.startsWith(p) || p.startsWith(ep)
  })
}

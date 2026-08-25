import { TimeSeries } from '@disk-tree/react'
import { useQuery } from '@tanstack/react-query'
import { useMemo } from 'react'
import type { Meta } from './types'
import { useUnits } from './units'

// Total stored bytes across the historical scans (specs/size-over-time.md, case
// pre-1). Built from the per-date `meta.json` (~4 KB each) — cheap, no tree
// loads. Fleet-total only for now; subpath / regex scoping needs the precomputed
// per-date index (see the spec), and a per-group stack needs a stacked-area mode
// (TimeSeries overlays, which reads wrong for nested magnitudes).

interface Pt { x: number; y: number }

export function SizeOverTime({ scans }: { scans: string[] }) {
  const { fmtBytes } = useUnits()
  const metas = useQuery({
    queryKey: ['size-series', scans],
    enabled: scans.length > 1,
    staleTime: Infinity,
    queryFn: async () => {
      const rows = await Promise.all(scans.map(async d => {
        const r = await fetch(`/data/${d}/meta.json`)
        if (!r.ok) return null
        return { date: d, m: (await r.json()) as Meta }
      }))
      return rows.filter((r): r is { date: string; m: Meta } => r != null)
    },
  })

  const series = useMemo(() => {
    const rows = metas.data ?? []
    if (rows.length < 2) return []
    return [{
      key: 'total',
      label: 'total',
      color: 'var(--s1)',
      // A calendar date id renders as a UTC instant here; fine for month/day ticks.
      points: rows.map(r => ({ x: new Date(r.date).getTime(), y: r.m.total_bytes })).sort((a, b) => a.x - b.x),
    }]
  }, [metas.data])

  if (scans.length < 2) return null
  return (
    <section>
      <h2>Size over time</h2>
      <p className="sub">
        Total stored bytes per scan (fleet-wide). Subpath &amp; regex scoping — and a per-group
        breakdown — are coming; see the size-over-time spec.
      </p>
      {metas.isError && <p className="tab-note" style={{ color: 'var(--s3)' }}>Couldn’t load the series.</p>}
      {series.length > 0 && (
        <TimeSeries<Pt>
          series={series}
          getX={p => p.x}
          getY={p => p.y}
          formatY={fmtBytes}
          formatX={x => new Date(x).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}
          yLabel="stored bytes"
          height={220}
        />
      )}
    </section>
  )
}

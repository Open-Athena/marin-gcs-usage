import { describe, expect, it } from 'vitest'
import { seriesToMap } from './overTime.js'

// Interval rows as the footer read returns them (one key's runs); seriesToMap
// wraps them as a MultiScan and expands via pyrmts' seriesFor, dropping absent
// (monoid-identity) scans.
const row = (depth: number, path: string, b: number, o: number, lo: number, hi: number) => ({ depth, path, b, o, __scan_lo: lo, __scan_hi: hi })

describe('seriesToMap', () => {
  const scans = ['2026-08-19T1201', '2026-08-19T2359', '2026-08-20T1201', '2026-08-20T2359']

  it('expands a key\'s runs into a point per covered scan', () => {
    const rows = [row(1, 'B', 100, 10, 0, 1), row(1, 'B', 200, 20, 2, 3)]
    expect([...seriesToMap(rows, scans, 1, 'B')]).toEqual([
      ['2026-08-19T1201', { b: 100, o: 10 }],
      ['2026-08-19T2359', { b: 100, o: 10 }],
      ['2026-08-20T1201', { b: 200, o: 20 }],
      ['2026-08-20T2359', { b: 200, o: 20 }],
    ])
  })

  it('drops absent (identity) scans — gap where the path did not exist', () => {
    // present only scans 1–2
    const m = seriesToMap([row(2, 'X', 7, 1, 1, 2)], scans, 2, 'X')
    expect([...m.keys()]).toEqual(['2026-08-19T2359', '2026-08-20T1201'])
  })

  it('returns only the queried key\'s line', () => {
    const rows = [row(1, 'B', 100, 10, 0, 3), row(2, 'B/a', 50, 5, 0, 3)]
    expect([...seriesToMap(rows, scans, 2, 'B/a').values()]).toEqual([
      { b: 50, o: 5 }, { b: 50, o: 5 }, { b: 50, o: 5 }, { b: 50, o: 5 },
    ])
  })
})

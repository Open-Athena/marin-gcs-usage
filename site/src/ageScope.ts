import type { AgeRow } from './types'

/** The age chart's rows under the page filter (specs/filter-views.md §4).
 * Age data exists per top-level dir only, so the chart can follow the filter
 * exactly when every match root IS a top-level dir; otherwise it stays whole
 * and the caller says so. */
export function scopeAgeRows(rows: AgeRow[], roots: string[] | undefined): { rows: AgeRow[]; scoped: boolean } {
  if (!roots?.length) return { rows, scoped: false }
  if (roots.some(r => r.includes('/'))) return { rows, scoped: false }
  const top = new Set(roots)
  return { rows: rows.filter(r => top.has(r.d1)), scoped: true }
}

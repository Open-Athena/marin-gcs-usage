import { describe, expect, it } from 'vitest'
import { scopeAgeRows } from './ageScope'
import type { AgeRow } from './types'

const rows: AgeRow[] = [
  { d: 20000, d1: 'tmp', b: 10, o: 1 },
  { d: 20001, d1: 'marin', b: 20, o: 2 },
  { d: 20002, d1: 'iris', b: 30, o: 3 },
]

describe('scopeAgeRows', () => {
  it('no filter: every row, not scoped', () => {
    expect(scopeAgeRows(rows, undefined)).toEqual({ rows, scoped: false })
    expect(scopeAgeRows(rows, [])).toEqual({ rows, scoped: false })
  })
  it('top-level match roots scope the rows exactly', () => {
    expect(scopeAgeRows(rows, ['tmp', 'iris'])).toEqual({ rows: [rows[0], rows[2]], scoped: true })
  })
  it('a nested match root cannot be honoured by per-top-level age data: whole rows, not scoped', () => {
    expect(scopeAgeRows(rows, ['tmp/ttl=14d', 'iris'])).toEqual({ rows, scoped: false })
  })
})

import { describe, expect, it } from 'vitest'
import { planAge, variantForPlan } from './agePyramid'

describe('planAge — budget picks the finest tier that fits', () => {
  // A 30-day window: 1h≈720 bins, 1d≈30, 1mo=1, 1y=1.
  const FROM = new Date('2026-06-01T00:00:00Z')
  const TO = new Date('2026-07-01T00:00:00Z')
  const cases: [number, string][] = [
    [1000, 'age-pyramid-1h'],  // 1h (~720) fits
    [100, 'age-pyramid-1d'],   // 1h too many, 1d (~30) fits
    [5, 'age-pyramid-1mo'],    // 1d too many, 1mo (1) fits
  ]
  for (const [budget, variant] of cases) {
    it(`30d, budget ${budget} → ${variant}`, () => {
      expect(variantForPlan(planAge(FROM, TO, budget))).toBe(variant)
    })
  }

  it('a 6-year window under budget 3 falls back to the coarsest tier (1y)', () => {
    const from = new Date('2020-01-01T00:00:00Z')
    const to = new Date('2026-01-01T00:00:00Z')
    expect(variantForPlan(planAge(from, to, 3))).toBe('age-pyramid-1y')
  })
})

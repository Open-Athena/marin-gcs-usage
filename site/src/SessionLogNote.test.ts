import { describe, expect, it } from 'vitest'
import { sessionLogNotice } from './SessionLogNote'

describe('the privacy notice', () => {
  it('names the last day logging runs (the switch ends at the next UTC midnight)', () => {
    expect(sessionLogNotice(Date.UTC(2026, 9, 18))).toBe('While enabled (through Oct 17), this site records your clicks and page views to help debug issues.')
  })
})

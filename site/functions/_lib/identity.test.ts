import { describe, expect, it } from 'vitest'
import { canonId, whoToHandle } from './identity.js'

describe('canonId', () => {
  it('maps a sign-in email to its registry id via the handle', () => {
    expect(whoToHandle('gonzalo.benegas@example.org')).toBe('gonzalo-benegas')
    expect(canonId('gonzalo.benegas@example.org')).toBe('gonzalo-benegas')
  })
  it('follows alias keys to the canonical id', () => {
    expect(canonId('gonzalobenegas@example.org')).toBe('gonzalo-benegas')
    expect(canonId('Gonzalo@example.org')).toBe('gonzalo-benegas')
  })
  it('falls back to the sanitized handle for an unknown user', () => {
    expect(canonId('new.person+x@example.org')).toBe('new-person-x')
  })
})

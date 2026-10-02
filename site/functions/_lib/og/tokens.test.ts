import { describe, expect, it } from 'vitest'
import { sqliteD1 } from '../testD1'
import { EPOCH, ogKey } from './sign'
import { fullTier, listTokens, mint, revoke, tokenLive } from './tokens'

// 2026-10-02T12:00Z: day 274.
const NOW = EPOCH + 274 * 86400 + 12 * 3600
const page = (p: string) => new URL(`https://site.example.org${p}`)

describe('og_tokens (gcs lineage through 0034, foreign keys on)', () => {
  it('mint → full tier for exactly that view; revoke → anonymous', async () => {
    const { db } = await sqliteD1('gcs')
    const key = await ogKey('s3cret')
    const m = await mint(db, key, 'marin GCS', page('/marin-a/ckpt?d=261002&n=50&og=stale'), 'ann@example.org', NOW, 30)
    expect(m).toEqual({ token: m!.token, url: `https://site.example.org/marin-a/ckpt?d=261002&n=50&og=${m!.token}`, expDay: 304 })
    const view = { path: 'marin-a/ckpt', d: '261002' }
    expect([
      await fullTier(db, key, 'map', view, m!.token, NOW),
      await fullTier(db, key, 'map', { path: 'marin-a' }, m!.token, NOW),
      await fullTier(db, key, 'map', view, null, NOW),
    ]).toEqual([{ day: 304 }, null, null])
    // A second mint of the same view and day: the same token, a second row.
    const again = await mint(db, key, 'marin GCS', page('/marin-a/ckpt?d=261002'), 'bo@example.org', NOW + 60, 30)
    expect([again!.token === m!.token, (await listTokens(db)).map(r => [r.token === m!.token, r.minted_by, r.page, r.exp_day, r.revoked_ts])]).toEqual([true, [
      [true, 'bo@example.org', '/marin-a/ckpt?d=261002', 304, null],
      [true, 'ann@example.org', '/marin-a/ckpt?d=261002&n=50', 304, null],
    ]])
    expect([await revoke(db, m!.token, 'admin@example.org', NOW + 120), await revoke(db, m!.token, 'admin@example.org', NOW + 180), await tokenLive(db, m!.token), await fullTier(db, key, 'map', view, m!.token, NOW)])
      .toEqual([2, 0, false, null])
  })
  it('a token D1 never recorded is not honoured, however well-formed', async () => {
    const { db } = await sqliteD1('gcs')
    const key = await ogKey('s3cret')
    const { mintToken } = await import('./sign')
    expect(await fullTier(db, key, 'map', { path: 'x' }, await mintToken(key, 'map', { path: 'x' }, 300), NOW)).toBe(null)
  })
  it('pages without a card mint nothing', async () => {
    const { db } = await sqliteD1('gcs')
    expect(await mint(db, await ogKey('s3cret'), 'marin GCS', page('/api/subtree'), 'ann@example.org', NOW)).toBe(null)
  })
})

describe('a deployment without the table (cw lineage)', () => {
  it('honours no token', async () => {
    const { db } = await sqliteD1('cw')
    expect(await tokenLive(db, 'EZjUobkMrUuF8')).toBe(false)
  })
})

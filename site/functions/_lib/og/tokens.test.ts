import { describe, expect, it } from 'vitest'
import { sqliteD1 } from '../testD1'
import { EPOCH, ogKey } from './sign'
import { hashToken } from '@open-athena/auth'
import { fullTier, listTokens, mint, revoke, shareKeyLive, tokenLive } from './tokens'

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
    expect(await tokenLive(db, 'EZjUobkMrUuF')).toBe(false)
  })
})

describe('shareKeyLive: a `key=` share link earns the full card iff its bearer would get in', () => {
  it('the gate\'s presented-token rule (a used-up redeem cap still serves a bearer); a viewer scope; nothing redeemed or touched', async () => {
    const { db, raw } = await sqliteD1('gcs')
    const K = (s: string) => `${s}-share-link-key-000000`
    const grant = async (name: string, cols: Record<string, string | number> = {}) => {
      const row: Record<string, string | number> = { id: name, token_hash: await hashToken(K(name)), scopes: 'gcs:read', created_at: 1, created_by: 'admin@example.org', ...cols }
      const ks = Object.keys(row)
      raw.prepare(`INSERT INTO grants (${ks.join(',')}) VALUES (${ks.map(() => '?').join(',')})`).run(...Object.values(row))
    }
    await grant('live')
    await grant('viewer', { scopes: 'gcs' })
    await grant('revoked', { revoked_at: NOW - 1 })
    await grant('disabled', { disabled_at: NOW - 1 })
    await grant('expired', { expires_at: NOW - 1 })
    await grant('used-up', { max_redeems: 1, redeems: 1 })
    await grant('other-scope', { scopes: 'cw:read' })
    const live = (k: string | null) => shareKeyLive(db, k, ['gcs', 'gcs:read'], NOW)
    expect([
      await live(K('live')), await live(K('viewer')), await live(K('revoked')), await live(K('disabled')), await live(K('expired')),
      await live(K('used-up')), await live(K('other-scope')), await live(K('unknown')), await live(null), await live('short'),
    ]).toEqual([true, true, false, false, false, true, false, false, false, false])
    expect(raw.prepare('SELECT SUM(redeems) AS r, COUNT(last_used_at) AS used FROM grants').all()).toEqual([{ r: 1, used: 0 }])
  })
})

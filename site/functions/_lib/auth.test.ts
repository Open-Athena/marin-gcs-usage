import { describe, expect, it } from 'vitest'
import { type Env, identify, isAdmin, scopesFor } from './auth'

// A D1 stand-in for the two policy lookups: `allowed_emails` and `admin_emails`
// rows, keyed by table. Only the `prepare().bind().first()` shape the policy
// uses is modelled; any other SQL is a test bug.
const db = (rows: { allowed?: string[]; admin?: string[] }) => ({
  prepare: (sql: string) => {
    const table = /FROM (\w+)/.exec(sql)?.[1]
    const have = table === 'allowed_emails' ? rows.allowed ?? [] : table === 'admin_emails' ? rows.admin ?? [] : null
    if (have === null) throw new Error(`unexpected SQL in policy: ${sql}`)
    return { bind: (email: string) => ({ first: async () => (have.includes(email) ? { email } : null) }) }
  },
}) as unknown as Env['DB']

const cw = (rows: { allowed?: string[]; admin?: string[] }, extra: Partial<Env> = {}): Env => ({
  DB: db(rows),
  BASE_SCOPE: 'cw',
  STAFF_DOMAIN: 'openathena.ai',
  VIEWER_DOMAINS: 'coreweave.com',
  ADMIN_EMAILS: '1',
  ...extra,
})

describe('scopesFor — the in-app policy that replaces the Access policy', () => {
  it('staff get every scope', async () => {
    expect(await scopesFor(cw({}))('ryan@openathena.ai')).toEqual(['gcs', 'cw', 'admin', 'requests'])
  })

  it('a viewer domain gets the base scope with no allowlist row', async () => {
    expect(await scopesFor(cw({}))('someone@coreweave.com')).toEqual(['cw'])
  })

  it('a viewer-domain admin_emails row adds admin', async () => {
    expect(await scopesFor(cw({ admin: ['ops@coreweave.com'] }))('ops@coreweave.com')).toEqual(['cw', 'admin'])
  })

  it('an allowed_emails row admits any other address, lower-cased', async () => {
    expect(await scopesFor(cw({ allowed: ['guest@example.org'] }))('Guest@Example.org')).toEqual(['cw'])
  })

  it('an unlisted address from an unlisted domain is denied', async () => {
    expect(await scopesFor(cw({ allowed: ['guest@example.org'] }))('other@example.org')).toBeNull()
  })

  it('without ADMIN_EMAILS the admin table is never consulted (gcs shape)', async () => {
    const env = cw({ allowed: ['guest@example.org'] }, { ADMIN_EMAILS: undefined, VIEWER_DOMAINS: undefined, BASE_SCOPE: 'gcs' })
    expect(await scopesFor(env)('guest@example.org')).toEqual(['gcs'])
    expect(await scopesFor(env)('someone@coreweave.com')).toBeNull()
  })

  it('no DB (local dev) admits non-staff to the base scope', async () => {
    expect(await scopesFor({ BASE_SCOPE: 'cw' })('anyone@example.org')).toEqual(['cw'])
  })
})

describe('isAdmin', () => {
  it('staff by domain, otherwise the admin_emails row where the table exists', async () => {
    expect(await isAdmin(cw({}), 'ryan@openathena.ai')).toBe(true)
    expect(await isAdmin(cw({ admin: ['ops@coreweave.com'] }), 'Ops@coreweave.com')).toBe(true)
    expect(await isAdmin(cw({ admin: ['ops@coreweave.com'] }), 'other@coreweave.com')).toBe(false)
    expect(await isAdmin(cw({ admin: ['ops@coreweave.com'] }, { ADMIN_EMAILS: undefined }), 'ops@coreweave.com')).toBe(false)
  })
})

describe('identify — the edge header is only trusted behind an edge', () => {
  const withEdgeJwt = (env: Env) => identify({
    request: new Request('https://cw-s3.oa.dev/api/whoami', { headers: { 'Cf-Access-Jwt-Assertion': 'not-a-jwt' } }),
    env,
  })

  it('no ACCESS_AUD and no gate → anonymous, without touching the header', async () => {
    expect(await withEdgeJwt(cw({}))).toBeNull()
  })
})

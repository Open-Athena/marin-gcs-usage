// The bulk bar's real submission, end to end against the handlers over a local D1 (`sqliteD1`): a large set
// goes in batches, and lands as exactly the prefixes sent — staging as ONE batch (merged), the ledger as one
// row per prefix. (Never against a deployment's D1: dev shares prod's.)
import { describe, expect, it } from 'vitest'
import { onRequest as actionsRoute } from './actions'
import { onRequest as plansRoute } from './plans/[[path]]'
import { sqliteD1 } from '../_lib/testD1'
import { assignInBatches, stageMany } from '../../src/batches'

const B = 'bkt'
const base = { STAGING: '1', STORE_SCHEME: 'gs://', STORE_BUCKETS: B, DEV_EMAIL: 'ann@example.test' }

describe('bulk submission against a local D1', () => {
  it('stages 1,100 matching folders in 3 POSTs, merged into one batch', async () => {
    const { db, raw } = await sqliteD1('cw')
    raw.exec(`INSERT INTO index_schema (date, variant, version, schema_json) VALUES ('2026-10-09', 'path', 2, '[]')`)
    const env = { ...base, DB: db }
    const prefixes = Array.from({ length: 1100 }, (_, i) => `gs://${B}/d${String(i).padStart(4, '0')}/`)
    const sent: string[] = []
    const post = (async (url: string, method: string, body: unknown) => {
      sent.push(url)
      const r = await plansRoute({ request: new Request(`http://localhost${url}`, { method, body: JSON.stringify(body) }), env } as never)
      const j = await r.json()
      if (!r.ok) throw new Error(JSON.stringify(j))
      return j
    }) as never
    const res = await stageMany(prefixes, 'tomat matches', undefined, post)
    const staged = await (await plansRoute({ request: new Request('http://localhost/api/plans/staged'), env } as never)).json() as { items: { prefix: string; batch_id: number }[]; batches: { id: number }[] }
    expect([
      sent,
      [res.plan_id, res.batch_id, res.staged.length],
      staged.batches.map(b => b.id),
      [...new Set(staged.items.map(i => i.batch_id))],
      staged.items.map(i => i.prefix).sort(),
    ]).toEqual([
      ['/api/plans/stage', '/api/plans/stage', '/api/plans/stage', '/api/plans/1/batches/merge'],
      [1, 1, 1100],
      [1],
      [1],
      [...prefixes].sort(),
    ])
  })

  it('assigns 1,100 folder prefixes in 3 POSTs (the API takes ≤ 500), one ledger row each', async () => {
    const { db } = await sqliteD1('cw')
    await db.prepare('CREATE TABLE actions (id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT, ts REAL, scan TEXT, pattern TEXT, set_owner INTEGER, owner TEXT, memo TEXT)').run()
    await db.prepare('CREATE TABLE owner_prefixes (prefix TEXT, owner TEXT, ts REAL, action_id INTEGER, tombstoned REAL)').run()
    const env = { ...base, DB: db }
    const prefixes = Array.from({ length: 1100 }, (_, i) => `gs://${B}/d${String(i).padStart(4, '0')}/`)
    const statuses: number[] = []
    const n = await assignInBatches(prefixes, pattern => ({ pattern, owner: 'ann', memo: "bulk filter:'tomat'", scan: '2026-10-09' }), async actions => {
      const r = await actionsRoute({ request: new Request('http://localhost/api/actions', { method: 'POST', body: JSON.stringify(actions) }), env } as never)
      statuses.push(r.status)
      if (!r.ok) throw new Error(await r.text())
    })
    const rows = (await db.prepare('SELECT prefix, owner FROM owner_prefixes ORDER BY prefix').all<{ prefix: string; owner: string }>()).results
    expect([n, statuses, rows.length, rows.map(r => r.prefix), [...new Set(rows.map(r => r.owner))]]).toEqual([1100, [200, 200, 200], 1100, prefixes, ['ann']])
  })
})

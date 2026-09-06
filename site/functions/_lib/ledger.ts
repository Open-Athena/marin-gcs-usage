/** The live ledger (marks + claims) and its head, straight from D1 — the
 * WAL every read applies on top of the scan (specs/view-serving.md §2). */
import type { Env } from './auth.js'
import type { KeepRow, OwnerRow } from './marks.js'

export interface Ledger {
  keepRows: KeepRow[]
  ownerRows: OwnerRow[]
  /** `MAX(actions.id)`: changes iff the ledger changed — the cache key for
   * anything folded from it. */
  head: number
}

export async function loadLedger(env: Env): Promise<Ledger> {
  if (!env.DB) throw new Error('ledger backend not configured (DB)')
  const [keepRows, ownerRows, headRow] = await Promise.all([
    env.DB.prepare(
      'SELECT k.prefix, k.keep, k.ts, a.actor AS who, a.id AS action_id ' +
      'FROM keep_prefixes k JOIN actions a ON a.id = k.action_id WHERE k.tombstoned IS NULL',
    ).all<KeepRow>(),
    env.DB.prepare(
      'SELECT o.prefix, o.owner, o.ts, a.id AS action_id ' +
      'FROM owner_prefixes o JOIN actions a ON a.id = o.action_id WHERE o.tombstoned IS NULL',
    ).all<OwnerRow>(),
    env.DB.prepare('SELECT COALESCE(MAX(id), 0) AS head FROM actions').first<{ head: number }>(),
  ])
  return { keepRows: keepRows.results, ownerRows: ownerRows.results, head: headRow?.head ?? 0 }
}

/** Just the head (one tiny query) — for cache keys before deciding whether a
 * full fold is needed. */
export async function ledgerHead(env: Env): Promise<number> {
  if (!env.DB) throw new Error('ledger backend not configured (DB)')
  const row = await env.DB.prepare('SELECT COALESCE(MAX(id), 0) AS head FROM actions').first<{ head: number }>()
  return row?.head ?? 0
}
